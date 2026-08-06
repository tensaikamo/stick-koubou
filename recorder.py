"""予測の永続記録層(record=事実 → hunch=予測)。

サイト生成(sanbo.py)とは独立した記録専用パス。ワークフローでは別ステップ・
continue-on-error で実行され、この処理が失敗してもサイトの生成・公開は妨げない。
判定・スコア計算・UI表示は本モジュールの対象外(構造化して貯めるだけ)。

日付は全て Asia/Tokyo 基準(GitHub Actions は UTC で走るため厳守)。
"""
import os, re, json, html
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests  # 分岐1: 本文取得は requests を使う(明示指示)

from common import GeminiClient, fetch_all, parse_json, PAGE_CSS
import memory
import decision

JST = ZoneInfo("Asia/Tokyo")
DATA_DIR = "data"
RECORDS_PATH = os.path.join(DATA_DIR, "records.json")
HUNCHES_PATH = os.path.join(DATA_DIR, "hunches.json")
RUNS_DIR = os.path.join(DATA_DIR, "runs")

SCHEMA_VERSION = "1"
GENERATOR_VER = "v1"
BODY_LIMIT = 8000            # 本文テキストは先頭8000字まで
TOP_N = 8                    # 本文取得・記録対象にする上位記事数
DEADLINE_MIN_DAYS = 3
DEADLINE_MAX_DAYS = 30
DEDUPE_DAYS = 30             # この日数内に同一URLの record があれば新規に起こさない
RENDER_LIMIT = 100           # 台帳ページに描画する最大件数(全件描画だと確実に肥大する)

# decider が観測手続きに落ちない曖昧表現(PHASE 3 の遮断キーワード・ヒューリスティック)
VAGUE_WORDS = ["話題", "注目", "バズ", "盛り上が", "期待", "騒がれ", "人気", "有名", "評判"]

# 第三者が公開情報だけで○×できない=検証不能な予測を弾く語(claim/deciderを走査)。
# "裏付け"の"裏"を誤爆しないよう、単独の"裏"は避けて具体語に限定する。
UNVERIFIABLE_WORDS = ["非公開", "未公表", "秘密", "極秘", "リーク", "水面下", "こっそり",
                      "内部情報", "内部で", "裏で", "裏枠", "裏api", "非公表", "密かに"]

PERSONA = """読者は個人で情報優位を作り先回りを狙う人物。断言型・根拠つき。
「確実だ」「間違いない」等の断定語は使わない。企業向け提言・一般論は書かない。

【外部視点を先に置け(参照クラス予測)】
実データで測ったら、台帳の予測33件が**33件とも「XがYをする」型**で、確度は0.60〜0.85の
狭い帯(標準偏差0.06)に固まっていた。つまりこの参謀は「起きる方に賭け続け、しかも
どれが確からしいか区別できていない」。中央値14日という短期に企業が特定の新しい行動を
取る基準率は低いので、これは体系的な過信であり、実際に最初の決着は外れだった。

だから順番を守れ。まず**参照クラスの頻度**(この種の企業が、この種のことを、この日数で
実際にやる頻度)を置く。次に今日の材料で上下に調整する。確度は基準率からの乖離として
決めろ。願望や語調の強さで上げるな。

【起きない方にも賭けろ】
「起きない」「見送られる」「期日までに出ない」という予測も**同じだけ価値がある**。
短期では大半のことは起きないのだから、むしろそちらが基準率に沿う。的中しやすい方に
逃げず、外部視点が示す方に賭けろ。

【確度は本気で刻め】
0.75・0.80・0.85 のようなキリの良い値に逃げるな。0.05〜0.95 の全域を使い、
自信が無いなら0.3や0.15と書け。全部同じ確度を付ける予測者は、何も予測していない。"""


# ---- 入出力ユーティリティ ------------------------------------------------

def load_json_array(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
    except Exception as e:
        print("load_json_array", path, e)
        return []
    if not isinstance(v, list):
        return []
    # 壊れた要素(null・文字列など)は捨てる。1つ混じるだけで台帳を読む全処理が
    # AttributeError で落ち、ブリーフィングごと止まるため(memory._load と同じ方針)。
    out = [x for x in v if isinstance(x, dict)]
    if len(out) != len(v):
        print("load_json_array", path, "壊れた要素 %d件を除外" % (len(v) - len(out)))
    return out


def dump_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def strip_html(raw):
    """stdlib のみで HTML からテキストを抽出(script/style除去→タグ除去→空白圧縮)。"""
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    txt = html.unescape(raw)
    txt = re.sub(r"[ \t\r\f]+", " ", txt)
    txt = re.sub(r"\n\s*\n\s*", "\n\n", txt)
    return txt.strip()


_GH = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9._-]+)(?:/([A-Za-z0-9._-]+))?", re.I)


def github_api_urls(url):
    """github.com のページURLを、中身が確実に取れる api.github.com のURLに置き換える。

    GitHubのHTMLはJSで描画されるため、取得は成功しても本文がほぼ空で返る。それを
    「開いて確かめた」と扱うと、実際には何も読めていないのに『載っていない→外れ』と
    判定しかねない(偽×は参謀の信用を壊す最悪の事故)。JSON API なら実体が取れる。
    戻り: 取得すべきURLの配列(github.com でなければ空配列)。
    """
    m = _GH.match(url or "")
    if not m:
        return []
    org, repo = m.group(1), m.group(2)
    if repo and repo.lower() not in ("orgs", "search", "about", "features"):
        return ["https://api.github.com/repos/%s/%s" % (org, repo),
                "https://api.github.com/repos/%s/%s/releases?per_page=20" % (org, repo)]
    return ["https://api.github.com/orgs/%s/repos?sort=updated&per_page=50" % org,
            "https://api.github.com/users/%s/repos?sort=updated&per_page=50" % org]


def fetch_body(url):
    """記事本文を best-effort で取得。失敗は握りつぶし ("", False) を返す。
    ここでの失敗が記録処理・サイト生成を止めることは絶対にない。
    github.com は JSON API へ振り替える(HTMLだとJS描画で中身が取れないため)。"""
    api = github_api_urls(url)
    if api:
        # 匿名だと 60req/時(IP単位)で、Actions の共有IPだと詰まりうる。トークンがあれば使う。
        hdr = {"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github+json"}
        _tok = os.environ.get("GITHUB_TOKEN", "")
        if _tok:
            hdr["Authorization"] = "Bearer " + _tok
        out = []
        for u in api:
            try:
                r = requests.get(u, timeout=10, headers=hdr)
                if r.status_code != 200:
                    print("fetch_body(github)", u, "HTTP", r.status_code)
                    continue
                out.append(u + "\n" + _github_summary(r.json()))
            except Exception as e:
                print("fetch_body(github)", u, repr(e)[:120])
        text = "\n\n".join(out)
        return text[:BODY_LIMIT], bool(text.strip())
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return "", False  # 404/500等のエラーページ本文を記録しない
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype and ctype:
            return "", False
        text = strip_html(r.text)
        return text[:BODY_LIMIT], bool(text.strip())
    except Exception as e:
        print("fetch_body", url, repr(e)[:120])
        return "", False


