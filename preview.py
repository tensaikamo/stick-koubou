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
import os, shutil, tempfile
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
        _hunch("demo-h05", "確認シグナルが点灯した判定待ちの予測", 9, 0.45, "pending", None,
               subject="Anthropic",
               indicators=[{"sign": "料金ページに新モデルが載る", "dir": "confirm"},
                           {"sign": "公式が延期を告知する", "dir": "kill"}],
               signals=[{"date": datetime.now(JST).strftime("%Y-%m-%d"),
                         "sign": "料金ページに新モデルが載る", "dir": "confirm",
                         "why": "OpenRouterの一覧に該当モデルが追加された"}]),
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
    records = [{"id": "demo-r01", "created_at": (now - timedelta(days=d)).isoformat(),
                "headline": h, "what_happened": "デモ用の記録です。", "background": "デモ",
                "changed": "デモ", "certainty": "reported", "source_tier": "secondary",
                "source": {"url": "https://example.com/demo", "title": t, "hn_score": 100},
                "body_fetched": True, "model": "demo", "related_ids": []}
               for d, h, t in [(3, "OpenAIが新機能を発表", "OpenAI ships feature"),
                               (2, "OpenAIが価格を改定", "OpenAI changes pricing"),
                               (1, "Anthropicが提携を発表", "Anthropic partners")]]
    for i, r in enumerate(records):
        r["id"] = "demo-r%02d" % (i + 1)
    return records, hunches


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


def preview_index(records, hunches):
    """index の決着まわり(追跡中の予測 / 答え合わせ)だけを、本番と同じ panels で描く。"""
    today = datetime.now(JST).date()
    body = (panels.tracking_html(hunches, today) + panels.answers_html(hunches, today, memory))
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
            f.write(preview_index(records, hunches))
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
