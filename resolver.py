"""答え合わせ(verify)層。

期日が来た予測(hunch)を、公開情報を証拠に○×判定する。CI上は汎用Web検索が使えないため、
証拠源は HN Algolia 全文検索 + 公開ソースURL取得に限る。判定はハイブリッド:
- 証拠が明確な時だけ自動で hit/miss(status=resolved)
- 曖昧/証拠なしは unclear のまま needs_review=True で保留(人が最終確認)
**誤った○×は絶対に出さない**(打率の信頼が命)。特に miss は「起きなかったと確信できる時」だけ。

同時に**決着を落とさない**ことも守る。API の 429 やタイムアウトで実Web確認ができなかった日は、
「世界が曖昧だった」のではなく「こちらが見に行けなかった」だけなので、needs_review にせず
翌日やり直す(judge_attempts / MAX_ATTEMPTS)。一時的な詰まりを恒久的な判決にすると、
外れかけの予測ほど成績から消えて打率が実際より良く見える(生存者バイアス)。

サイト本体とは独立。CIでは別ステップ・continue-on-error で走り、失敗しても公開を止めない。
"""
import os, json, urllib.parse
from datetime import datetime, timedelta

from common import GeminiClient, http, parse_json
from recorder import load_json_array, dump_json, fetch_body, HUNCHES_PATH, JST, render_pages

CONF_HIT = 0.66      # hit を自動確定する最低確度
CONF_MISS = 0.75     # miss はより慎重に(偽×を出さない)
MAX_PER_RUN = 12     # 1実行で判定する期日到来hunchの上限
STALE_DAYS = 14      # 期日から この日数 を過ぎてなお未解決なら unscorable で終端
MAX_ATTEMPTS = 3     # 一時障害でのやり直し上限(これを超えたら needs_review で人に回す)
RESULT_SET = ("hit", "miss", "unclear")


def _past_stale(h, today):
    """期日から STALE_DAYS を超えて経過しているか(終端判定用)。"""
    try:
        dl = datetime.strptime(str(h.get("deadline", "")), "%Y-%m-%d").date()
        return (today - dl).days > STALE_DAYS
    except Exception:
        return False


def hn_search(query, since_dt, until_dt=None):
    """check_query で HN を検索し、作成日以降(必要なら期日まで)のヒットを証拠候補として返す。"""
    if not query.strip():
        return []
    try:
        nf = "created_at_i>%d" % int(since_dt.timestamp())
        if until_dt is not None:  # 期日の上限=期日後の証拠を「期日までの的中」と誤認しない
            nf += ",created_at_i<%d" % int(until_dt.timestamp())
        url = ("https://hn.algolia.com/api/v1/search?query=" + urllib.parse.quote(query)
               + "&tags=story&hitsPerPage=8&numericFilters=" + urllib.parse.quote(nf))
        d = json.loads(http(url).decode())
        out = []
        for h in d.get("hits", []):
            out.append({"title": h.get("title") or "",
                        "url": h.get("url") or ("https://news.ycombinator.com/item?id=" + str(h.get("objectID"))),
                        "points": h.get("points", 0), "date": (h.get("created_at", "") or "")[:10]})
        return out
    except Exception as e:
        print("hn_search", repr(e)[:120])
        return []


def gather_evidence(h):
    """hunch の証拠テキストを組む(HN検索 + source がURLなら本文)。"""
    res = h.get("resolution", {}) if isinstance(h.get("resolution"), dict) else {}
    try:
        since = datetime.strptime((h.get("created_at", "") or "")[:10], "%Y-%m-%d").replace(tzinfo=JST)
    except Exception:
        since = datetime.now(JST)
    until = None  # 期日+2日を証拠の上限に(期日後の出来事を的中証拠にしない)
    try:
        until = datetime.strptime(str(h.get("deadline", "")), "%Y-%m-%d").replace(tzinfo=JST) + timedelta(days=2)
    except Exception:
        pass
    hits = hn_search(str(res.get("check_query") or h.get("subject") or ""), since, until)
    lines = []
    for x in hits:
        lines.append("- [%s] %s (%spt) %s" % (x["date"], x["title"], x["points"], x["url"]))
    src = str(res.get("source") or "")
    if src.startswith("http"):
        body, ok = fetch_body(src)
        if ok:
            lines.append("公式ソース抜粋: " + body[:1200])
    return "\n".join(lines) if lines else "(公開情報から証拠を取得できず)"


def _last_json(text):
    """テキスト中で最後に完全デコードできる"トップレベル"JSON値を返す(グラウンディングは
    散文の末尾に判定JSONを置くため、common.parse_json の"最初"ではなく"最後"を拾う)。
    デコード済み範囲はスキップし、ネストした内側オブジェクトを誤って拾わない。"""
    dec = json.JSONDecoder()
    found = None
    i, n = 0, len(text or "")
    while i < n:
        if text[i] in "{[":
            try:
                v, end = dec.raw_decode(text[i:])
                found = v
                i += end
                continue
            except ValueError:
                pass
        i += 1
    return found


