"""決着表示・指標表示の見た目を「今」確認するためのプレビュー生成(オフライン)。

なぜ要るか: 最初の期日は 8/5 で、それまで ○× / 的中率 / Brier / シグナル点灯の表示は
本番で一度も動かない。壊れていても当日まで分からない、を無くす。

安全性(ここが要点):
- **合成データ**を一時ディレクトリに作り、そこへ chdir して**本物の描画関数**
  (recorder.render_pages / panels)を動かす。
- 生成物は `docs/preview/` にのみ複製する。**`data/` と本番の docs 直下には一切書かない**。
- ネットワーク・APIキーは不要。実行しても課金は発生しない。
- 全ページ冒頭に「デモデータ」バナーを入れ、実績と誤認させない。

使い方: python preview.py
"""
import os, json, shutil, tempfile
from datetime import datetime, timedelta

import recorder
import memory
import panels
from common import PAGE_CSS

JST = recorder.JST
OUT_DIR = os.path.join("docs", "preview")
BANNER = ('<div style="background:linear-gradient(180deg,#7a5a1a,#5a3f10);color:#ffe9bd;'
          'border:1px solid #a67c2a;border-radius:12px;padding:12px 14px;margin:0 0 22px;'
          'font-size:14px;line-height:1.7">⚠ <b>これはデモデータです</b>。'
          '表示の確認用に作った架空の予測であり、参謀の実績ではありません。'
          '<a href="../index.html" style="color:#ffd88a">→ 本物のブリーフィング</a></div>')


def _hunch(hid, claim, days, conf, status="pending", result=None, subject="OpenAI",
           evidence=None, indicators=None, signals=None, needs_review=False):
    now = datetime.now(JST)
    return {
        "id": hid, "created_at": (now - timedelta(days=20)).isoformat(),
        "based_on": ["demo-r01"], "prose": claim + "と見る。", "claim": claim, "subject": subject,
        "resolution": {"source": "公式ブログ", "check_query": "demo query",
                       "decider": "公式が当該事実を告知したか"},
        "deadline": (now + timedelta(days=days)).strftime("%Y-%m-%d"),
        "confidence": conf, "counter": "前提が崩れる最大の理由はこれだ(デモ)",
        "indicators": indicators or [], "signals": signals or [],
        "status": status, "resolved_at": (now.isoformat() if result else None),
        "result": result, "evidence": evidence, "needs_review": needs_review,
        "rejected": [], "model": "demo", "schema_version": "1", "generator_ver": "v1"}


