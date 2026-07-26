import os, re, json, html
from datetime import datetime, timezone, timedelta
from common import (GeminiClient, fetch_all, fetch_jp_hits, fetch_markets, watch_step,
                    median, parse_json, PAGE_CSS)
from recorder import (fetch_body, load_json_array, dump_json, HUNCHES_PATH,
                      render_pages)   # 記録層のテスト済み実装を再利用
import memory
import panels

API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not API_KEY:
    raise SystemExit("GEMINI_API_KEY が未設定です(リポジトリのSecretsを確認)")

# モデルフォールバック・過負荷再試行・回数ガードは common.GeminiClient が担う。
# 3段(選別/メモ/執筆)×過負荷リトライを許容できる上限に(無料枠1,500/日の保護内)。
# 段数が増えた(見張り1+選別1+メモ1+執筆1〜2+赤ペン1+確度3+一手1 ≒ 10)。過負荷リトライの
# 余裕を残して上限を置く。無料枠1,500/日に対し十分小さい(暴走ガードとしては機能する)。
_client = GeminiClient(API_KEY, call_limit=22)


def gemini(prompt, response_schema=None, model=None):
    return _client.generate(prompt, response_schema=response_schema, model=model)


# ブリーフィング執筆の構造化出力スキーマ(responseSchema)。JSON準拠を保証し、
# セクション増加でJSONが肥大してもパース失敗(=古いページ保持)を減らす。OpenAPI 3.0 サブセット。
BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "kuki": {"type": "object",
                 "properties": {"omote": {"type": "string"}, "ura": {"type": "string"}},
                 "required": ["omote", "ura"]},
        "dousuru": {"type": "string"},
        "kan": {"type": "string"},
        "kan_konkyo": {"type": "string"},   # 勘の根拠(任意・鎖を読者に見せる)
        "kan_hantai": {"type": "string"},   # 勘が外れるとすればの反証条件(任意)
        "kan_conf": {"type": "number"},     # 勘の確度0-1(任意・市場価格との乖離を測るため)
        "ura_taikou": {"type": "string"},   # 退けた対抗仮説と、退けた理由(ACH・任意)
        "ippan": {"type": "string"},
        "mijoriku": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "desc": {"type": "string"}, "why": {"type": "string"}},
            "required": ["title", "desc", "why"]}},
    },
    "required": ["kuki", "dousuru", "kan", "ippan"],
}


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

【月並み禁止・非コンセンサス】その説明を既に他の誰かが言っているなら、書くな。「規制は独占を守るため」
「大手が囲い込む」「安全性は建前」「オープンソースが追い上げる」の類は、世界中が言っている定型であり価値はゼロだ。
裏には**この材料を読んだ者にしか出せない具体**を書け——誰が・何を・いつ動かしたか、数字、条件、当事者の実利。
一般論を1つ書く代わりに、外れうる具体を1つ書け。書き終えたら「これは今日この材料を読まなくても書けた文章か?」
と自問し、YESなら書き直せ。

【基準率(base rate)】勘は思いつきの具体から書くな。まず参照クラスの頻度から入れ——「この種の企業が
新サービスを公表する頻度は年に何回か」「この会社が2週間以内に何かを出した回数は」。滅多に起きない事象に
短い期限を付けるのは、外れるための予測だ。基準率で外枠を決め、今日の材料で上下に調整し、その調整理由を
根拠として書け。確度は基準率と根拠の強さで決めろ(願望で上げるな)。

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

# 情報源は3層(T1=一次情報・発表前の兆候 / T2=実勢 / T3=二次)。話題化した記事だけを読む
# 構造から抜けるため、公式ブログ・arXiv・求人・規制・SEC・SDKリリースを直接読む。
arts = fetch_all()
# 時間優位: 昨日との差分(値下げ・新モデル・退役予告)。発表を待たずに変化を掴む最上位の材料。
diffs = watch_step()
if diffs:
    arts = diffs + [a for a in arts if a["title"] not in {d["title"] for d in diffs}]
TIER_JA = {1: "一次", 2: "実勢", 3: "二次"}
lst = "\n".join(str(i) + ". [" + TIER_JA.get(a.get("tier", 3), "二次") + "/" + a["src"] + "] " + a["title"]
                for i, a in enumerate(arts))
