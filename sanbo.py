import os, re, json, html
from datetime import datetime, timezone, timedelta
from common import GeminiClient, fetch_hn, fetch_tc, parse_json

API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not API_KEY:
    raise SystemExit("GEMINI_API_KEY が未設定です(リポジトリのSecretsを確認)")

# モデルフォールバック・過負荷再試行・回数ガードは common.GeminiClient が担う。
# 1実行あたりのAPI呼び出し上限10回(無料枠1,500/日の保護)。
_client = GeminiClient(API_KEY, call_limit=10)


def gemini(prompt):
    return _client.generate(prompt)


PERSONA = """読者はただ一人。以下の人物だけに向けて書け。
- 日本在住。昼は現場仕事、動ける時間は朝と夜、装備はiPhone一台
- 組織に属さず、個人で情報優位を作って先回りすることを狙う
- AIを消費する側ではなく、AIで仕掛ける側に回ろうとしている
- 知りたいのは「何が起きたか」ではなく「裏で何が動いてるか」「日本にまだ届いてない何か」「自分が先回りできる隙はどこか」
- 嫌うもの:企業向け提言、一般論、「注視が必要です」で終わる文
- 好むもの:断言と根拠、期限つきの予測、誰も言ってない視点

参謀の文体:断言型。「〜と見る」「〜のはずだ」と言い切る。ヘッジ表現(「可能性があります」「かもしれません」の乱用)は禁止。ただし断言には必ず根拠を一言つける。

注意:読者の「現場仕事」は生活制約(動ける時間が朝晩のみ・装備はiPhone一台)を示す情報であり、興味領域ではない。現場作業・建設・ブルーカラー向けAIといった職業連想で記事を選んだり話題を寄せたりするな。関心はあくまでシリコンバレーAI業界の権力・金・技術の動きと、そこで個人が先回りできる隙だ。
"""

def unwrap_list(v):
    # {"selected": [..]} のようにオブジェクトで包まれた配列も許容する
    if isinstance(v, dict):
        v = next((x for x in v.values() if isinstance(x, list)), None)
    return v if isinstance(v, list) else None

# 正しい形の用語タグ <t data-d='解説'>用語</t> だけを通すパターン。
# 属性はシングルクォート指定(JSON文字列内の二重引用符エスケープ漏れでJSON全体が
# 壊れる事故が実際に起きたため)だが、二重引用符も後方互換で受ける
T_RE = re.compile(r"""<t\s+data-d=(?:'([^'<>]{1,160})'|"([^"<>]{1,160})")\s*>([^<>]{1,60})</t>""")