def _github_summary(data):
    """GitHub API の応答を、判定に使える平文へ畳む(名前・説明・更新日・公開日)。"""
    def one(d):
        if not isinstance(d, dict):
            return ""
        f = [str(d.get(k) or "") for k in ("full_name", "name", "tag_name")]
        who = next((x for x in f if x), "")
        when = str(d.get("published_at") or d.get("pushed_at") or d.get("updated_at") or "")[:10]
        desc = str(d.get("description") or d.get("body") or "")[:300]
        return "- %s%s%s" % (who, (" [%s]" % when if when else ""), (" : " + desc if desc else ""))
    if isinstance(data, list):
        return "\n".join(x for x in (one(d) for d in data[:50]) if x)
    return one(data)


# ---- LLM 生成 ------------------------------------------------------------

def build_prompt(articles, today_str):
    lines = []
    for i, a in enumerate(articles):
        body = a["body"] if a["body_fetched"] else "(本文取得に失敗。タイトルのみ)"
        lines.append(
            "[記事 " + str(i) + "] " + a["title"]
            + " (signal " + str(a["hn_score"]) + ", " + a["src"] + ")\n"
            + "URL: " + a["url"] + "\n"
            + "body_fetched: " + ("true" if a["body_fetched"] else "false") + "\n"
            + "本文抜粋: " + (body[:1500] if body else "") + "\n")
    material = "\n".join(lines)
    recs, huns = memory.load_ledger()
    digest = memory.build_digest(recs, huns)  # 過去の自分の観測・読み・結果
    mem_block = (digest + "\n\n") if digest else ""
    return (PERSONA + "\n\n" + mem_block
        + "本日はJST(日本標準時)で " + today_str + " である。予測期限はこの日付を基準に相対日数で示せ。\n"
        "上の【参謀の記憶】がある場合は、過去の読みと結果を踏まえて確度を較正し、外した型を繰り返すな。"
        "既存スレッドの話題は新ネタ扱いせず「続き」として捉え、乗り換えずに深めよ。\n"
        "以下は本日の候補記事。シリコンバレーAI業界の観点で重要なものについて、"
        "後から採点できる『事実(record)』と『予測(hunch)』を作れ。\n"
        "次のJSONオブジェクトだけを返せ(前置き・コードフェンス・配列ラップ禁止):\n"
        '{\n'
        '  "records": [\n'
        '    {\n'
        '      "article_index": 記事番号(整数),\n'
        '      "headline": "事実のみの見出し。断定的修辞・裏読み・煽り禁止",\n'
        '      "what_happened": "誰が何を発表/報告したかを明示した要約(3〜5文)。body_fetched=false の記事はタイトルのみに基づくと明示せよ",\n'
        '      "background": "日本語話者が理解に必要な前提知識",\n'
        '      "changed": "この件で何が変わったか(事実の範囲のみ)",\n'
        '      "certainty": "confirmed|reported|rumor のいずれか",\n'
        '      "source_tier": "primary|secondary のいずれか"\n'
        '    }\n'
        '  ],\n'
        '  "hunches": [\n'
        '    {\n'
        '      "based_on": [根拠にする records のインデックス(整数)。1件以上必須],\n'
        '      "prose": "サイト表示体裁の散文。断言型・根拠つき。「確実だ」「間違いない」等の断定語は禁止",\n'
        '      "claim": "予測本文(1文)",\n'
        '      "subject": "主体(企業/製品/組織の固有名)",\n'
        '      "resolution": {\n'
        '        "source": "判定時に実際に開くURL(https://で始まる1本。例 https://fireworks.ai/blog '
        'や 料金ページ)。説明文ではなくURLを書け。**期日にそのページを開けば的中/外れが分かる**場所を選べ。'
        'JSで描画されるページ(SPA)は中身が読めないので避けろ",\n'
        '        "check_query": "英語のみ2〜4語",\n'
        '        "decider": "何が満たされたら的中か。公式発表/公開リーダーボード/公開リポジトリ/報道など"'
        '『第三者が公開情報だけで期日に○×を付けられる観測点』で書け。『話題になる』等の曖昧表現は禁止"\n'
        '      },\n'
        '      "deadline_days": 判定期限までの日数(今日からの相対、3〜30の整数。絶対日付は書くな),\n'
        '      "base_rate": 0.01〜0.95の数値。**先にこれを決めろ**。今日の材料を一切見ずに、'
        '「この種の企業が・この種のことを・この日数で実際にやる」参照クラスの頻度はいくつか。'
        '短期に特定の新しい行動が起きる頻度は普通かなり低い,\n'
        '      "base_rate_class": "どの参照クラスで数えたか(例『大手AI企業が予告なしに新モデルをGAする・14日窓』)",\n'
        '      "confidence": 0.05〜0.95の数値。base_rate を出発点に、今日の材料の強さで**上下に**調整した値。'
        'キリの良い値(0.75/0.80/0.85)に逃げず刻め。材料が弱いなら base_rate を下回ってよい,\n'
        '      "confidence_why": "base_rate からどちら向きに何故ずらしたか(1文)。ずらさないならその理由",\n'
        '      "counter": "この予測が外れるとしたら最も強い理由(1文・具体的に)。過信を戒め自分で反証せよ",\n'
        '      "indicators": [{"sign": "この予測の生死を示す、公開情報で毎日観測できる具体的な事象", "dir": "confirm または kill"}]\n'
        '    }\n'
        '  ]\n'
        '}\n'
        "record は重要な記事のみ(最大" + str(TOP_N) + "件)。hunch は 0〜2件、必ず**本日初めて得た**recordを根拠にせよ。"
        "新しい事実が無い、既存予測と実質同じ、基準率から見てedgeが無い場合はhunchを0件にしろ。"
        "予測を毎日作ること自体には価値がない。\n"
        "【指標(indicators)】各 hunch に2〜3個。期日を待たずに生死が分かる観測点を置け。"
        "dir=confirm は『これが起きたら的中に近づく』、dir=kill は『これが起きたらこの予測は死ぬ』。"
        "毎日の公開情報で観測できる形にしろ(例:『APIの価格表に新モデルが載る』『◯◯社が該当職種の求人を出す』"
        "『モデル一覧から非推奨になる』)。『話題になる』等の観測できない表現は禁止。\n"
        "【予測の絶対条件】第三者が公開情報だけで期日に○×を判定できる予測に限る。"
        "非公開・秘密・リーク・内部情報・水面下の動きを前提にした予測は禁止(検証できないため失格)。"
        "可の例:『9月中にOpenAIが日本でセルフサーブ受付を開始する』。"
        "不可の例:『裏で〜枠を売る』『非公開で〜と提携する』。\n"
        "【誰を利するか点検】その変化が資本・既存プレイヤーを利するものを、個人の先回り機会として"
        "描くな(例:広告枠の新設は推薦を「買える場所」にし、オーガニックな差し込み余地はむしろ縮む)。\n\n"
        + material)