diff_block = ("\n\n=== 本日検知した「静かな変化」(公式発表なしに変わったもの。最優先の材料) ===\n"
              + "\n".join("- " + d["title"] for d in diffs)) if diffs else ""

# --- 指標監視(I&W): 判定待ち予測の「生死シグナル」が今日点灯したかを見る ---------
# 期日まで放置せず、毎日「もう死んでいないか」を見る(thesis monitoring)。
# hunch毎ではなく**1回の呼び出し**で全件を突き合わせる(コスト固定)。
# kill が点灯しても自動で×にはしない(needs_review を立てるだけ)=偽×を出さない原則を守る。
watchdog_note = ""
try:
    _huns = load_json_array(HUNCHES_PATH)
    _pend = [h for h in _huns if h.get("status") == "pending" and h.get("indicators")]
    if _pend and (diffs or arts):
        _events = ("\n".join("- " + d["title"] for d in diffs[:8])
                   + "\n" + "\n".join("- " + a["title"] for a in arts[:20]))
        _watch_list = "\n".join(
            "%s / %s: " % (h.get("id"), str(h.get("claim", ""))[:50])
            + " | ".join("[%d]%s(%s)" % (i, x.get("sign", ""), x.get("dir", ""))
                         for i, x in enumerate(h.get("indicators") or []))
            for h in _pend[:12])
        _wd = parse_json(gemini(
            "次は追跡中の予測と、その『生死を示す観測点(指標)』の一覧だ。\n" + _watch_list
            + "\n\n次は本日実際に起きた事象の一覧だ。\n" + _events
            + "\n\n本日の事象によって**実際に点灯した指標だけ**を返せ。"
            "こじつけは禁止。曖昧なら返すな。該当が無ければ空配列を返せ。\n"
            '{"hits":[{"hid":"予測ID","idx":指標番号,"why":"点灯したと言える理由を1文"}]} のJSONだけ。',
            response_schema={"type": "object", "properties": {"hits": {"type": "array", "items": {
                "type": "object",
                "properties": {"hid": {"type": "string"}, "idx": {"type": "integer"}, "why": {"type": "string"}},
                "required": ["hid", "idx"]}}}, "required": ["hits"]}))
        _by_id = {h.get("id"): h for h in _huns}
        _lit, _today = [], datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        for _hit in ((_wd or {}).get("hits") or [])[:12]:
            _h = _by_id.get(str(_hit.get("hid")))
            _inds = (_h or {}).get("indicators") or []
            _idx = _hit.get("idx")
            if not _h or not isinstance(_idx, int) or not (0 <= _idx < len(_inds)):
                continue
            _ind = _inds[_idx]
            _h.setdefault("signals", [])
            if any(s.get("sign") == _ind.get("sign") and s.get("date") == _today for s in _h["signals"]):
                continue                                   # 同日重複は積まない(冪等)
            _h["signals"].append({"date": _today, "sign": _ind.get("sign", ""),
                                  "dir": _ind.get("dir", "confirm"),
                                  "why": str(_hit.get("why") or "")[:200]})
            if _ind.get("dir") == "kill":
                _h["needs_review"] = True                  # 人の目を入れる。自動で×にはしない
            _lit.append((_h.get("id"), _ind.get("dir"), _ind.get("sign", "")))
        if _lit:
            dump_json(HUNCHES_PATH, _huns)                 # 追記のみ・他フィールドは不変
            render_pages()                                 # 台帳へ即反映
            watchdog_note = ("\n\n=== 追跡中の予測に本日点灯したシグナル(自分の読みの生死) ===\n"
                             + "\n".join("- %s [%s] %s" % (i, d, s) for i, d, s in _lit))
        print("watchdog: 点灯 %d件 / 監視中 %d件" % (len(_lit), len(_pend)))
    else:
        print("watchdog: 監視対象なし(指標つき判定待ち予測が0件)")
except Exception as e:
    print("watchdog 失敗(無視して続行):", repr(e)[:140])

