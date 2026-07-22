"""答え合わせ(verify)層。

期日が来た予測(hunch)を、公開情報を証拠に○×判定する。CI上は汎用Web検索が使えないため、
証拠源は HN Algolia 全文検索 + 公開ソースURL取得に限る。判定はハイブリッド:
- 証拠が明確な時だけ自動で hit/miss(status=resolved)
- 曖昧/証拠なしは unclear のまま needs_review=True で保留(人が最終確認)
**誤った○×は絶対に出さない**(打率の信頼が命)。特に miss は「起きなかったと確信できる時」だけ。

サイト本体とは独立。CIでは別ステップ・continue-on-error で走り、失敗しても公開を止めない。
"""
import os, json, urllib.parse
from datetime import datetime

from common import GeminiClient, http, parse_json
from recorder import load_json_array, dump_json, fetch_body, HUNCHES_PATH, JST, render_pages

CONF_HIT = 0.66      # hit を自動確定する最低確度
CONF_MISS = 0.75     # miss はより慎重に(偽×を出さない)
MAX_PER_RUN = 12     # 1実行で判定する期日到来hunchの上限
RESULT_SET = ("hit", "miss", "unclear")


def hn_search(query, since_dt):
    """check_query で HN を検索し、作成日以降のヒットを証拠候補として返す。"""
    if not query.strip():
        return []
    try:
        since_ts = int(since_dt.timestamp())
        url = ("https://hn.algolia.com/api/v1/search?query=" + urllib.parse.quote(query)
               + "&tags=story&hitsPerPage=8&numericFilters="
               + urllib.parse.quote("created_at_i>%d" % since_ts))
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
    hits = hn_search(str(res.get("check_query") or h.get("subject") or ""), since)
    lines = []
    for x in hits:
        lines.append("- [%s] %s (%spt) %s" % (x["date"], x["title"], x["points"], x["url"]))
    src = str(res.get("source") or "")
    if src.startswith("http"):
        body, ok = fetch_body(src)
        if ok:
            lines.append("公式ソース抜粋: " + body[:1200])
    return "\n".join(lines) if lines else "(公開情報から証拠を取得できず)"


def judge(client, h, evidence, fake=None):
    if fake is not None:
        return fake
    res = h.get("resolution", {}) if isinstance(h.get("resolution"), dict) else {}
    prompt = (
        "あなたは予測の答え合わせ担当。集めた証拠だけで、期日までに的中したか判定せよ。\n"
        "厳守: 証拠が無い/不十分なら必ず unclear。憶測で hit/miss を出すな。"
        "miss は『起きなかったと確信できる時』だけ(単に証拠が見つからないだけなら unclear)。\n\n"
        "予測(claim): " + str(h.get("claim", "")) + "\n"
        "的中条件(decider): " + str(res.get("decider", "")) + "\n"
        "主体: " + str(h.get("subject", "")) + " / 作成日: " + str(h.get("created_at", ""))[:10]
        + " / 期限: " + str(h.get("deadline", "")) + "\n\n"
        "集めた証拠:\n" + evidence + "\n\n"
        '次のJSONだけ返せ: {"result":"hit|miss|unclear",'
        '"evidence":{"summary":"判定根拠を一文・日本語","url":"最も根拠になるURL(なければ空文字)"},'
        '"confidence":0〜1の数値}')
    for _ in range(2):
        try:
            d = parse_json(client.generate(prompt))
            if isinstance(d, dict) and d.get("result") in RESULT_SET:
                return d
        except Exception as e:
            print("judge", repr(e)[:120])
    return None


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
        fake = (fake_map or {}).get(h.get("id")) if fake_map is not None else None
        evidence = "(fake)" if fake is not None else gather_evidence(h)
        verd = judge(client, h, evidence, fake=fake)
        if not verd:
            # 判定できず → 保留(要確認)。既にreviewなら再書き込みしない(冪等)
            if not h.get("needs_review"):
                h["needs_review"] = True
                changed = True
            continue
        result = verd.get("result")
        try:
            conf = float(verd.get("confidence"))
        except Exception:
            conf = 0.0
        ev = verd.get("evidence") if isinstance(verd.get("evidence"), dict) else {}
        ev = {"summary": str(ev.get("summary", ""))[:400], "url": str(ev.get("url", ""))[:400]}

        auto = (result == "hit" and conf >= CONF_HIT) or (result == "miss" and conf >= CONF_MISS)
        if auto:
            h["result"] = result
            h["evidence"] = ev
            h["resolved_at"] = now_iso
            h["status"] = "resolved"
            h["needs_review"] = False
            changed = True
            print("resolver:", h.get("id"), "→", result, "(conf %.2f)" % conf)
        else:
            # 曖昧/確度不足 → 保留(要確認)。証拠メモは残す。偽×は出さない。
            if not h.get("needs_review") or (h.get("evidence") or {}) != ev:
                h["needs_review"] = True
                h["evidence"] = ev  # 人が確認するための手掛かり
                changed = True
            print("resolver:", h.get("id"), "→ 保留(要確認) result=%s conf=%.2f" % (result, conf))

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