def _norm_verdict(d):
    """{result, evidence{summary,url}, confidence} に正規化。不正なら None。"""
    if not isinstance(d, dict) or d.get("result") not in RESULT_SET:
        return None
    ev = d.get("evidence") if isinstance(d.get("evidence"), dict) else {}
    d["evidence"] = {"summary": str(ev.get("summary", ""))[:400], "url": str(ev.get("url", ""))[:400]}
    return d


def _judge_prompt(h, grounded):
    res = h.get("resolution", {}) if isinstance(h.get("resolution"), dict) else {}
    today = datetime.now(JST).strftime("%Y-%m-%d")
    head = ("本日はJSTで " + today + "。次の予測が作成日から期日までに的中したかを、"
            + ("Google検索で最新の公開情報を実際に確認して" if grounded else "集めた証拠だけで")
            + "判定せよ。\n")
    body = ("予測(claim): " + str(h.get("claim", "")) + "\n"
            "的中条件(decider): " + str(res.get("decider", "")) + "\n"
            "主体: " + str(h.get("subject", "")) + " / 作成日: " + str(h.get("created_at", ""))[:10]
            + " / 期限: " + str(h.get("deadline", "")) + "\n")
    # 期間中に観測された指標の点灯は一次材料なので判定に渡す(見落とさないため)
    sigs = h.get("signals") if isinstance(h.get("signals"), list) else []
    if sigs:
        body += ("期間中に観測された兆候(参考。これ自体は的中の証明ではない):\n"
                 + "\n".join("- [%s] %s: %s" % (s.get("date", ""),
                                                "確認寄り" if s.get("dir") != "kill" else "否定寄り",
                                                str(s.get("why") or s.get("sign", ""))[:120])
                             for s in sigs[-5:]) + "\n")
    rule = ("厳守: 公開情報で確認できない/不十分なら必ず unclear。憶測で hit/miss を出すな。"
            "miss は『起きなかったと確信できる時』だけ(単に証拠が見つからないだけなら unclear)。\n")
    tail = ('最後に判定を次のJSON1つだけで出力せよ(前に説明があってもよい): '
            '{"result":"hit|miss|unclear",'
            '"evidence":{"summary":"判定根拠を一文・日本語","url":"最も根拠になるURL(なければ空文字)"},'
            '"confidence":0〜1の数値}')
    return head + body + rule + tail


def judge(client, h, fake=None, meta=None):
    """答え合わせ。グラウンディング(実Web検索)を優先し、失敗時はHN証拠にフォールバック。

    meta: dict を渡すと {"grounded": bool} を書き戻す。grounded=False は
    **実Web確認ができなかった**印。呼び出し側はこれを見て、一時障害由来の unclear を
    恒久的な保留(needs_review)に変えず、翌日やり直す。
    """
    if meta is None:
        meta = {}
    meta["grounded"] = False
    if fake is not None:
        meta["grounded"] = True      # fake は「判定できた」扱い(オフラインテストの意味を保つ)
        return fake
    # 1) Google検索グラウンディングで実際に確認
    try:
        text = client.generate_grounded(_judge_prompt(h, grounded=True))
        v = _norm_verdict(_last_json(text))
        if v:
            meta["grounded"] = True
            if not v["evidence"].get("url") and getattr(client, "last_grounding_urls", None):
                v["evidence"]["url"] = client.last_grounding_urls[0]["url"]
            return v
        print("resolver: grounding応答から判定JSONを抽出できず→HNフォールバック")
    except Exception as e:
        print("resolver: grounding失敗→HNフォールバック", repr(e)[:120])
    # 2) フォールバック: HN全文検索の証拠でJSON判定
    evidence = gather_evidence(h)
    prompt = _judge_prompt(h, grounded=False) + "\n\n集めた証拠:\n" + evidence
    for _ in range(2):
        try:
            v = _norm_verdict(parse_json(client.generate(prompt)))
            if v:
                return v
        except Exception as e:
            print("judge(fallback)", repr(e)[:120])
    return None


def _defer_or_review(h, why):
    """一時障害で判定できなかった時の扱い。needs_review を立てず**翌日やり直す**。
    MAX_ATTEMPTS 回続けて駄目なら、そこで初めて人に回す(無限リトライでチャーンさせない)。"""
    n = int(h.get("judge_attempts") or 0) + 1
    h["judge_attempts"] = n
    if n >= MAX_ATTEMPTS:
        h["needs_review"] = True
        print("resolver:", h.get("id"), "→ 保留(要確認 %s・%d回失敗)" % (why, n))
    else:
        print("resolver:", h.get("id"), "→ 再挑戦へ(%s %d/%d)" % (why, n, MAX_ATTEMPTS))