# コンセンサスの値段: 予測市場の価格=群衆が金を賭けた確率。相場を知らずに「非コンセンサス」は名乗れない。
markets = fetch_markets()
market_block = ("\n\n=== 予測市場の現在値(群衆のコンセンサス。ここから外れる読みだけが edge) ===\n"
                + "\n".join("[%d] %d%% %s [%s]" % (i, round(m["p"] * 100), m["q"], m["src"])
                            for i, m in enumerate(markets))) if markets else ""
print("予測市場: %d件" % len(markets))

# --- 1段目: 選別 ---
sel = None
if arts:
    try:
        sel = unwrap_list(parse_json(gemini(PERSONA +
            "\n以下は過去24時間の候補。[一次]=公式発表・論文・求人・規制・SDKリリース等の一次情報や"
            "発表前の兆候、[実勢]=数字で分かる現実、[二次]=既に話題化した記事。\n"
            "重要な記事を6〜8本選び、番号だけをJSON配列で返せ。\n"
            "【選定の原則】**[差分]は最優先(公式発表なしに静かに変わったもの=最も早い兆候)。"
            "次に[一次]を優先し、最低3本は[差分]か[一次]から選べ**。"
            "[二次]だけで埋めるな(全員が読んだ記事の要約に価値はない)。"
            "特に『まだ誰も繋げていない兆候』(求人の職種・SDKの新機能・論文・規制の条文)を拾え。\n"
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

# --- 本文を読ませる(質の根本原因) -------------------------------------------
# 従来は見出しだけで分析していたため、中身を知らないモデルが定型の一般論で埋めていた。
# 上位6本の本文抜粋を渡す。取得失敗は握りつぶし、タイトルのみで続行する(止めない)。
_bodies = []
for _i, _a in enumerate(picked[:6]):
    _txt, _ok = fetch_body(_a["url"])
    _bodies.append(str(_i) + ". " + _a["title"] + "\n  body_fetched: " + ("true" if _ok else "false")
                   + "\n  本文抜粋: " + (_txt[:1200] if _ok else "(取得できず。タイトルのみで判断せよ)"))
body_block = "\n\n".join(_bodies)
print("本文取得: %d/%d 件成功" % (sum(1 for b in _bodies if "body_fetched: true" in b), len(_bodies)))

# --- 2段目: 分析メモ ---
memos = None
if picked:
    try:
        m = unwrap_list(parse_json(gemini(PERSONA +
            "\n以下は今日の重要記事。参謀として各記事を分析し、次のJSON配列だけを返せ:\n"
            '[{"i": 記事番号, "omote": "表:何が起きたか(1〜2文)", '
            '"ura": "裏:それが本当に意味すること・裏で誰が何を狙っているかの見立て(1〜2文)", '
            '"nihon": "日本語圏への未到達度(高/中/低)とその理由を一言", '
            '"hypotheses": [{"h": "この件の裏で何が起きているかの仮説", '
            '"kill": "この仮説を殺しうる事実・矛盾(無ければ「見当たらない」)"}]}]\n'
            "【ACH=対立仮説分析】hypotheses は必ず2〜3個。**自分の推し仮説を含め、各仮説を"
            "『支持する証拠』ではなく『反証する事実』で潰しにいけ**(確証バイアス対策・諜報分析の定石)。"
            "反証に最も耐えた仮説が本命だ。\n"
            "【厳守】uraは本文の具体(誰が何をいつ・数字・条件)に基づいて書け。"
            "body_fetched:false の記事について中身を断定するな(タイトルから言えることだけ書け)。\n\n"
            + plist + "\n\n=== 本文抜粋 ===\n" + body_block)))
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
         "kan_konkyo": str(b.get("kan_konkyo") or "").strip(),  # 勘の根拠(任意)
         "kan_hantai": str(b.get("kan_hantai") or "").strip(),  # 反証条件(任意)
         "kan_conf": b.get("kan_conf"),                          # 確度(任意・後段で中央値に置換)
         "ura_taikou": str(b.get("ura_taikou") or "").strip(),   # 退けた対抗仮説(ACH・任意)
         "ippan": str(b.get("ippan") or "").strip(),  # 一般人の超参謀(任意・無くてもフォールバックは動く)
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
    material = "今日の重要記事:\n" + plist + diff_block + market_block + watchdog_note
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
        '"dousuru": "読者個人への具体的な示唆のみ。日本企業・業界への提言は禁止。「明日これを見ておけ」レベルまで具体化。2〜4文。**これは読者への指示だから命令形で書け**(「〜しろ」「〜を終わらせておけ」)。推量形(「〜するはずだ」「〜だろう」)で指示を書くな", '
        '"kan": "参謀の勘:確証はないが匂う話を1つ。第三者が公開情報で後から○×を付けられる、期限つき予測の形で書く。基準率から入って調整しろ(滅多に起きない事象に短い期限を付けるな)。非公開・秘密・リーク前提の当てられない予測は書くな", '
        '"kan_konkyo": "その勘の根拠。今日のどの材料(どの一次情報・数字)から来たかを1文で", '
        '"kan_hantai": "外れるとすれば最も強い理由を1文(自分で反証しろ)", '
        '"kan_conf": 勘が的中する確率(0.05〜0.95の数値。基準率から入って調整した値), '
        "※kan の文体は kan_conf と矛盾させるな。確度が50%未満なら『〜する』と言い切らず"
        "『本命ではないが〜と見る』『五分に満たないが〜の目がある』のように、確度と整合する書き方にしろ。 "
        '"ura_taikou": "裏を書く際に検討して退けた対抗仮説と、退けた理由を1文(ACH)", '
        '"ippan": "AIが使える一般人用超参謀:AIを日常で使う普通の人向けに、今日のニュースを専門知識ゼロでも今日から得する/損しない具体行動へ翻訳(2〜4文)。煽らず・実用・すぐできる。誇大広告や詐欺から守る視点も。※この項目だけは一般読者向け(他の先回り個人像とは別)", '
        '"mijoriku": [{"title": "記事タイトル(日本語訳可)", "desc": "一言説明", "why": "なぜ日本で先回りの価値があるか"}]}\n'
        "mijorikuは材料の中から日本語圏でまだほぼ話題になっていなさそうな話を1〜2本選ぶこと。弱い根拠を『確実』で塗るな。\n"
        "【記憶がある場合】上の【参謀の記憶】を踏まえ、omote/ura/kan は過去の自分の読みと結果に触れて自己更新せよ"
        "(例:『前に◯◯と読んだが△△で外した/当たった。今回はこう修正する』)。継続スレッドは乗り換えず続きとして書け。\n"
        "専門用語には <t data-d='この文脈での一言解説'>用語</t> の形式で解説を埋め込め"
        "(1セクションあたり2〜4語まで。data-dは必ずシングルクォートで書き、中に引用符・山括弧・改行を入れない。"
        "JSON文字列を壊す二重引用符は文中で使わない。t以外のタグは使わない。"
        "語を分割・重複させてタグ付けするな——直前に同じ字を残す『独<t>独占</t>』のような重複を作らず、タグは語全体に付けろ)。\n\n" + material)
    for attempt in range(2):  # 応答形式が不正だった場合は1回だけ再生成を試す
        # attempt0 は構造化出力(JSON準拠保証)。schema起因の失敗があっても attempt1 は
        # schema なしで従来動作に退避(グレースフル=壊さない)。
        schema = BRIEF_SCHEMA if attempt == 0 else None
        try:
            final = norm_final(parse_json(gemini(prompt3, response_schema=schema)))
        except Exception as e:
            print("final", e)
        if final:
            break
        print("final attempt", attempt + 1, "failed,", "retrying" if attempt == 0 else "giving up")

# --- 赤ペン(自己添削): 公開前に自分の草稿を殴る -----------------------------
# 一直線の生成は「それらしい定型」で落ち着きやすい。公開前に1回だけ批判→修正させる。
# 失敗・不正形なら草稿をそのまま使う(壊さない)。
if final:
    try:
        _crit = norm_final(parse_json(gemini(
            PERSONA + "\n次はあなたが書いた今朝の草稿だ。公開前に自分で赤を入れろ。\n"
            + json.dumps(final, ensure_ascii=False) + "\n\n"
            "点検項目:\n"
            "1. 裏が『誰でも言える定型』(規制=独占防衛/大手の囲い込み/安全性は建前 等)になっていないか。"
            "なっていれば、材料の具体(誰が・何を・いつ・数字)に基づく非コンセンサスな読みへ**書き直せ**。\n"
            "2. 逆読みしていないか(資本・既存プレイヤーを利する変化を、個人の先回り機会として描いていないか)。\n"
            "3. 『確実』等の危険語を使っていないか。\n"
            "4. 勘に期限があるか。基準率を無視した過剰具体になっていないか。kan_konkyo(根拠)と"
            "kan_hantai(外れるとすれば)が埋まっているか。空なら埋めろ。\n"
            "5. 『今日この材料を読まなくても書けた文章』が混じっていないか。あれば差し替えろ。\n"
            "問題が無い項目はそのまま残してよい。**同じスキーマのJSONだけ**を返せ(説明禁止)。",
            response_schema=BRIEF_SCHEMA)))
        if _crit:
            print("赤ペン: 草稿を更新")
            final = _crit
        else:
            print("赤ペン: 応答が不正のため草稿を維持")
    except Exception as e:
        print("赤ペン失敗(草稿を維持):", repr(e)[:120])

# --- 勘の確度をアンサンブルで決める(赤ペンで勘が確定した後に実施) --------------
# Halawi(NeurIPS24)/Nostreambot: 単発より複数見積もりの集約が強い。独立にK回見積もり中央値を採る
# (外れ値に強い)。1票は別モデル(flash-lite)へ振り相関を下げる。失敗時は執筆時の確度のまま(壊さない)。
ens_votes = []
if final and final.get("kan"):
    _ens_prompt = (PERSONA + "\n次の予測が的中する確率だけを見積もれ。\n予測: " + final["kan"]
                   + ("\n根拠: " + final["kan_konkyo"] if final.get("kan_konkyo") else "")
                   + ("\n外れるとすれば: " + final["kan_hantai"] if final.get("kan_hantai") else "")
                   + market_block
                   + "\n\n手順を厳守しろ: (1)この種の事象が起きる**基準率**を先に置く "
                   "(2)今日の材料で上下に調整する (3)**その確度は過信/過小でないか**を自問して補正する。\n"
                   "さらに market_i: 上の予測市場の中に**この予測とまったく同じ事象の成否を問うもの**が"
                   "あればその番号を、無ければ -1 を返せ。**主体(企業名)が同じだけでは同じ事象ではない**"
                   "(例: 『Anthropicが買収を発表するか』と『Anthropicの訴訟が和解するか』は別物なので -1)。\n"
                   '{"p": 0.05〜0.95の数値, "why": "基準率と調整理由を1文", "market_i": 整数} のJSONだけを返せ。')
    _ens_schema = {"type": "object",
                   "properties": {"p": {"type": "number"}, "why": {"type": "string"},
                                  "market_i": {"type": "integer"}},
                   "required": ["p"]}
    mkt_votes = []
    for _mdl in (None, None, "gemini-flash-lite-latest"):   # 3票(うち1票は低相関メンバー)
        try:
            _v = parse_json(gemini(_ens_prompt, response_schema=_ens_schema, model=_mdl))
            _p = float(_v.get("p")) if isinstance(_v, dict) else None
            if _p is not None and 0.0 < _p < 1.0:
                ens_votes.append(round(_p, 2))
            _mi = _v.get("market_i") if isinstance(_v, dict) else None
            if isinstance(_mi, int) and 0 <= _mi < len(markets):
                mkt_votes.append(_mi)
        except Exception as e:
            print("ensemble vote", repr(e)[:100])
    # 同じ事象を問う市場は、多数決で2票以上一致した時だけ採用する(1票の思い込みで誤対応させない)
    market_match = None
    for _i in set(mkt_votes):
        if mkt_votes.count(_i) >= 2:
            market_match = markets[_i]
            break
    print("市場マッチ:", (market_match["q"][:60] if market_match else "該当なし(乖離は表示しない)"))
    _med = median(ens_votes)
    if _med is not None:
        print("勘の確度: 票", ens_votes, "→ 中央値", _med)
        final["kan_conf"] = _med
    else:
        print("勘の確度: アンサンブル不成立。執筆時の値を使用")

# 執筆(LLM)が成功したか。失敗時は既存の良好なページを保持し上書きしない。
generation_ok = bool(final)

# --- 「今日の一手」の材料生成(docs/ichite.json) ---------------------------
# 着地不足を叩く道具。ランダムな一般論ではなく、今日の記事+参謀の記憶+今日のブリーフィングから
# 「あなたが今日動くための問い」と「30分で始められる一手」を作る。
# 失敗しても既存の JSON を残すだけ(サイト本体には一切影響しない)。
ICHITE_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {"type": "array", "items": {"type": "string"}},
        "moves": {"type": "array", "items": {
            "type": "object",
            "properties": {"t": {"type": "string"}, "why": {"type": "string"}},
            "required": ["t", "why"]}},
    },
    "required": ["questions", "moves"],
}