def call_records_hunches(client, articles, today_str, fake_response=None):
    """LLM を呼び records/hunches を得る。パース失敗時は最大2回再試行(計3回)。"""
    if fake_response is not None:
        return fake_response
    prompt = build_prompt(articles, today_str)
    for attempt in range(3):
        try:
            raw = client.generate(prompt)
            data = parse_json(raw)
            if isinstance(data, dict) and isinstance(data.get("records"), list) \
                    and isinstance(data.get("hunches"), list):
                return data
            print("records/hunches unexpected shape (attempt", attempt + 1, "):", repr(data)[:200])
        except Exception as e:
            print("call_records_hunches attempt", attempt + 1, e)
    return None


def regen_hunch(client, base_records, reason, today_str, fake_response=None):
    """検証不合格 hunch を1件、prose 含め丸ごと作り直す。"""
    if fake_response is not None:
        return fake_response
    recs = [{"index": i, "headline": r["headline"], "certainty": r["certainty"]}
            for i, r in enumerate(base_records)]
    prompt = (PERSONA + "\n\n"
        "本日はJSTで " + today_str + "。次の事実(records)を根拠に、採点可能な予測(hunch)を1件だけ作り直せ。\n"
        "前回の不合格理由: " + reason + "\n"
        "不合格を避ける要件: subject(固有名)・resolution.source(https://で始まる実URL。期日にそこを"
        "開けば○×が分かる場所)・resolution.decider(観測可能な閾値/事象、"
        "『話題になる』等の曖昧語禁止)・deadline_days(今日からの相対日数、3〜30の整数。絶対日付は書くな)を"
        "必ず満たし、based_on は下記 index を1件以上参照(rumor のみを根拠にしない)。\n"
        "次のJSONオブジェクトだけを返せ:\n"
        '{"based_on":[index...],"prose":"...","claim":"...","subject":"...",'
        '"resolution":{"source":"https://で始まる実URL","check_query":"英語2〜4語","decider":"..."},'
        '"deadline_days":3〜30の整数,"base_rate":0.01〜0.95,"base_rate_class":"参照クラス",'
        '"confidence":0.05〜0.95,"confidence_why":"基準率からのずらし方",'
        '"counter":"外れる最も強い理由(1文)"}\n\n'
        "records:\n" + json.dumps(recs, ensure_ascii=False, indent=2))
    for attempt in range(2):
        try:
            raw = client.generate(prompt)
            data = parse_json(raw)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print("regen_hunch attempt", attempt + 1, e)
    return None


# ---- 検証ゲート(PHASE 3) ------------------------------------------------

def coerce_deadline_days(h):
    """deadline_days を整数として取り出す(文字列数値も許容)。取れなければ None。"""
    dd = h.get("deadline_days")
    if isinstance(dd, bool):
        return None
    if isinstance(dd, int):
        return dd
    if isinstance(dd, float) and dd.is_integer():
        return int(dd)
    if isinstance(dd, str) and dd.strip().lstrip("+").isdigit():
        return int(dd.strip())
    return None


def validate_hunch(h, records, created_dt):
    """不合格なら理由文字列、合格なら None を返す。records は採番前の内部配列。
    deadline は今日からの相対日数 deadline_days(3〜30)で受け、絶対日付はコード側で確定する
    (モデルが実行日を知らず過去日を出す事故を排除するため)。"""
    subject = str(h.get("subject") or "").strip()
    res = h.get("resolution") if isinstance(h.get("resolution"), dict) else {}
    decider = str(res.get("decider") or "").strip()
    based = h.get("based_on") if isinstance(h.get("based_on"), list) else []

    if not subject:
        return "subject が欠落"
    if not decider:
        return "resolution.decider が欠落"
    # 判定先は**実URL**であること。説明文("公式ブログ")だと答え合わせ側で開けず、
    # 検索グラウンディング頼みになる。無料枠ではそれが枯れて実際に決着を落とした
    # (2026-08-05)。開けるURLがあれば、クォータに依存せず自力で確かめられる。
    if not str(res.get("source") or "").strip().lower().startswith("https://"):
        return "resolution.source が https:// で始まる実URLでない(期日に開ける場所を書け)"
    # 外部視点(参照クラス)を通っていない予測は採らない。実測で33件中33件が「起きる」型・
    # 確度は0.60〜0.85の狭い帯に固まっており、体系的に過信していた。基準率を必須の
    # 構造化フィールドにして、確度を「基準率からどうずらしたか」として説明させる。
    br = h.get("base_rate")
    if not isinstance(br, (int, float)) or not (0.0 < float(br) < 1.0):
        return "base_rate(参照クラスの頻度)が欠落、または0〜1の数値でない"
    if not str(h.get("base_rate_class") or "").strip():
        return "base_rate_class(何をどの窓で数えたか)が欠落"
    conf = h.get("confidence")
    if isinstance(conf, (int, float)):
        # 基準率から大きく離すなら理由が要る(材料の強さで説明できない乖離を通さない)
        if abs(float(conf) - float(br)) > 0.25 and not str(h.get("confidence_why") or "").strip():
            return "confidence が base_rate から0.25超離れているのに confidence_why がない"
    dd = coerce_deadline_days(h)
    if dd is None:
        return "deadline_days が欠落または整数でない"
    if any(w in decider for w in VAGUE_WORDS):
        return "decider が観測手続きに落ちない曖昧表現"
    # 第三者が公開情報で判定できない予測(裏API枠型)は失格
    claim = str(h.get("claim") or "")
    blob = (claim + " " + decider).lower()
    if any(w in blob for w in UNVERIFIABLE_WORDS):
        return "第三者が公開情報で○×判定できない予測(非公開・秘密・リーク前提)"
    if not (DEADLINE_MIN_DAYS <= dd <= DEADLINE_MAX_DAYS):
        return "deadline_days が+3〜+30日の範囲外"
    # based_on: 1件以上・実在index・rumorのみは不可
    # 索引ずれ防止の空プレースホルダ(id="")は根拠として数えない
    idxs = [i for i in based if isinstance(i, int) and 0 <= i < len(records) and records[i].get("id")]
    if not idxs:
        return "based_on が空、または実在 record を参照していない"
    if not any(records[i].get("is_new", True) for i in idxs):
        return "本日初めて得た事実を根拠にしていない"
    if all(records[i].get("certainty") == "rumor" for i in idxs):
        return "根拠が certainty=rumor の record のみ"
    return None


# ---- レコード/ハンチ組み立て --------------------------------------------

REQUIRED_RECORD_FIELDS = ["id", "created_at", "headline", "what_happened", "background",
                          "changed", "certainty", "source_tier", "source", "body_fetched",
                          "model", "related_ids"]
REQUIRED_HUNCH_FIELDS = ["id", "created_at", "based_on", "prose", "claim", "subject",
                         "resolution", "deadline", "confidence", "status", "resolved_at",
                         "result", "evidence", "rejected", "model", "schema_version",
                         "generator_ver"]


