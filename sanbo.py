import os, re, json, html
from datetime import datetime, timezone, timedelta
from common import GeminiClient, fetch_hn, fetch_tc, parse_json, PAGE_CSS
import memory

API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not API_KEY:
    raise SystemExit("GEMINI_API_KEY が未設定です(リポジトリのSecretsを確認)")

# モデルフォールバック・過負荷再試行・回数ガードは common.GeminiClient が担う。
# 3段(選別/メモ/執筆)×過負荷リトライを許容できる上限に(無料枠1,500/日の保護内)。
_client = GeminiClient(API_KEY, call_limit=16)


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

【逆読み厳禁・誰を利するか点検】ある変化について「個人が先回りできる」と書く前に、その変化が誰を利するかを必ず確認しろ。資本・既存プレイヤー・プラットフォーム側を利する変化を、個人の先回り機会として描くのは逆読みだ。例:ChatGPTに広告枠ができる=推薦が「金で買える場所」になり、無料のオーガニックな差し込み余地はむしろ縮む(SEOにAdWordsが来たのと同じ)。「出稿画面を見ろ」は金を払う側=資本の土俵の話。個人の隙はむしろ「その有料化で締め出される層がどこへ逃げるか」の側にある。結論が「巨大資本から横取りする個人」の趣旨と矛盾していないか、書き終わりに自己点検しろ。

【危険語禁止】「確実」「確実な」「間違いなく」等の、根拠を伴わない断定安全語を使うな。断言はしてよいが必ず一言の根拠を添えろ。根拠が弱いなら弱いと認めて確度を下げろ(見栄で「確実」と塗るな)。

【反ピボット・着地優先】読者が詰まっているのは着想不足ではなく着地不足だ。毎朝「今すぐ新しいことを始めろ」と別のネタへ乗り換えさせるのは、弱点を増幅する。「で、どうする」は、既に動いている流れに乗る/仕込む/続きを追う具体を優先しろ。過去の【参謀の記憶】がある場合は、それを踏まえて同じ賭けを継続・更新しろ(毎朝ピボットを作るな)。
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
    _recs, _huns = memory.load_ledger()
    _digest = memory.build_digest(_recs, _huns, compact=True)  # 過去の自分の読み・結果・スレッド
    if _digest:
        material = _digest + "\n\n" + material
    prompt3 = (PERSONA +
        "\n以下の材料から今朝のブリーフィングを執筆し、次のJSONオブジェクトだけを返せ(配列で包まない):\n"
        '{"kuki": {"omote": "表:何が起きたか。2〜3文", '
        '"ura": "裏:それが本当に意味すること・裏で誰が何を狙っているかの見立て。2〜3文"}, '
        '"dousuru": "読者個人への具体的な示唆のみ。日本企業・業界への提言は禁止。「明日これを見ておけ」レベルまで具体化。2〜4文", '
        '"kan": "参謀の勘:確証はないが匂う話を1つ。第三者が公開情報で後から○×を付けられる、期限つき予測の形で書く(例:2週間以内に◯◯が公式発表する と見る。根拠は◯◯)。非公開・秘密・リーク前提の当てられない予測は書くな", '
        '"mijoriku": [{"title": "記事タイトル(日本語訳可)", "desc": "一言説明", "why": "なぜ日本で先回りの価値があるか"}]}\n'
        "mijorikuは材料の中から日本語圏でまだほぼ話題になっていなさそうな話を1〜2本選ぶこと。弱い根拠を『確実』で塗るな。\n"
        "【記憶がある場合】上の【参謀の記憶】を踏まえ、omote/ura/kan は過去の自分の読みと結果に触れて自己更新せよ"
        "(例:『前に◯◯と読んだが△△で外した/当たった。今回はこう修正する』)。継続スレッドは乗り換えず続きとして書け。\n"
        "専門用語には <t data-d='この文脈での一言解説'>用語</t> の形式で解説を埋め込め"
        "(1セクションあたり2〜4語まで。data-dは必ずシングルクォートで書き、中に引用符・山括弧・改行を入れない。"
        "JSON文字列を壊す二重引用符は文中で使わない。t以外のタグは使わない。"
        "語を分割・重複させてタグ付けするな——直前に同じ字を残す『独<t>独占</t>』のような重複を作らず、タグは語全体に付けろ)。\n\n" + material)
    for attempt in range(2):  # 応答形式が不正だった場合は1回だけ再生成を試す
        try:
            final = norm_final(parse_json(gemini(prompt3)))
        except Exception as e:
            print("final", e)
        if final:
            break
        print("final attempt", attempt + 1, "failed,", "retrying" if attempt == 0 else "giving up")