if generation_ok:
    try:
        _ich_prompt = (PERSONA +
            "\n今朝のブリーフィングは次の通り:\n"
            + json.dumps({"omote": final["omote"], "ura": final["ura"],
                          "dousuru": final["dousuru"], "kan": final["kan"]}, ensure_ascii=False)
            + ("\n\n" + _digest if _digest else "")
            + "\n\n読者は着想は溢れるほど出るが『着地』しない人物だ。今日この人を1歩だけ動かすための材料を作れ。\n"
            "次のJSONだけを返せ:\n"
            '{"questions": ["今日の状況を踏まえた、答えると自分の一手が決まる問い(3つ・各40字以内)。'
            "抽象的な自己啓発でなく、今日の記事の具体に紐づけろ。『どう思う？』ではなく『あなたは何をするか』を引き出せ\"], "
            '"moves": [{"t": "今日30分以内に始められる具体行動(25字以内・動詞で始める)", '
            '"why": "なぜ今日これなのか、今日の記事や過去の読みに紐づけて1文"}]}\n'
            "movesは3つ。『情報収集する』『検討する』のような曖昧な行動は禁止。"
            "手を動かして終わる形(作る/送る/申し込む/書く/試す/測る)にしろ。"
            "新しいネタへ乗り換えさせず、既に動いている流れの続きを優先しろ。")
        _ich = parse_json(gemini(_ich_prompt, response_schema=ICHITE_SCHEMA))
        _qs = [str(q).strip() for q in (_ich.get("questions") or []) if str(q).strip()][:3] if isinstance(_ich, dict) else []
        _mv = []
        for _m in ((_ich.get("moves") or []) if isinstance(_ich, dict) else []):
            if isinstance(_m, dict) and str(_m.get("t") or "").strip():
                _mv.append({"t": str(_m["t"]).strip(), "why": str(_m.get("why") or "").strip()})
            if len(_mv) == 3:
                break
        if _qs and _mv:
            os.makedirs("docs", exist_ok=True)
            with open("docs/ichite.json", "w", encoding="utf-8") as f:
                json.dump({"date": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"),
                           "questions": _qs, "moves": _mv,
                           "kan": final.get("kan", "")}, f, ensure_ascii=False, indent=2)
            print("ichite: docs/ichite.json を更新 (問い%d・一手%d)" % (len(_qs), len(_mv)))
        else:
            print("ichite: 形が不正のため据え置き:", repr(_ich)[:160])
    except Exception as e:
        print("ichite 生成失敗(据え置き):", repr(e)[:160])

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