def valid_record_schema(r):
    if any(f not in r for f in REQUIRED_RECORD_FIELDS):
        return False
    if r["certainty"] not in ("confirmed", "reported", "rumor"):
        return False
    if r["source_tier"] not in ("primary", "secondary"):
        return False
    if not isinstance(r["source"], dict) or not isinstance(r["related_ids"], list):
        return False
    return bool(str(r["headline"]).strip() and str(r["what_happened"]).strip())


def valid_hunch_schema(h):
    if any(f not in h for f in REQUIRED_HUNCH_FIELDS):
        return False
    if h["status"] not in ("pending", "unscorable"):
        return False
    if not isinstance(h["based_on"], list) or not isinstance(h["resolution"], dict):
        return False
    if not isinstance(h["rejected"], list):
        return False
    return bool(str(h["prose"]).strip() and str(h["claim"]).strip())


def process(articles, gen, created_dt, date_str, existing_records, existing_hunches):
    """LLM出力(gen)から採番済み records/hunches を構築して返す。
    gen: {"records":[...], "hunches":[...]}, existing_*: 既存配列(採番の連番算出用)。
    戻り: (new_records, new_hunches)。"""
    # --- 連番採番(その日の既存件数から続ける) ---
    r_seq = sum(1 for r in existing_records if str(r.get("id", "")).startswith(date_str + "-r"))
    h_seq = sum(1 for h in existing_hunches if str(h.get("id", "")).startswith(date_str + "-h"))
    now_iso = created_dt.isoformat()
    model = gen.get("_model") or "unknown"

    # --- records: article_index から source/body_fetched を実データで確定 ---
    # 既出URLは新recordを起こさない(冪等な取り込み)。HN上位は数日居座るため、
    # 素朴に記録すると同じ記事が毎日別recordになり(実測で重複率54%)、記憶が
    # 「同じ話が4日続く重要テーマ」という**偽の継続性**で汚染される。
    seen_urls = {}
    for r in existing_records:
        d = (r.get("created_at", "") or "")[:10]
        try:
            if (created_dt.date() - datetime.strptime(d, "%Y-%m-%d").date()).days > DEDUPE_DAYS:
                continue
        except Exception:
            pass
        u = ((r.get("source") or {}).get("url") or "").strip()
        if u:
            # certainty も覚える。噂として記録した事実を、翌日の重複経路で
            # 勝手に「報道」へ格上げしない(下の噂ゲートをすり抜けさせない)。
            seen_urls[u] = (r.get("id"), r.get("certainty") or "reported")

    _uniq_existing = memory.dedupe_by_url(existing_records)   # 関連付けは重複を除いた履歴で行う
    internal_records = []   # 検証・based_on解決用の内部表現(certainty含む)
    new_records = []
    for rec in gen.get("records", []):
        if not isinstance(rec, dict):
            continue
        ai = rec.get("article_index")
        art = articles[ai] if isinstance(ai, int) and 0 <= ai < len(articles) else None
        _u = (art or {}).get("url", "")
        if _u and _u in seen_urls:
            # 既出の事実。新recordは起こさず、根拠の鎖は既存recordのidで保つ。
            # certainty は**元のrecordのものを引き継ぐ**。決め打ちで "reported" にすると、
            # 昨日 rumor として記録した噂が今日は報道扱いになり、噂だけを根拠にした
            # 予測を弾くゲート(validate_hunch)を通ってしまう。
            _dup_id, _dup_cert = seen_urls[_u]
            internal_records.append({"id": _dup_id, "certainty": _dup_cert, "is_new": False,
                                     "headline": str(rec.get("headline") or "")})
            print("record 重複のためスキップ(既出:%s certainty=%s): %s" % (_dup_id, _dup_cert, _u[:70]))
            continue
        certainty = rec.get("certainty") if rec.get("certainty") in ("confirmed", "reported", "rumor") else "reported"
        # 一次/二次はモデルに自己申告させず、収集器が付けた層を正とする。
        source_tier = "primary" if (art or {}).get("tier") == 1 else "secondary"
        r_seq += 1
        rid = date_str + "-r%02d" % r_seq
        obj = {
            "id": rid,
            "created_at": now_iso,
            "headline": str(rec.get("headline") or "").strip(),
            "what_happened": str(rec.get("what_happened") or "").strip(),
            "background": str(rec.get("background") or "").strip(),
            "changed": str(rec.get("changed") or "").strip(),
            "certainty": certainty,
            "source_tier": source_tier,
            "source": {
                "url": art["url"] if art else "",
                "title": art["title"] if art else str(rec.get("headline") or ""),
                "hn_score": art["hn_score"] if art else 0,
                "name": art.get("src", "") if art else "",
                "tier": art.get("tier", 3) if art else 3,
            },
            "body_fetched": bool(art["body_fetched"]) if art else False,
            "model": model,
            # 同一主体の過去record(直近)へ紐づけ、記憶を"線"にする(スレッド化)
            # 重複を除いてから紐づける。素のままだと「関連6件」が同じ記事6本になり、
            # 枠を使い切って本当に別の出来事が入らない。
            "related_ids": memory.related_ids_for(
                str(rec.get("headline") or ""),
                (art["title"] if art else ""), _uniq_existing),
        }
        if valid_record_schema(obj):
            internal_records.append({"id": rid, "certainty": certainty, "is_new": True,
                                     "headline": obj["headline"]})
            new_records.append(obj)
        else:
            # 除外しても **索引をずらさない**(based_on はLLMが返した records の位置を指すため、
            # 詰めると別の record に根拠が付け替わる)。空idはbased_on解決時に落とす。
            internal_records.append({"id": "", "certainty": certainty, "is_new": False, "headline": ""})
            print("record スキーマ不正のため除外:", repr(obj)[:160])

    # --- hunches: 検証ゲート + 再生成 + 採番 ---
    new_hunches = []
    fakes = gen.get("_fake_regens", [])  # テスト用: 再生成応答の順次差し込み
    for hraw in gen.get("hunches", []):
        if not isinstance(hraw, dict):
            continue
        # 同じ記事を拾い直しただけの日に、新しい予測を捏造しない。
        _raw_idxs = [i for i in (hraw.get("based_on") or [])
                     if isinstance(i, int) and 0 <= i < len(internal_records)]
        if not any(internal_records[i].get("is_new") for i in _raw_idxs):
            print("hunch スキップ: 本日初めて得た事実を根拠にしていない")
            continue
        rejected = []
        current = hraw
        status = "pending"
        for attempt in range(3):
            reason = validate_hunch(current, internal_records, created_dt)
            if reason is None:
                break
            rejected.append({"draft": current, "reason": reason})
            if attempt == 2:
                status = "unscorable"
                break
            fake = fakes.pop(0) if fakes else None
            # based_on はこの実行内の article_index を指す。重複URLを含む日は
            # new_records だけを渡すと添字がずれるため、内部表現と同じ並びを渡す。
            regen = regen_hunch(getattr(process, "_client", None), internal_records, reason,
                                date_str, fake_response=fake)
            if not isinstance(regen, dict):
                # 再生成できなければ現案のまま次周(3回目で unscorable)
                continue
            current = regen

        # based_on(index)→ 実 record id へ変換
        based_idx = [i for i in (current.get("based_on") or []) if isinstance(i, int)
                     and 0 <= i < len(internal_records)]
        based_ids = [internal_records[i]["id"] for i in based_idx if internal_records[i]["id"]]

        res = current.get("resolution") if isinstance(current.get("resolution"), dict) else {}
        try:
            conf = float(current.get("confidence"))
        except Exception:
            conf = 0.5
        # 下限は 0.05。**0.50 で床を張ってはいけない**——「起きないと思う」を表明できず、
        # 確度が0.60〜0.85の狭い帯に潰れていた(実測 標準偏差0.06=判別力ゼロ)。
        # Brier は確度の正しさを測るので、低く言えないことは一方的な損になる。
        conf = max(0.05, min(0.95, conf))
        try:
            base_rate = max(0.01, min(0.99, float(current.get("base_rate"))))
        except (TypeError, ValueError):
            base_rate = None

        # deadline は相対日数からコード側で JST 絶対日付に確定(+3〜+30日内のときのみ)
        dd = coerce_deadline_days(current)
        if isinstance(dd, int) and DEADLINE_MIN_DAYS <= dd <= DEADLINE_MAX_DAYS:
            deadline = (created_dt + timedelta(days=dd)).strftime("%Y-%m-%d")
        else:
            deadline = ""  # unscorable 側でのみ起こりうる(範囲外/欠落)

        h_seq += 1
        hid = date_str + "-h%02d" % h_seq
        obj = {
            "id": hid,
            "created_at": now_iso,
            "based_on": based_ids,
            "prose": str(current.get("prose") or "").strip(),
            "claim": str(current.get("claim") or "").strip(),
            "subject": str(current.get("subject") or "").strip(),
            "resolution": {
                "source": str(res.get("source") or "").strip(),
                "check_query": str(res.get("check_query") or "").strip(),
                "decider": str(res.get("decider") or "").strip(),
            },
            "deadline": deadline,
            "confidence": conf,
            # 外部視点(参照クラス予測)の記録。確度を「基準率からどうずらしたか」として残すと、
            # 後から『材料に酔って上振れさせる癖』が測れる(較正の材料になる)。
            "base_rate": base_rate,
            "base_rate_class": str(current.get("base_rate_class") or "").strip(),
            "confidence_why": str(current.get("confidence_why") or "").strip(),
            "counter": str(current.get("counter") or "").strip(),  # 反証条件(外れる最も強い理由)
            # 指標監視(I&W): 期日を待たず生死が分かる観測点と、その点灯履歴。任意=既存データと後方互換。
            "indicators": [
                {"sign": str(x.get("sign") or "").strip(), "dir": ("kill" if x.get("dir") == "kill" else "confirm")}
                for x in (current.get("indicators") or [])
                if isinstance(x, dict) and str(x.get("sign") or "").strip()][:3],
            "signals": [],
            "status": status,
            "resolved_at": None,
            "result": None,
            "evidence": None,
            "rejected": rejected,
            "model": model,
            "schema_version": SCHEMA_VERSION,
            "generator_ver": GENERATOR_VER,
        }
        dup_id, dup_sim = decision.duplicate_claim(obj["claim"], existing_hunches + new_hunches)
        if dup_id:
            print("hunch 重複のためスキップ: %s と類似 %.2f" % (dup_id, dup_sim))
            continue
        # 同じ主体に同じ日で複数賭けない。文面が違えば類似度では捕まらないが、
        # 中身は一つの賭けであることがある: 実際に初決着した2件は
        # 「Fireworksがルーティング用コードを公開する」「Fireworksがルーティング機能をGAする」で、
        # 類似度0.19(閾値0.58)をすり抜けて2件計上され、**一度の読み違いが打率とBrierに
        # 二重に効いた**(2件とも外れ / Brier 0.64)。独立でない賭けを独立として数えない。
        _subj = str(obj.get("subject") or "").strip().lower()
        if status == "pending" and _subj and any(
                str(h.get("subject") or "").strip().lower() == _subj
                and h.get("status") == "pending" for h in new_hunches):
            print("hunch 同一主体のためスキップ(相関した賭けを2件にしない): %s" % obj.get("subject"))
            continue
        if status == "pending" and sum(1 for h in new_hunches if h.get("status") == "pending") >= 2:
            print("hunch 上限のためスキップ: 採点対象は1日最大2件")
            continue
        if valid_hunch_schema(obj):
            new_hunches.append(obj)
        else:
            print("hunch スキーマ不正のため除外:", repr(obj)[:160])

    return new_records, new_hunches