def demo_ledger():
    """○×が付いた後の世界を再現する合成台帳。確度と結果の組み合わせを意図的にばらけさせ、
    的中率と Brier が別々の意味を持つことが見えるようにする。"""
    ev = {"summary": "公式ブログが当該事実を告知した", "url": "https://example.com/demo"}
    hunches = [
        _hunch("demo-h01", "自信を持って当てた予測(確度0.85→的中)", -12, 0.85, "resolved", "hit", evidence=ev),
        _hunch("demo-h02", "自信満々で外した予測(確度0.80→外し)", -10, 0.80, "resolved", "miss",
               subject="Anthropic", evidence={"summary": "期日までに告知はなかった", "url": "https://example.com/x"}),
        _hunch("demo-h03", "低い確度で当てた予測(確度0.55→的中)", -8, 0.55, "resolved", "hit",
               subject="Google", evidence=ev),
        _hunch("demo-h04", "証拠が足りず判定不能のまま保留", -6, 0.60, "pending", None,
               subject="Meta", needs_review=True,
               evidence={"summary": "公開情報では確認できなかった", "url": ""}),
        _hunch("demo-h05", "確認シグナルが点灯して確度が上がった判定待ちの予測", 9, 0.62, "pending", None,
               subject="Anthropic",
               indicators=[{"sign": "料金ページに新モデルが載る", "dir": "confirm"},
                           {"sign": "公式が延期を告知する", "dir": "kill"}],
               # 逐次ベイズ更新の跡: 点灯で 0.45 → 0.62 へ動いた(従来は表示するだけで動かなかった)
               signals=[{"date": datetime.now(JST).strftime("%Y-%m-%d"),
                         "sign": "料金ページに新モデルが載る", "dir": "confirm",
                         "why": "OpenRouterの一覧に該当モデルが追加された",
                         "conf_before": 0.45,
                         "conf_after": memory.update_confidence(0.45, "confirm")}]),
        _hunch("demo-h06", "死亡シグナルが点灯し要確認になった予測", 14, 0.30, "pending", None,
               subject="xAI", needs_review=True,
               indicators=[{"sign": "当該部門の求人が取り下げられる", "dir": "kill"}],
               signals=[{"date": datetime.now(JST).strftime("%Y-%m-%d"),
                         "sign": "当該部門の求人が取り下げられる", "dir": "kill",
                         "why": "公開求人ボードから該当職種が消えた"}]),
        _hunch("demo-h07", "まだ何も起きていない判定待ちの予測", 21, 0.65, "pending", None, subject="Mistral",
               indicators=[{"sign": "SDKに新フラグが追加される", "dir": "confirm"}]),
    ]
    now = datetime.now(JST)
    # 最後の1件は3日前と同じURL(=同じ記事の再掲)。台帳が重複を1件にまとめることを見せる。
    records = [{"id": "demo-r01", "created_at": (now - timedelta(days=d)).isoformat(),
                "headline": h, "what_happened": "デモ用の記録です。", "background": "デモ",
                "changed": "デモ", "certainty": "reported", "source_tier": "secondary",
                "source": {"url": u, "title": t, "hn_score": 100},
                "body_fetched": True, "model": "demo", "related_ids": []}
               for d, h, t, u in [
                   (3, "OpenAIが新機能を発表", "OpenAI ships feature", "https://example.com/a"),
                   (2, "OpenAIが価格を改定", "OpenAI changes pricing", "https://example.com/b"),
                   (1, "Anthropicが提携を発表", "Anthropic partners", "https://example.com/c"),
                   (0, "OpenAIが新機能を発表(再掲)", "OpenAI ships feature", "https://example.com/a")]]
    for i, r in enumerate(records):
        r["id"] = "demo-r%02d" % (i + 1)
    return records, hunches


def b1_demo():
    """B1(生存者バイアス)の before/after を**実際に resolver を動かして**測る。

    同じ予測・同じ現実(=同じ判定結果)を、旧挙動(kill点灯で needs_review)と
    新挙動(alert のみ)の2通りで通し、打率と Brier がどう変わるかを出す。
    旧挙動では「外れそうだと自分で気づいた予測」が採点から消え、打率が実際より良く見えた。
    戻り: (旧, 新) それぞれ {"rate","brier","n","dropped","detail"}。
    LLM・ネットワークは使わない(resolver の fake seam を使う)。
    """
    import resolver
    now = datetime.now(JST)
    past = (now - timedelta(days=resolver.STALE_DAYS + 2)).strftime("%Y-%m-%d")  # 期日+STALE超

    def make(old_style):
        # 2件とも期日到来。h-a は当たり、h-b は外れ(死亡シグナルが点灯していた)
        a = _hunch("b1-a", "当たった予測", -20, 0.80, subject="OpenAI")
        b = _hunch("b1-b", "外れた予測(死亡シグナルが点灯していた)", -20, 0.75, subject="Anthropic")
        for h in (a, b):
            h["deadline"] = past
        b["signals"] = [{"date": past, "sign": "公式が延期を告知", "dir": "kill",
                         "why": "公式ブログが延期を明記した"}]
        if old_style:
            b["needs_review"] = True      # 旧: watchdog が needs_review を立てていた
        else:
            b["alert"] = True             # 新: 表示専用フラグ。判定は止めない
        return [a, b]

    fake = {"b1-a": {"result": "hit", "confidence": 0.9,
                     "evidence": {"summary": "公式が告知した", "url": "https://example.com/a"}},
            "b1-b": {"result": "miss", "confidence": 0.9,
                     "evidence": {"summary": "期日までに起きなかった", "url": "https://example.com/b"}}}

    out = []
    cwd = os.getcwd()
    for old_style in (True, False):
        tmp = tempfile.mkdtemp(prefix="sanbo-b1-")
        try:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            os.chdir(tmp)
            recorder.dump_json(recorder.HUNCHES_PATH, make(old_style))
            recorder.dump_json(recorder.RECORDS_PATH, [])
            fp = os.path.join(tmp, "fake.json")
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(fake, f)
            os.environ["RESOLVER_FAKE_RESPONSE"] = fp
            resolver.main()
            hs = recorder.load_json_array(recorder.HUNCHES_PATH)
            st, br = memory.hit_stats(hs), memory.brier(hs)
            out.append({
                "rate": (round(st["rate"] * 100) if st["total"] else None),
                "brier": br["score"], "n": br["n"],
                "dropped": [h["id"] for h in hs if h.get("result") not in ("hit", "miss")],
                "detail": [(h["id"], h.get("status"), h.get("result")) for h in hs]})
        finally:
            os.environ.pop("RESOLVER_FAKE_RESPONSE", None)
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)
    return out[0], out[1]