# variant perception: 参謀の確度と、最も近い市場価格の**乖離**こそが edge。
# 相場を知らずに「非コンセンサス」は名乗れない。近い市場が無ければ何も出さない。
edge_html = ""
_near = globals().get("market_match")   # 同一事象と2票以上で判定された市場だけ(語の一致では対応させない)
if final.get("kan_conf") and _near:
    _mine = round(float(final["kan_conf"]) * 100)
    _mkt = round(_near["p"] * 100)
    _gap = _mine - _mkt
    edge_html = ('<p class="m"><b>市場との差</b> 市場 ' + str(_mkt) + '% / 参謀 ' + str(_mine)
                 + '% → 乖離 ' + ("+" if _gap >= 0 else "") + str(_gap) + 'pt'
                 + ('（ここが edge。一致なら妙味なし）' if abs(_gap) >= 10 else '（ほぼ市場並み＝妙味は薄い）')
                 + '<br><span class="kv">' + html.escape(_near["q"][:70]) + ' [' + _near["src"] + ']</span></p>')

mj_html = ""
if final["mijoriku"]:
    # 「未上陸」をLLMの主観でなく実測にする: 日本語ニュースを実際に引き、0件なら本当に未到達と言える。
    _mj_parts = []
    for x in final["mijoriku"]:
        _jp = fetch_jp_hits(x["title"], limit=3)
        _ev = ('<p class="m">日本語記事: <b>0件</b>(実測・本当に未到達)</p>' if not _jp
               else '<p class="m">日本語記事: ' + str(len(_jp)) + '件あり(例: '
                    + html.escape(_jp[0][:40]) + ')</p>')
        _mj_parts.append('<div class="mj"><div class="mjt">' + render_rich(x["title"]) + "</div><p>"
                         + render_rich(x["desc"]) + '</p><p class="mjw">先回りの価値:'
                         + render_rich(x["why"]) + "</p>" + _ev + "</div>")
    mj_html = '<section class="reveal"><h2>未上陸</h2>' + "".join(_mj_parts) + "</section>"