# ---- 台帳ページ生成(記録・勘を別ページに) ------------------------------
# 記録層の副産物。サイト本体(index.html)とは別ファイルなので、ここが失敗しても
# ブリーフィング本体の生成・公開には一切影響しない。全フィールドを html.escape する。

CERT_JA = {"confirmed": "確定", "reported": "報道", "rumor": "噂"}
TIER_JA = {"primary": "一次", "secondary": "二次"}
STATUS_JA = {"pending": "判定待ち", "unscorable": "採点対象外"}


def _esc(v):
    return html.escape(str(v if v is not None else ""))


def _page_shell(title, subtitle, nav_html, body_html):
    return ("""<!DOCTYPE html><html lang="ja" class="no-js"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>""" + _esc(title) + """</title><style>""" + PAGE_CSS + """</style></head><body>
<div class="progress" aria-hidden="true"></div>
<main>
<header class="hd"><h1>◇ """ + _esc(title) + """</h1>
<div class="d">""" + _esc(subtitle) + """</div>
""" + nav_html + """</header>
""" + body_html + """
</main>
<script>
(function(){var root=document.documentElement;root.className="js";
var reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
var rev=document.querySelectorAll(".reveal");
if(!reduce&&"IntersectionObserver"in window){var io=new IntersectionObserver(function(es){
es.forEach(function(en){if(en.isIntersecting){en.target.classList.add("in");io.unobserve(en.target);}});},
{threshold:0.08,rootMargin:"0px 0px -6% 0px"});rev.forEach(function(el){io.observe(el);});}
else{rev.forEach(function(el){el.classList.add("in");});}})();
</script>
</body></html>""")