# 執筆(LLM)が成功したか。失敗時は既存の良好なページを保持し上書きしない。
generation_ok = bool(final)

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
    mj_html = '<section class="reveal">' + mj_html + "</section>"

# 答え合わせ節(過去の勘の実績=成長する参謀。dataから作り、執筆LLMの成否と独立)
_ans_recs, _ans_huns = memory.load_ledger()
_ans_st = memory.hit_stats(_ans_huns)

# 追跡中の予測(読む勘=採点される勘の一致): 表示している散文の勘の下に、実際に○×が付く
# 追跡対象の予測を並べる。記録が別ステップのため直近分は前サイクルの可能性あり(予測は数日〜)。
_tracking = [h for h in reversed(_ans_huns) if h.get("status") == "pending"][:3]
track_html = ""
if _tracking:
    _trows = ""
    for _h in _tracking:
        _dl = _h.get("deadline", "")
        _drem = ""
        try:
            _delta = (datetime.strptime(_dl, "%Y-%m-%d").date() - jst.date()).days
            _drem = ("・残り%d日" % _delta) if _delta >= 0 else ("・期限超過%d日" % (-_delta))
        except Exception:
            pass
        _trows += ('<li>' + html.escape((_h.get("claim", "") or "")[:64])
                   + ' <span class="m">期限' + html.escape(_dl) + _drem + '</span></li>')
    track_html = ('<section class="reveal"><h2>追跡中の予測</h2>'
                  '<p class="m">期日が来たら、この予測に○×が付く。参謀の読みのうち、実際に採点される予測がこれだ。</p>'
                  '<ul>' + _trows + '</ul>'
                  '<p class="m"><a href="hunches.html">すべての予測と答え合わせ →</a></p></section>')

ans_html = ""
if _ans_huns:
    if _ans_st["total"]:
        _head = ('<span class="hitrate">的中率 ' + str(round(_ans_st["rate"] * 100)) + '%</span> '
                 '<span class="m">(的中' + str(_ans_st["hit"]) + '/外し' + str(_ans_st["miss"])
                 + '・判定待ち' + str(_ans_st["pending"]) + ')</span>')
    else:
        _head = '<span class="m">まだ答え合わせ前。判定待ち ' + str(_ans_st["pending"]) + ' 件(期日が来たら○×が付く)</span>'
    _decided = [h for h in reversed(_ans_huns) if h.get("status") == "resolved"][:3]
    _rows = "".join('<li>' + {"hit": "○", "miss": "×"}.get(h.get("result"), "—") + " "
                    + html.escape((h.get("claim", "") or "")[:48]) + "</li>" for h in _decided)
    ans_html = ('<section class="reveal"><h2>答え合わせ</h2><p>' + _head + "</p>"
                + ("<ul>" + _rows + "</ul>" if _rows else "")
                + '<p class="m"><a href="hunches.html">勘の台帳(全予測と○×)→</a></p></section>')