def resolve_all(fake_map=None):
    hunches = load_json_array(HUNCHES_PATH)
    today = datetime.now(JST).date()
    now_iso = datetime.now(JST).isoformat()

    # 対象: pending かつ 期日到来 かつ 未resolved
    due = []
    for h in hunches:
        if h.get("status") != "pending" or h.get("resolved_at"):
            continue
        try:
            if datetime.strptime(str(h.get("deadline", "")), "%Y-%m-%d").date() <= today:
                due.append(h)
        except Exception:
            continue
    due.sort(key=lambda x: str(x.get("deadline", "")))   # 期日を過ぎた順=待たせている順に判定
    due = due[:MAX_PER_RUN]
    if not due:
        print("resolver: 期日到来の未判定予測なし。無操作")
        render_pages()  # 表示は最新のままにしておく
        return

    client = None
    if fake_map is None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            print("resolver: GEMINI_API_KEY 未設定。判定スキップ")
            return
        client = GeminiClient(api_key, call_limit=MAX_PER_RUN * 3)

    changed = False
    for h in due:
        # #3 既に needs_review の予測は再判定しない(grounding quota / 毎日のコミットチャーンを回避)。
        # 期日を大きく過ぎてなお未解決なら unscorable で終端(judge を呼ばない=ネット/fake不要)。
        if h.get("needs_review"):
            if _past_stale(h, today):
                h["status"] = "unscorable"
                h["needs_review"] = False
                changed = True
                print("resolver:", h.get("id"), "→ unscorable(期日+%d日超・終端)" % STALE_DAYS)
            continue

        # #5 fake密閉: fakeモードで応答が用意されていない due は触らない(実ネット遮断)
        fake = (fake_map or {}).get(h.get("id")) if fake_map is not None else None
        if fake_map is not None and fake is None:
            continue

        meta = {}
        verd = judge(client, h, fake=fake, meta=meta)
        if not verd:
            _defer_or_review(h, "判定応答なし")   # 応答が取れないのは一時障害。翌日やり直す
            changed = True
            continue
        result = verd.get("result")
        try:
            conf = float(verd.get("confidence"))
        except Exception:
            conf = 0.0
        ev = verd.get("evidence") if isinstance(verd.get("evidence"), dict) else {}
        ev = {"summary": str(ev.get("summary", ""))[:400], "url": str(ev.get("url", ""))[:400]}

        # #1 出典URLが無い hit/miss は自動確定しない(出典ゼロの判定を打率に載せない)
        has_src = bool(ev.get("url"))
        auto = has_src and ((result == "hit" and conf >= CONF_HIT)
                            or (result == "miss" and conf >= CONF_MISS))
        if auto:
            h["result"] = result
            h["evidence"] = ev
            h["resolved_at"] = now_iso
            h["status"] = "resolved"
            h["needs_review"] = False
            changed = True
            print("resolver:", h.get("id"), "→", result, "(conf %.2f, 出典あり)" % conf)
        elif result == "unclear" and not meta.get("grounded"):
            # **決着を落とさない**: 実Web確認(グラウンディング)ができないまま出た unclear は、
            # 「世界が曖昧」ではなく「こちらが見に行けなかった」。恒久的な保留にせず翌日やり直す。
            # (429 で初の答え合わせ2件を永久に失った実障害への対策)
            h["evidence"] = ev
            _defer_or_review(h, "実Web確認できず")
            changed = True
        else:
            # 曖昧/確度不足/出典なし → 保留(要確認)。証拠メモは残す。偽×は出さない。
            h["needs_review"] = True
            h["evidence"] = ev  # 人が確認するための手掛かり
            changed = True
            why = "出典なし" if (result in ("hit", "miss") and not has_src) else "確度不足/unclear"
            print("resolver:", h.get("id"), "→ 保留(要確認 %s) result=%s conf=%.2f" % (why, result, conf))

    if changed:
        dump_json(HUNCHES_PATH, hunches)
    render_pages()  # ○×を反映して台帳/index を再生成
    print("resolver: done due=%d changed=%s" % (len(due), changed))


def main():
    fake_path = os.environ.get("RESOLVER_FAKE_RESPONSE")  # テスト用シーム
    fake_map = None
    if fake_path:
        try:
            with open(fake_path, encoding="utf-8") as f:
                fake_map = json.load(f)
        except Exception as e:
            print("resolver: fake 読み込み失敗", e)
            fake_map = {}
    resolve_all(fake_map=fake_map)


if __name__ == "__main__":
    main()