def render_records_page(records):
    sub = datetime.now(JST).strftime("%Y.%m.%d %H:%M") + " 更新 · 事実の台帳"
    nav = ('<nav class="nav"><a href="index.html">← ブリーフィング</a>'
           '<a href="hunches.html">勘の台帳</a><a href="threads.html">記憶の物語</a>'
           '<a href="ichite.html">今日の一手</a></nav>')
    # 同じ記事を毎日拾い直した分を読む側で落とす(実測36件中ユニーク13件。AP記事が7回並んでいた)。
    # 台帳の履歴そのものは改変しない — 表示だけを1件にまとめる。
    shown = memory.dedupe_by_url(records)
    if not shown:
        body = '<p class="empty">まだ記録がありません。</p>'
    else:
        cards = []
        for r in list(reversed(shown))[:RENDER_LIMIT]:  # 新しい順・上限(全件描画は必ず肥大する)
            c = r.get("certainty", "")
            src = r.get("source", {}) if isinstance(r.get("source"), dict) else {}
            url = src.get("url", "")
            date = (r.get("created_at", "") or "")[:10]
            bf = "本文取得済" if r.get("body_fetched") else "タイトルのみ"
            parts = ['<article class="card reveal">',
                     '<h3>' + _esc(r.get("headline", "")) + '</h3>',
                     '<div class="row">',
                     '<span class="badge b-' + _esc(c) + '">' + _esc(CERT_JA.get(c, c)) + '</span>',
                     '<span class="badge">' + _esc(TIER_JA.get(r.get("source_tier", ""), "")) + '情報源</span>',
                     '<span class="kv">' + _esc(date) + ' · ' + bf + '</span>',
                     '</div>',
                     '<p>' + _esc(r.get("what_happened", "")) + '</p>']
            if r.get("changed"):
                parts.append('<p class="kv"><b>何が変わったか</b> ' + _esc(r["changed"]) + '</p>')
            if r.get("background"):
                parts.append('<p class="kv"><b>背景</b> ' + _esc(r["background"]) + '</p>')
            if url:
                _src_name = str(src.get("name") or "ソース")
                _score = src.get("hn_score", 0)
                _score_text = (" · signal " + str(_score)) if _score else ""
                parts.append('<p><a href="' + _esc(url) + '">' + _esc(src.get("title") or "ソースを開く")
                             + '</a> <span class="m">' + _esc(_src_name + _score_text) + '</span></p>')
            parts.append('<p class="kv">' + _esc(r.get("id", "")) + '</p></article>')
            cards.append("".join(parts))
        _dup = len(records) - len(shown)
        _more = ('（全' + str(len(shown)) + '件中、新しい' + str(RENDER_LIMIT) + '件を表示）'
                 if len(shown) > RENDER_LIMIT else '(全' + str(len(shown)) + '件)')
        if _dup > 0:
            _more += '（同一記事の再掲' + str(_dup) + '件は除外）'
        body = ('<section><h2>記録の台帳 <span class="kv">' + _more + '</span></h2>'
                + "".join(cards) + '</section>')
    return _page_shell("記録の台帳", sub, nav, body)


RESULT_BADGE = {"hit": ("b-hit", "○ 的中"), "miss": ("b-miss", "× 外し"),
                "unclear": ("b-review", "— 判定不能")}


def _hunch_card(h, today):
    res = h.get("resolution", {}) if isinstance(h.get("resolution"), dict) else {}
    dl = h.get("deadline", "")
    drem = ""
    try:
        delta = (datetime.strptime(dl, "%Y-%m-%d").date() - today).days
        drem = ("・残り" + str(delta) + "日") if delta >= 0 else ("・期限超過" + str(-delta) + "日")
    except Exception:
        pass
    parts = ['<article class="card reveal">', '<h3>' + _esc(h.get("claim", "")) + '</h3>', '<div class="row">']
    result = h.get("result")
    if result in RESULT_BADGE:
        cls, lab = RESULT_BADGE[result]
        parts.append('<span class="badge ' + cls + '">' + lab + '</span>')
    elif h.get("needs_review"):
        parts.append('<span class="badge b-review">要確認</span>')
    elif h.get("alert"):
        # 死亡シグナル点灯。判定は期日に通常どおり行う(採点から外さない)
        parts.append('<span class="badge b-miss">危険信号</span>')
    else:
        parts.append('<span class="badge b-pending">判定待ち</span>')
    # 確度は「基準率をどうずらしたか」として見せる。数字だけだとどこから来たか分からない。
    _br = h.get("base_rate")
    if isinstance(_br, (int, float)):
        try:
            _c = float(h.get("confidence"))
            _arrow = "↑" if _c > _br else ("↓" if _c < _br else "→")
            parts.append('<span class="kv">基準率 %d%% %s 確度 %d%%</span>'
                         % (round(_br * 100), _arrow, round(_c * 100)))
        except (TypeError, ValueError):
            parts.append('<span class="kv">確度 ' + _esc(h.get("confidence", "")) + '</span>')
    else:
        parts.append('<span class="kv">確度 ' + _esc(h.get("confidence", "")) + '</span>')
    if dl:
        parts.append('<span class="kv deadline">期限 ' + _esc(dl) + drem + '</span>')
    parts.append('</div>')
    if h.get("prose"):
        parts.append('<p>' + _esc(h["prose"]) + '</p>')
    ev = h.get("evidence") if isinstance(h.get("evidence"), dict) else None
    if ev and ev.get("summary"):
        link = ' <a href="' + _esc(ev.get("url", "")) + '">証拠</a>' if ev.get("url") else ""
        parts.append('<p class="kv"><b>答え合わせ</b> ' + _esc(ev["summary"]) + link + '</p>')
    parts.append('<p class="kv"><b>主体</b> ' + _esc(h.get("subject", "")) + '</p>')
    if h.get("counter"):
        parts.append('<p class="kv"><b>外れるとすれば</b> ' + _esc(h["counter"]) + '</p>')
    inds = h.get("indicators") if isinstance(h.get("indicators"), list) else []
    if inds:
        parts.append('<p class="kv"><b>見張り</b> ' + "、".join(
            _esc(x.get("sign", "")) + ("（死亡）" if x.get("dir") == "kill" else "（確認）") for x in inds) + '</p>')
    sigs = h.get("signals") if isinstance(h.get("signals"), list) else []
    for s in sigs[-3:]:   # 点灯履歴(新しい方から数件)
        cls = "b-miss" if s.get("dir") == "kill" else "b-hit"
        lab = "⚠ 死亡シグナル" if s.get("dir") == "kill" else "🔥 確認シグナル"
        # 確度が動いたなら、いくつからいくつへ動いたかまで見せる(逐次ベイズ更新の跡)
        _mv = ""
        try:
            _b, _a = float(s.get("conf_before")), float(s.get("conf_after"))
            if abs(_a - _b) >= 0.005:
                _mv = " <b>確度 %d%%→%d%%</b>" % (round(_b * 100), round(_a * 100))
        except (TypeError, ValueError):
            pass
        parts.append('<p class="kv"><span class="badge ' + cls + '">' + lab + '</span> '
                     + _esc(s.get("date", "")) + " " + _esc(s.get("why", "") or s.get("sign", ""))
                     + _mv + '</p>')
    if h.get("base_rate_class"):
        parts.append('<p class="kv"><b>参照クラス</b> ' + _esc(h["base_rate_class"])
                     + ((' / <b>ずらした理由</b> ' + _esc(h["confidence_why"]))
                        if h.get("confidence_why") else "") + '</p>')
    if res.get("decider"):
        parts.append('<p class="kv"><b>的中条件</b> ' + _esc(res["decider"]) + '</p>')
    if res.get("source") or res.get("check_query"):
        parts.append('<p class="kv"><b>確認先</b> ' + _esc(res.get("source", ""))
                     + ' / <code>' + _esc(res.get("check_query", "")) + '</code></p>')
    based = h.get("based_on", []) if isinstance(h.get("based_on"), list) else []
    parts.append('<p class="kv"><b>根拠の記録</b> ' + _esc("、".join(based) or "—") + '</p>')
    parts.append('<p class="kv">' + _esc(h.get("id", "")) + '</p></article>')
    return "".join(parts)