page = """<!DOCTYPE html><html lang="ja" class="no-js"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>シリコンバレー参謀</title><style>""" + PAGE_CSS + """</style></head><body>
<div class="progress" aria-hidden="true"></div>
<main>
<header class="hd"><h1>◇ シリコンバレー参謀</h1>
<div class="d">""" + jst.strftime("%Y.%m.%d %H:%M") + """ JST</div>
<nav class="nav"><a class="refresh" href="https://github.com/tensaikamo/stick-koubou/actions/workflows/sanbo.yml" target="_blank" rel="noopener">⟳ 参謀に調べ直させる</a><a href="records.html">記録の台帳</a><a href="hunches.html">勘の台帳</a></nav>
<div class="hint">「調べ直させる」→ GitHubで Run workflow を1タップ。数分で参謀が記憶を踏まえて考え直す。反映後にこのページを再読み込み。</div></header>
<section class="reveal"><h2>今日の空気</h2>
<p><span class="lb">表</span>""" + render_rich(final["omote"]) + """</p>
<p><span class="lb">裏</span>""" + render_rich(final["ura"]) + """</p></section>
<section class="reveal"><h2>で、どうする</h2><p>""" + render_rich(final["dousuru"]) + """</p></section>
<section class="reveal"><h2>参謀の勘</h2><p>""" + render_rich(final["kan"]) + """</p></section>
""" + track_html + """
""" + ans_html + """
""" + mj_html + """
<section class="reveal"><h2>今日の重要記事</h2><ul>""" + links + """</ul></section>
</main>
<script>
(function(){
  var root=document.documentElement;
  root.className="js";
  var reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;

  // セクションのスクロール・イン(iOS Safari含め実動。JS無効時は全表示のまま)
  var rev=document.querySelectorAll(".reveal");
  if(!reduce && "IntersectionObserver" in window){
    var io=new IntersectionObserver(function(es){
      es.forEach(function(en){ if(en.isIntersecting){ en.target.classList.add("in"); io.unobserve(en.target); } });
    },{threshold:0.12,rootMargin:"0px 0px -8% 0px"});
    rev.forEach(function(el){ io.observe(el); });
  } else { rev.forEach(function(el){ el.classList.add("in"); }); }

  // 用語タップ解説: Popover API(iOS 17+)、未対応は同等のフォールバック配置
  var canPop = ("popover" in HTMLElement.prototype);
  var tip=null, anchor=null;
  function place(t,el){
    var r=el.getBoundingClientRect();
    var w=Math.min(t.offsetWidth, window.innerWidth-24);
    var left=Math.min(Math.max(12, r.left), window.innerWidth-12-w);
    t.style.left=left+"px"; t.style.top=(r.bottom+8)+"px";
  }
  function close(){
    if(!tip) return;
    var t=tip; tip=null; anchor=null;
    if(canPop){ try{ t.hidePopover(); }catch(e){} }
    t.remove();
  }
  function open(el){
    close();
    tip=document.createElement("div"); tip.className="tip";
    tip.textContent=el.getAttribute("data-d")||"";
    if(canPop) tip.setAttribute("popover","manual");
    document.body.appendChild(tip);
    if(canPop){ try{ tip.showPopover(); }catch(e){} }
    place(tip,el);
    if(!canPop) requestAnimationFrame(function(){ tip.classList.add("show"); });
    anchor=el;
  }
  function vt(fn){ if(!reduce && document.startViewTransition){ document.startViewTransition(fn); } else fn(); }
  document.addEventListener("click", function(e){
    var el=e.target.closest?e.target.closest("t"):null;
    if(el){ e.preventDefault(); if(anchor===el){ vt(close); } else { vt(function(){ open(el); }); } }
    else if(tip && !(e.target.closest&&e.target.closest(".tip"))){ vt(close); }
  });
  window.addEventListener("scroll", function(){ if(tip) close(); }, {passive:true});
})();
</script>
</body></html>"""

os.makedirs("docs", exist_ok=True)
# 生成成功時のみ上書き。失敗時に既存の良好なページがあれば保持し、
# 「本日の生成に失敗」を公開してしまう事故を防ぐ(初回や既存なし時のみ劣化版を書く)。
if generation_ok or not os.path.exists("docs/index.html"):
    open("docs/index.html", "w", encoding="utf-8").write(page)
    print("done", len(picked), "api_calls", _client.calls, "generation_ok", generation_ok)
else:
    print("生成失敗のため既存ページを保持(上書きせず)。api_calls", _client.calls)