# 答え合わせ節(過去の勘の実績=成長する参謀。dataから作り、執筆LLMの成否と独立)
_ans_recs, _ans_huns = memory.load_ledger()
_ans_st = memory.hit_stats(_ans_huns)

# 追跡中の予測・答え合わせは panels.py の純関数に切り出し済み(プレビューと同一コードを使う)。
track_html = panels.tracking_html(_ans_huns, jst.date())
ans_html = panels.answers_html(_ans_huns, jst.date(), memory)

page = """<!DOCTYPE html><html lang="ja" class="no-js"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>シリコンバレー参謀</title><style>""" + PAGE_CSS + """</style></head><body>
<div class="progress" aria-hidden="true"></div>
<main>
<header class="hd"><h1>◇ シリコンバレー参謀</h1>
<div class="d">""" + jst.strftime("%Y.%m.%d %H:%M") + """ JST</div>
<nav class="nav"><a class="refresh" href="https://github.com/tensaikamo/stick-koubou/actions/workflows/sanbo.yml" target="_blank" rel="noopener">⟳ 参謀に調べ直させる</a><a href="records.html">記録の台帳</a><a href="hunches.html">勘の台帳</a><a href="threads.html">記憶の物語</a><a href="ichite.html">今日の一手</a></nav>
<div class="hint">「調べ直させる」→ GitHubで Run workflow を1タップ。数分で参謀が記憶を踏まえて考え直す。反映後にこのページを再読み込み。</div></header>
<section class="reveal"><h2>今日の空気</h2>
<p><span class="lb">表</span>""" + render_rich(final["omote"]) + """</p>
<p><span class="lb">裏</span>""" + render_rich(final["ura"]) + """</p>""" + (
  ('<p class="m"><b>退けた説</b> ' + render_rich(final["ura_taikou"]) + "</p>") if final.get("ura_taikou") else "") + """</section>
<section class="reveal"><h2>で、どうする</h2><p>""" + render_rich(final["dousuru"]) + """</p></section>
<section class="reveal"><h2>参謀の勘</h2><p>""" + render_rich(final["kan"]) + """</p>""" + (
  ('<p class="m"><b>根拠</b> ' + render_rich(final["kan_konkyo"]) + "</p>") if final.get("kan_konkyo") else "") + (
  ('<p class="m"><b>外れるとすれば</b> ' + render_rich(final["kan_hantai"]) + "</p>") if final.get("kan_hantai") else "") + (
  ('<p class="m"><b>確度</b> ' + str(round(float(final["kan_conf"]) * 100)) + "%"
   + ("（独立見積り " + "・".join(str(round(v * 100)) + "%" for v in ens_votes) + " の中央値）" if len(ens_votes) > 1 else "")
   + "</p>") if final.get("kan_conf") else "") + edge_html + """</section>
""" + (('<section class="reveal"><h2>一般人の超参謀</h2><p class="m">AIを普通に使う人向け・今日からできる一手</p><p>'
        + render_rich(final["ippan"]) + "</p></section>") if final.get("ippan") else "") + """
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