def render_hunches_page(hunches):
    today = datetime.now(JST).date()
    sub = datetime.now(JST).strftime("%Y.%m.%d %H:%M") + " 更新 · 予測の台帳"
    nav = ('<nav class="nav"><a href="index.html">← ブリーフィング</a>'
           '<a href="records.html">記録の台帳</a><a href="threads.html">記憶の物語</a>'
           '<a href="ichite.html">今日の一手</a></nav>')
    st = memory.hit_stats(hunches)
    nd = memory.next_due(hunches, today)
    due_html = ""
    if nd:
        _dd, _rem = nd
        try:
            _md = datetime.strptime(_dd, "%Y-%m-%d")
            _mds = str(_md.month) + "/" + str(_md.day)
        except Exception:
            _mds = _dd
        due_html = ('<p class="kv"><b>次の決着</b> ' + _esc(_mds)
                    + '(あと' + str(_rem) + '日)</p>')
    if st["total"]:
        _br = memory.brier(hunches)
        # 指標の点灯で確度を更新するようになったので、通常の Brier は「途中で現実を見て
        # 直した後」の成績になる。当初の読みの成績と並べないと、後知恵込みの数字を
        # 予測成績として出すことになる。差が無い(更新が無かった)日は何も足さない。
        _br0 = memory.brier(hunches, key="initial_confidence")
        _br0_html = ""
        if (_br0["score"] is not None and _br["score"] is not None
                and abs(_br0["score"] - _br["score"]) >= 0.001):
            _br0_html = ('<p class="kv"><b>当初の確度でのBrier</b> ' + ("%.3f" % _br0["score"])
                         + '（途中の更新を入れない、作成日の読みだけの成績）</p>')
        if st["total"] < 20:
            rate = ('<p class="kv"><b>暫定成績</b> 的中' + str(st["hit"]) + ' / 外し' + str(st["miss"])
                    + '（n=' + str(st["total"]) + '。20件までは能力値として扱わない）</p>'
                    + (('<p class="kv"><b>参考Brier</b> ' + ("%.3f" % _br["score"])
                        + '（標本不足）</p>') if _br["score"] is not None else "") + _br0_html)
        else:
            rate = ('<p><span class="hitrate">的中率 ' + str(round(st["rate"] * 100)) + '%</span> '
                    '<span class="kv">(的中' + str(st["hit"]) + ' / 外し' + str(st["miss"])
                    + ' / 判定不能除く)</span></p>'
                    + (('<p class="kv"><b>Brier</b> ' + ("%.3f" % _br["score"]) + '（n=' + str(_br["n"])
                        + '・低いほど良い。常に50%と答えるだけなら 0.250）</p>') if _br["score"] is not None else "")
                    + _br0_html)
    else:
        rate = '<p class="kv">まだ答え合わせ前。的中率は期日到来分の決着後に出る。</p>'
    if not hunches:
        body = '<p class="empty">まだ予測がありません。</p>'
    else:
        resolved = [h for h in reversed(hunches) if h.get("status") == "resolved"][:RENDER_LIMIT]
        pending = [h for h in reversed(hunches) if h.get("status") != "resolved"][:RENDER_LIMIT]
        secs = ['<section><h2>打率 <span class="kv">(判定待ち ' + str(st["pending"]) + '件)</span></h2>' + rate + due_html + '</section>']
        if resolved:
            secs.append('<section><h2>答え合わせ済 <span class="kv">(' + str(len(resolved)) + '件)</span></h2>'
                        + "".join(_hunch_card(h, today) for h in resolved) + '</section>')
        secs.append('<section><h2>判定待ち <span class="kv">(' + str(len(pending)) + '件)</span></h2>'
                    + "".join(_hunch_card(h, today) for h in pending) + '</section>')
        body = "".join(secs)
    return _page_shell("勘の台帳", sub, nav, body)


def render_threads_page(records):
    """記憶の物語ページ。主体ごとの記録を時系列で束ね(memory.all_threads)、"線"を読者に見せる。
    決着待ち期間でも、参謀が何を追ってきたかの文脈を辿れるようにする。"""
    sub = datetime.now(JST).strftime("%Y.%m.%d %H:%M") + " 更新 · 記憶の物語"
    nav = ('<nav class="nav"><a href="index.html">← ブリーフィング</a>'
           '<a href="records.html">記録の台帳</a><a href="hunches.html">勘の台帳</a>'
           '<a href="ichite.html">今日の一手</a></nav>')
    threads = memory.all_threads(records)
    if not threads:
        body = ('<p class="empty">まだスレッドがありません。'
                '同じ主体が2回以上登場すると、ここで時系列の"線"になります。</p>')
    else:
        cards = []
        for subj, evs in threads:
            items = []
            for d, hl, url in evs:
                head = _esc(hl)
                if url:
                    head = '<a href="' + _esc(url) + '">' + head + '</a>'
                items.append('<li><span class="kv">' + _esc(d) + '</span> ' + head + '</li>')
            cards.append('<article class="card reveal"><h3>' + _esc(subj)
                         + ' <span class="kv">(' + str(len(evs)) + '件)</span></h3>'
                         + '<ul>' + "".join(items) + '</ul></article>')
        body = ('<section><h2>記憶の物語 <span class="kv">(' + str(len(threads))
                + 'スレッド)</span></h2>'
                '<p class="kv">参謀が追ってきた主体ごとの時系列。線がつながるほど、勘は文脈を持つ。</p>'
                + "".join(cards) + '</section>')
    return _page_shell("記憶の物語", sub, nav, body)