def render_rich(text):
    # モデル出力をHTML化する唯一の経路。正規形のtタグのみ再構築し、
    # それ以外(他のタグ・壊れたtタグ)はすべてエスケープしてページ破壊を防ぐ
    text = str(text)
    out, pos = [], 0
    for m in T_RE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append('<t data-d="' + html.escape(m.group(1) or m.group(2), quote=True) + '">'
                   + html.escape(m.group(3)) + "</t>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)

items = fetch_hn() + fetch_tc()
seen, arts = set(), []
for a in items:
    k = a["title"].lower().strip()
    if k and k not in seen:
        seen.add(k); arts.append(a)
arts = arts[:40]

lst = "\n".join(str(i) + ". [" + a["src"] + "] " + a["title"] for i, a in enumerate(arts))

# --- 1段目: 選別 ---
sel = None
if arts:
    try:
        sel = unwrap_list(parse_json(gemini(PERSONA +
            "\n以下は過去24時間の英語ヘッドライン。\n"
            "『シリコンバレーのAI業界の空気を掴む』観点で重要な記事を6〜8本選び、番号だけをJSON配列で返せ。\n"
            "選定基準はシリコンバレーAI業界の重要度と先回り価値のみ。読者の職業に寄せない。"
            "AIと無関係な記事(収集ノイズ)は選ばない。\n"
            "説明不要、JSON配列のみ。\n\n" + lst)))
    except Exception as e:
        print("sel", e)
if not isinstance(sel, list):
    print("sel fallback: 応答が配列でないため先頭7件を採用")
    sel = list(range(min(7, len(arts))))
picked = []
for i in sel:
    if isinstance(i, str) and i.strip().isdigit():
        i = int(i)  # 番号を文字列で返すモデルを許容
    if isinstance(i, int) and 0 <= i < len(arts) and arts[i] not in picked:
        picked.append(arts[i])
if not picked:
    print("sel fallback: 有効な番号がないため先頭7件を採用")
    picked = arts[:7]

plist = "\n".join(str(i) + ". [" + a["src"] + "] " + a["title"] + " (" + a["url"] + ")"
                  for i, a in enumerate(picked))

# --- 2段目: 分析メモ ---
memos = None
if picked:
    try:
        m = unwrap_list(parse_json(gemini(PERSONA +
            "\n以下は今日の重要記事。参謀として各記事を分析し、次のJSON配列だけを返せ:\n"
            '[{"i": 記事番号, "omote": "表:何が起きたか(1〜2文)", '
            '"ura": "裏:それが本当に意味すること・裏で誰が何を狙っているかの見立て(1〜2文)", '
            '"nihon": "日本語圏への未到達度(高/中/低)とその理由を一言"}]\n\n' + plist)))
        if m is not None:
            memos = [x for x in m if isinstance(x, dict) and str(x.get("omote") or "").strip()] or None
        if memos is None:
            print("memo unexpected shape:", repr(m)[:200])
    except Exception as e:
        print("memo", e)

# --- 3段目: 執筆 ---
def norm_final(b):
    # 配列ラップを剥がし、4セクションが揃っているかを検証する
    if isinstance(b, list):
        b = next((x for x in b if isinstance(x, dict)), None)
    if not isinstance(b, dict):
        if b is not None:
            print("final unexpected shape:", repr(b)[:200])
        return None
    k = b.get("kuki") if isinstance(b.get("kuki"), dict) else {}
    r = {"omote": str(k.get("omote") or "").strip(), "ura": str(k.get("ura") or "").strip(),
         "dousuru": str(b.get("dousuru") or "").strip(), "kan": str(b.get("kan") or "").strip(),
         "mijoriku": []}
    if not (r["omote"] and r["ura"] and r["dousuru"] and r["kan"]):
        print("final missing sections:", repr(b)[:200])
        return None
    mj = b.get("mijoriku")
    for x in (mj if isinstance(mj, list) else []):
        if isinstance(x, dict) and all(str(x.get(f) or "").strip() for f in ("title", "desc", "why")):
            r["mijoriku"].append({f: str(x[f]) for f in ("title", "desc", "why")})
        if len(r["mijoriku"]) == 2:
            break
    return r

final = None
if picked:
    material = "今日の重要記事:\n" + plist
    if memos:
        material += "\n\n参謀の分析メモ(2段目の下書き。これを材料に磨き上げろ):\n" + json.dumps(memos, ensure_ascii=False)
    prompt3 = (PERSONA +
        "\n以下の材料から今朝のブリーフィングを執筆し、次のJSONオブジェクトだけを返せ(配列で包まない):\n"
        '{"kuki": {"omote": "表:何が起きたか。2〜3文", '
        '"ura": "裏:それが本当に意味すること・裏で誰が何を狙っているかの見立て。2〜3文"}, '
        '"dousuru": "読者個人への具体的な示唆のみ。日本企業・業界への提言は禁止。「明日これを見ておけ」レベルまで具体化。2〜4文", '
        '"kan": "参謀の勘:確証はないが匂う話を1つ。必ず期限つき予測の形で書く(例:2週間以内に◯◯が動くと見る。根拠は◯◯)", '
        '"mijoriku": [{"title": "記事タイトル(日本語訳可)", "desc": "一言説明", "why": "なぜ日本で先回りの価値があるか"}]}\n'
        "mijorikuは材料の中から日本語圏でまだほぼ話題になっていなさそうな話を1〜2本選ぶこと。\n"
        "専門用語には <t data-d='この文脈での一言解説'>用語</t> の形式で解説を埋め込め"
        "(1セクションあたり2〜4語まで。data-dは必ずシングルクォートで書き、中に引用符・山括弧・改行を入れない。"
        "JSON文字列を壊す二重引用符は文中で使わない。t以外のタグは使わない)。\n\n" + material)
    for attempt in range(2):  # 応答形式が不正だった場合は1回だけ再生成を試す
        try:
            final = norm_final(parse_json(gemini(prompt3)))
        except Exception as e:
            print("final", e)
        if final:
            break
        print("final attempt", attempt + 1, "failed,", "retrying" if attempt == 0 else "giving up")

# フォールバック: 各段が失敗しても前段の結果で劣化版を出す(白紙ページ禁止)
if not final:
    if memos:
        final = {"omote": " / ".join(str(x.get("omote")) for x in memos[:2]),
                 "ura": " / ".join(str(x.get("ura") or "") for x in memos[:2]).strip(" /") or "-",
                 "dousuru": "本日の執筆に失敗したため分析メモの抜粋を表示。次回実行で回復します。",
                 "kan": "-", "mijoriku": []}
    elif picked:
        final = {"omote": "本日の生成に失敗。次回実行で回復します。", "ura": "-",
                 "dousuru": "-", "kan": "-", "mijoriku": []}
    else:
        final = {"omote": "過去24時間で基準を満たす記事を取得できませんでした。取得元の一時的な不調の可能性があります。",
                 "ura": "-",
                 "dousuru": "次回の自動実行での回復を待つ。継続する場合は取得条件の見直しを。",
                 "kan": "-", "mijoriku": []}

jst = datetime.now(timezone(timedelta(hours=9)))
links = "\n".join('<li><a href="' + html.escape(a["url"]) + '">' + html.escape(a["title"])
                  + '</a> <span class="m">' + html.escape(a["src"] + " " + a["meta"]) + "</span></li>" for a in picked)

mj_html = ""
if final["mijoriku"]:
    mj_html = "<h2>未上陸</h2>" + "".join(
        '<div class="mj"><div class="mjt">' + render_rich(x["title"]) + "</div><p>" + render_rich(x["desc"])
        + '</p><p class="mjw">先回りの価値:' + render_rich(x["why"]) + "</p></div>" for x in final["mijoriku"])

page = """<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>シリコンバレー参謀</title><style>
body{background:#0b0f14;color:#dbe4ec;font-family:system-ui,'Hiragino Sans',sans-serif;
margin:0;padding:24px 18px;line-height:1.9}
main{max-width:640px;margin:0 auto}
h1{font-size:15px;letter-spacing:.3em;color:#5fd7c8;font-weight:400}
.d{color:#586a7a;font-size:12px;letter-spacing:.15em;margin-bottom:28px}
h2{font-size:13px;letter-spacing:.25em;color:#d6a24c;border-bottom:1px solid #1a2431;
padding-bottom:8px;margin-top:36px;font-weight:400}
p{font-size:15px}
.lb{color:#5fd7c8;font-size:11px;letter-spacing:.2em;border:1px solid #24414d;border-radius:4px;
padding:1px 8px;margin-right:8px;white-space:nowrap}
.mj{border:1px solid #1a2431;border-radius:8px;padding:12px 14px;margin:14px 0}
.mjt{font-size:14px;color:#dbe4ec}
.mj p{font-size:13px;margin:6px 0}
.mjw{color:#8fb8d8}
t{border-bottom:1px dotted #5fd7c8;cursor:pointer}
.tip{display:block;background:#101a26;border:1px solid #24414d;border-radius:8px;
padding:8px 12px;margin:6px 0;font-size:13px;line-height:1.7;color:#aec4d4;max-width:100%}
ul{padding-left:0;list-style:none}
li{margin:14px 0;font-size:14px}
a{color:#8fb8d8;text-decoration:none}
.m{color:#586a7a;font-size:11px;margin-left:6px}
</style></head><body><main>
<h1>◇ シリコンバレー参謀</h1>
<div class="d">""" + jst.strftime("%Y.%m.%d %H:%M") + """ JST</div>
<h2>今日の空気</h2>
<p><span class="lb">表</span>""" + render_rich(final["omote"]) + """</p>
<p><span class="lb">裏</span>""" + render_rich(final["ura"]) + """</p>
<h2>で、どうする</h2><p>""" + render_rich(final["dousuru"]) + """</p>
<h2>参謀の勘</h2><p>""" + render_rich(final["kan"]) + """</p>
""" + mj_html + """
<h2>今日の重要記事</h2><ul>""" + links + """</ul>
</main>
<script>
document.addEventListener("click", function (e) {
  var t = e.target.closest ? e.target.closest("t") : null;
  var old = document.querySelector(".tip");
  if (old) {
    var same = old.tipSource === t;
    old.remove();
    if (same) return;  // 同じ用語の再タップは閉じるだけ
  }
  if (!t) return;
  var s = document.createElement("span");
  s.className = "tip";
  s.textContent = t.getAttribute("data-d") || "";
  s.tipSource = t;
  t.insertAdjacentElement("afterend", s);
});
</script>
</body></html>"""

os.makedirs("docs", exist_ok=True)
open("docs/index.html", "w", encoding="utf-8").write(page)
print("done", len(picked), "api_calls", _client.calls)