class _QuotaClient:
    """API が 429 で落ち、実Web確認(グラウンディング)に届かない状態のダミー。
    2026-08-05 に実際に起きた障害の再現。"""
    last_grounding_urls = None

    def generate_grounded(self, prompt):
        raise RuntimeError("HTTPError 429: Too Many Requests")

    def generate(self, prompt, response_schema=None, model=None):
        return ('{"result":"unclear","evidence":{"summary":"証拠情報を取得できなかったため",'
                '"url":""},"confidence":0.0}')


class _RecoveredClient:
    """翌日、APIが復旧して実Web確認ができた状態のダミー。"""
    last_grounding_urls = [{"title": "公式ブログ", "url": "https://example.com/official"}]

    def generate_grounded(self, prompt):
        return ('検索の結果、公式ブログで一般提供が告知されていた。'
                '{"result":"hit","evidence":{"summary":"公式が一般提供を告知","url":""},"confidence":0.9}')


def a1_demo():
    """A1(決着を落とさない)の before/after を**実際に resolver を動かして**測る。

    1日目に API が 429 で落ち、2日目には復旧する、という同じ現実を2通りで通す。
    旧挙動: 1日目の失敗で needs_review が立ち、以後 resolver は二度と判定せず、
            期日+STALE_DAYS で unscorable=**永久に採点から消える**。
    新挙動: 1日目は「やり直す」と記録するだけ。2日目に判定され、正しく採点される。
    2026-08-05、参謀の初の答え合わせ2件はこれで失われた。
    戻り: (旧, 新) それぞれ {"n","rate","status","result","note"}。
    """
    import resolver
    now = datetime.now(JST)
    cwd, out = os.getcwd(), []

    def run(old_style):
        tmp = tempfile.mkdtemp(prefix="sanbo-a1-")
        real_client = resolver.GeminiClient
        try:
            os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
            os.chdir(tmp)
            h = _hunch("a1-x", "APIが詰まった日に期日を迎えた予測", -20, 0.70)
            # 期日は STALE_DAYS を超えて過去(=旧挙動なら終端まで進む)
            h["deadline"] = (now - timedelta(days=resolver.STALE_DAYS + 2)).strftime("%Y-%m-%d")
            recorder.dump_json(recorder.HUNCHES_PATH, [h])
            recorder.dump_json(recorder.RECORDS_PATH, [])
            os.environ["GEMINI_API_KEY"] = "demo"

            if old_style:
                # 旧: 429 の unclear で needs_review が立っていた状態を作る
                h["needs_review"] = True
                h["evidence"] = {"summary": "証拠情報を取得できなかったため", "url": ""}
                recorder.dump_json(recorder.HUNCHES_PATH, [h])
                resolver.GeminiClient = lambda *a, **k: _RecoveredClient()
                resolver.main()          # 翌日=復旧しても、needs_review なので判定されない
                note = "APIが復旧しても二度と判定されず、期日+%d日で採点対象外に" % resolver.STALE_DAYS
            else:
                resolver.GeminiClient = lambda *a, **k: _QuotaClient()
                resolver.main()          # 1日目: 429 → やり直す(needs_review を立てない)
                resolver.GeminiClient = lambda *a, **k: _RecoveredClient()
                resolver.main()          # 2日目: 復旧 → 通常どおり判定
                note = "1日目は「やり直す」と記録するだけ。2日目に判定され採点される"

            hs = recorder.load_json_array(recorder.HUNCHES_PATH)
            st = memory.hit_stats(hs)
            g = hs[0]
            return {"n": st["total"], "rate": (round(st["rate"] * 100) if st["total"] else None),
                    "status": g.get("status"), "result": g.get("result"),
                    "attempts": g.get("judge_attempts"), "note": note}
        finally:
            resolver.GeminiClient = real_client
            os.environ.pop("GEMINI_API_KEY", None)
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)

    for old_style in (True, False):
        out.append(run(old_style))
    return out[0], out[1]