def build_ledger(records, hunches, limit=12):
    """参謀の記憶と成績を、アプリが1画面で読める形に畳む。

    同じ知識が records.html / hunches.html / threads.html という**別デザインの別ページ**に
    散っていて、司令室アプリからは切り離されていた。ページは残す(直リンク用)が、
    アプリはこの JSON を読んで同じ画面に統合する。純関数なのでテストできる。
    """
    today = datetime.now(JST).date()
    st = memory.hit_stats(hunches)
    br = memory.brier(hunches)
    br0 = memory.brier(hunches, key="initial_confidence")
    nd = memory.next_due(hunches, today)

    def _h(h):
        return {"id": h.get("id", ""), "claim": str(h.get("claim") or "")[:120],
                "subject": h.get("subject", ""), "deadline": h.get("deadline", ""),
                "confidence": h.get("confidence"),
                "initial_confidence": h.get("initial_confidence"),
                "result": h.get("result"), "status": h.get("status", ""),
                "alert": bool(h.get("alert")),
                "counter": str(h.get("counter") or "")[:160],
                "evidence": (h.get("evidence") or {}).get("summary", "") if isinstance(h.get("evidence"), dict) else "",
                "signals": [{"date": s.get("date", ""), "dir": s.get("dir", ""),
                             "why": str(s.get("why") or s.get("sign") or "")[:120]}
                            for s in (h.get("signals") or [])[-2:]]}

    decided = [_h(h) for h in reversed(hunches) if h.get("result") in ("hit", "miss")][:limit]
    pending = [_h(h) for h in sorted(
        [h for h in hunches if h.get("status") == "pending"],
        key=lambda x: str(x.get("deadline", "9999-99-99")))][:limit]
    uniq = memory.dedupe_by_url(records)     # 過去に溜まった重複で水増ししない
    return {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "score": {"hit": st["hit"], "miss": st["miss"], "total": st["total"],
                  "rate": st["rate"], "pending": st["pending"],
                  "brier": br["score"], "brier_n": br["n"],
                  "brier_initial": br0["score"],
                  # 世界の物差し。自分の数字がどの位置かを読者が判断できるようにする
                  # 比較できる基準だけを置く。常に50%と答える予測者の Brier は
                  # **問題セットに関係なく** 0.25 になる(採点式の性質)ので、うちの数字と
                  # 直接比べられる。一方 ForecastBench の 0.13 や超予測者の 0.02 は
                  # 別の問題セット・別の期間で測った値で、同じ物差しに並べると
                  # 「参謀は超予測者より下手」と読めてしまうが、その比較は成立しない。
                  # 憲法3章「出典や測定条件が異なるベンチマーク数値を、同じ物差しの
                  # ように並べない」に従って外部ベンチマークは載せない。
                  "baseline": {"always50": 0.25,
                               "always50_note": "常に50%と答えた場合の値。問題セットに依らず一定なので直接比較できる"}},
        "next_due": ({"date": nd[0], "days": nd[1]} if nd else None),
        "decided": decided, "pending": pending,
        "threads": [{"subject": s, "events": [{"date": d, "headline": hl, "url": u}
                                              for d, hl, u in evs[-4:]]}
                    for s, evs in memory.all_threads(records)[:6]],
        "records": [{"id": r.get("id", ""), "date": (r.get("created_at", "") or "")[:10],
                     "headline": str(r.get("headline") or "")[:120],
                     "certainty": r.get("certainty", ""),
                     "url": (r.get("source") or {}).get("url", "")}
                    for r in list(reversed(uniq))[:limit]],
        "record_count": len(uniq),
    }


def write_ledger_json(records, hunches):
    try:
        os.makedirs("docs", exist_ok=True)
        with open("docs/ledger.json", "w", encoding="utf-8") as f:
            json.dump(build_ledger(records, hunches), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("ledger.json 生成失敗(据え置き):", repr(e)[:160])


def render_pages():
    """現在の records.json / hunches.json から台帳ページを生成(冪等)。
    失敗しても記録・サイト本体を止めないよう握りつぶす。"""
    try:
        os.makedirs("docs", exist_ok=True)
        records = load_json_array(RECORDS_PATH)
        with open("docs/records.html", "w", encoding="utf-8") as f:
            f.write(render_records_page(records))
        with open("docs/hunches.html", "w", encoding="utf-8") as f:
            f.write(render_hunches_page(load_json_array(HUNCHES_PATH)))
        with open("docs/threads.html", "w", encoding="utf-8") as f:
            f.write(render_threads_page(records))
        write_ledger_json(records, load_json_array(HUNCHES_PATH))
        print("recorder: 台帳ページ(records.html / hunches.html / threads.html)を生成")
    except Exception as e:
        print("render_pages", repr(e)[:160])


# ---- メイン --------------------------------------------------------------

def main():
    now = datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")
    runs_path = os.path.join(RUNS_DIR, date_str + ".json")

    # 冪等: runs ファイルがあれば記録はスキップ。ただし台帳ページは現在のJSONから再生成
    # しておく(既存データと同一なら実質無変更=冪等)。
    if os.path.exists(runs_path):
        print("recorder: runs/" + date_str + ".json が存在。記録処理をスキップ")
        render_pages()
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(RUNS_DIR, exist_ok=True)
    for p in (RECORDS_PATH, HUNCHES_PATH):
        if not os.path.exists(p):
            dump_json(p, [])

    started_at = now.isoformat()

    # --- 記事収集 + 本文取得(分岐1) ---
    # サイト本体と同じ収集器を使う。表示用と採点用が別の世界を見る状態をやめる。
    items = fetch_all(limit=TOP_N * 3)
    seen, uniq = set(), []
    for a in items:
        k = (a.get("title") or "").lower().strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(a)
    uniq.sort(key=lambda a: a.get("points", 0), reverse=True)
    top = uniq[:TOP_N]

    fake_path = os.environ.get("RECORDER_FAKE_RESPONSE")  # テスト用シーム
    articles = []
    for a in top:
        if fake_path:                       # オフラインテスト時は本文取得もスキップ
            body, ok = "", False
        else:
            body, ok = fetch_body(a["url"])
        articles.append({"title": a.get("title") or "", "url": a.get("url") or "",
                         "hn_score": a.get("points", 0), "src": a.get("src", ""),
                         "tier": a.get("tier", 3),
                         "body": body, "body_fetched": ok})

    # --- LLM 生成(records+hunches 同時、1回・失敗時2回再試行) ---
    client = None
    gen = None
    if fake_path:
        try:
            with open(fake_path, encoding="utf-8") as f:
                fake = json.load(f)
            gen = call_records_hunches(None, articles, date_str, fake_response=fake)
            gen["_model"] = fake.get("_model", "fake-model")
            gen["_fake_regens"] = fake.get("_fake_regens", [])
        except Exception as e:
            print("recorder: fake response 読み込み失敗", e)
            gen = None
    else:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            print("recorder: GEMINI_API_KEY 未設定。記録をスキップ(runs未書き=再実行可)")
            return
        client = GeminiClient(api_key, call_limit=10)
        gen = call_records_hunches(client, articles, date_str)
        if gen is not None:
            gen["_model"] = client.last_model_version or "unknown"

    if not gen or not gen.get("records"):
        # records も作れないハード失敗: runs を書かず終了(=再dispatchで retry 可能)
        print("recorder: records 生成に失敗。runs 未書きで終了(次回リトライ)")
        return

    process._client = client  # 再生成で使う(fake時は None)

    existing_records = load_json_array(RECORDS_PATH)
    existing_hunches = load_json_array(HUNCHES_PATH)
    new_records, new_hunches = process(articles, gen, now, date_str,
                                       existing_records, existing_hunches)

    # --- 追記書き込み(既存は不変) ---
    if new_records:
        dump_json(RECORDS_PATH, existing_records + new_records)
    if new_hunches:
        dump_json(HUNCHES_PATH, existing_hunches + new_hunches)

    finished = datetime.now(JST).isoformat()
    status = "ok" if new_records else "empty"
    dump_json(runs_path, {
        "date": date_str,
        "started_at": started_at,
        "finished_at": finished,
        "record_ids": [r["id"] for r in new_records],
        "hunch_ids": [h["id"] for h in new_hunches],
        "status": status,
    })
    render_pages()  # 台帳ページを最新データで再生成
    print("recorder: done records=%d hunches=%d status=%s" % (
        len(new_records), len(new_hunches), status))


if __name__ == "__main__":
    main()