def a1_html(old, new):
    def cell(x):
        return ("採点された予測 <b>" + str(x["n"]) + "件</b>"
                + " / 的中率 " + (str(x["rate"]) + "%" if x["rate"] is not None else "—")
                + ' <span class="kv">(status=' + str(x["status"])
                + " result=" + str(x["result"]) + ")</span>")
    return ('<section class="reveal"><h2>A1: 決着を落とさない（実証）</h2>'
            '<p class="lead">2026-08-05、参謀の<b>はじめての答え合わせ2件</b>が'
            'API の 429（一時的な過負荷）で失われた。世界が曖昧だったのではなく、'
            'こちらが見に行けなかっただけなのに、恒久的な判定として固まっていた。<br>'
            '下は「1日目にAPIが詰まり、2日目には復旧する」という同じ現実を、修正前後で通した結果。</p>'
            '<div class="q"><div class="qt">修正前（一時障害でも needs_review を立てていた）</div>'
            '<p class="kv">' + cell(old) + '</p>'
            '<p class="kv">→ ' + old["note"] + '。'
            '<b>較正も Brier も、決着が1件も確定しなければ永久に始まらない。</b></p></div>'
            '<div class="q"><div class="qt">修正後（実Web確認に届かなかった日はやり直す）</div>'
            '<p class="kv">' + cell(new) + '</p>'
            '<p class="kv">→ ' + new["note"] + '（やり直し回数 ' + str(new["attempts"] or 0)
            + '回・上限' + str(__import__("resolver").MAX_ATTEMPTS) + '回）。'
            '<b>決着が成績に載る。</b></p></div></section>')


def b1_html(old, new):
    def cell(x):
        return ("的中率 <b>" + (str(x["rate"]) + "%" if x["rate"] is not None else "—") + "</b>"
                + " / Brier " + (("%.3f" % x["brier"]) if x["brier"] is not None else "—")
                + " <span class=\"kv\">(採点された予測 " + str(x["n"]) + "件"
                + (("・採点から消えた: " + "、".join(x["dropped"])) if x["dropped"] else "")
                + ")</span>")
    rows = "".join("<li>%s → status=%s result=%s</li>" % t for t in new["detail"])
    return ('<section class="reveal"><h2>B1: 生存者バイアスの修正（実証）</h2>'
            '<p class="lead">同じ予測・同じ現実（当たり1件／外れ1件。外れの方は事前に'
            '「死亡シグナル」が点灯していた）を、修正前と修正後の両方で実際に答え合わせした結果。</p>'
            '<div class="q"><div class="qt">修正前（kill点灯で needs_review を立てていた）</div>'
            '<p class="kv">' + cell(old) + '</p>'
            '<p class="kv">→ resolver が「判定済み」とみなして二度と採点せず、期日+' +
            str(__import__("resolver").STALE_DAYS) + '日で採点対象外に。'
            '<b>外れそうだと自分で気づいた予測ほど成績から消え、打率が実際より良く見えていた。</b></p></div>'
            '<div class="q"><div class="qt">修正後（表示用の alert のみ。判定は止めない）</div>'
            '<p class="kv">' + cell(new) + '</p>'
            '<p class="kv">→ 死亡シグナルが点灯していても期日に通常どおり採点され、'
            '<b>× が正しく成績に載る</b>。<ul class="kv">' + rows + '</ul></p></div></section>')


def _finish_page(path):
    """生成済みHTMLに (1)デモバナーを差し込み (2)プレビュー内に存在しないページへの
    相対リンクを1階層上(本物)へ向け直す。preview/ 配下でリンク切れを出さないため。"""
    try:
        with open(path, encoding="utf-8") as f:
            s = f.read()
        if "<main>" in s and "これはデモデータです" not in s:
            s = s.replace("<main>", "<main>\n" + BANNER, 1)
        for name in ("ichite.html",):   # preview/ には複製しないページ
            s = s.replace('href="' + name + '"', 'href="../' + name + '"')
        with open(path, "w", encoding="utf-8") as f:
            f.write(s)
    except Exception as e:
        print("finish_page", path, repr(e)[:100])


def preview_index(records, hunches, b1=None, a1=None):
    """index の決着まわり(追跡中の予測 / 答え合わせ)だけを、本番と同じ panels で描く。
    a1/b1 が渡されれば、決着を落とさない修正・生存者バイアス修正の実証も載せる。"""
    today = datetime.now(JST).date()
    body = (panels.tracking_html(hunches, today) + panels.answers_html(hunches, today, memory))
    if b1:
        body = b1_html(*b1) + body
    if a1:
        body = a1_html(*a1) + body
    return ("""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><title>プレビュー — 決着表示の確認</title>
<style>""" + PAGE_CSS + """</style></head><body><main>
""" + BANNER + """
<header class="hd"><h1>◇ 決着表示プレビュー</h1>
<div class="d">8/5の初決着を待たずに、○×・的中率・Brier・シグナル点灯の見え方を確認する</div>
<nav class="nav"><a href="hunches.html">勘の台帳(デモ)</a><a href="records.html">記録(デモ)</a>
<a href="../index.html">← 本物のブリーフィング</a></nav></header>
""" + body + """
</main></body></html>""")


def main():
    records, hunches = demo_ledger()
    b1 = b1_demo()          # 実際に resolver を動かして before/after を測る
    print("B1実証: 修正前 的中率%s%% (採点%d件・消失%s) / 修正後 的中率%s%% (採点%d件)" % (
        b1[0]["rate"], b1[0]["n"], b1[0]["dropped"] or "なし", b1[1]["rate"], b1[1]["n"]))
    a1 = a1_demo()
    print("A1実証: 修正前 採点%d件 status=%s / 修正後 採点%d件 status=%s result=%s" % (
        a1[0]["n"], a1[0]["status"], a1[1]["n"], a1[1]["status"], a1[1]["result"]))
    out = os.path.abspath(OUT_DIR)
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="sanbo-preview-")
    try:
        # 一時ディレクトリで **本物の描画関数** を動かす(本番の data/ docs/ には触れない)
        os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
        os.chdir(tmp)
        recorder.dump_json(recorder.RECORDS_PATH, records)
        recorder.dump_json(recorder.HUNCHES_PATH, hunches)
        recorder.render_pages()
        with open(os.path.join("docs", "index.html"), "w", encoding="utf-8") as f:
            f.write(preview_index(records, hunches, b1, a1))
        os.chdir(cwd)
        os.makedirs(out, exist_ok=True)
        for name in ("index.html", "hunches.html", "records.html", "threads.html"):
            src = os.path.join(tmp, "docs", name)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(out, name))
                _finish_page(os.path.join(out, name))
        st = memory.hit_stats(hunches)
        br = memory.brier(hunches)
        print("preview: %s に生成 / 的中率 %s / Brier %s (n=%d)" % (
            OUT_DIR, (str(round(st["rate"] * 100)) + "%") if st["total"] else "—",
            ("%.3f" % br["score"]) if br["score"] is not None else "—", br["n"]))
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
