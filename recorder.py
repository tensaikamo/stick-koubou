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

from common import GeminiClient, fetch_hn, fetch_tc, parse_json

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

# decider が観測手続きに落ちない曖昧表現(PHASE 3 の遮断キーワード・ヒューリスティック)
VAGUE_WORDS = ["話題", "注目", "バズ", "盛り上が", "期待", "騒がれ", "人気", "有名", "評判"]

PERSONA = """読者は個人で情報優位を作り先回りを狙う人物。断言型・根拠つき。
「確実だ」「間違いない」等の断定語は使わない。企業向け提言・一般論は書かない。"""


# ---- 入出力ユーティリティ ------------------------------------------------

def load_json_array(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except Exception as e:
        print("load_json_array", path, e)
        return []


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


def fetch_body(url):
    """記事本文を best-effort で取得。失敗は握りつぶし ("", False) を返す。
    ここでの失敗が記録処理・サイト生成を止めることは絶対にない。"""
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype and ctype:
            return "", False
        text = strip_html(r.text)
        return text[:BODY_LIMIT], bool(text.strip())
    except Exception as e:
        print("fetch_body", url, repr(e)[:120])
        return "", False


# ---- LLM 生成 ------------------------------------------------------------

def build_prompt(articles, today_str):
    lines = []
    for i, a in enumerate(articles):
        body = a["body"] if a["body_fetched"] else "(本文取得に失敗。タイトルのみ)"
        lines.append(
            "[記事 " + str(i) + "] " + a["title"]
            + " (HN " + str(a["hn_score"]) + "pt, " + a["src"] + ")\n"
            + "URL: " + a["url"] + "\n"
            + "body_fetched: " + ("true" if a["body_fetched"] else "false") + "\n"
            + "本文抜粋: " + (body[:1500] if body else "") + "\n")
    material = "\n".join(lines)
    return (PERSONA + "\n\n"
        "本日はJST(日本標準時)で " + today_str + " である。予測期限はこの日付を基準に相対日数で示せ。\n"
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
        '        "source": "判定時に見る場所(公式サイト/報道/指標名)",\n'
        '        "check_query": "英語のみ2〜4語",\n'
        '        "decider": "何が満たされたら的中か。閾値または具体的事象を明記。『話題になる』等の曖昧表現は禁止"\n'
        '      },\n'
        '      "deadline_days": 判定期限までの日数(今日からの相対、3〜30の整数。絶対日付は書くな),\n'
        '      "confidence": 0.50〜0.95 の数値\n'
        '    }\n'
        '  ]\n'
        '}\n'
        "record は重要な記事のみ(最大" + str(TOP_N) + "件)。hunch は 1〜3件、必ず record を根拠にせよ。\n\n"
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
        "不合格を避ける要件: subject(固有名)・resolution.decider(観測可能な閾値/事象、"
        "『話題になる』等の曖昧語禁止)・deadline_days(今日からの相対日数、3〜30の整数。絶対日付は書くな)を"
        "必ず満たし、based_on は下記 index を1件以上参照(rumor のみを根拠にしない)。\n"
        "次のJSONオブジェクトだけを返せ:\n"
        '{"based_on":[index...],"prose":"...","claim":"...","subject":"...",'
        '"resolution":{"source":"...","check_query":"英語2〜4語","decider":"..."},'
        '"deadline_days":3〜30の整数,"confidence":0.5〜0.95}\n\n'
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
    dd = coerce_deadline_days(h)
    if dd is None:
        return "deadline_days が欠落または整数でない"
    if any(w in decider for w in VAGUE_WORDS):
        return "decider が観測手続きに落ちない曖昧表現"
    if not (DEADLINE_MIN_DAYS <= dd <= DEADLINE_MAX_DAYS):
        return "deadline_days が+3〜+30日の範囲外"
    # based_on: 1件以上・実在index・rumorのみは不可
    idxs = [i for i in based if isinstance(i, int) and 0 <= i < len(records)]
    if not idxs:
        return "based_on が空、または実在 record を参照していない"
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
    internal_records = []   # 検証・based_on解決用の内部表現(certainty含む)
    new_records = []
    for rec in gen.get("records", []):
        if not isinstance(rec, dict):
            continue
        ai = rec.get("article_index")
        art = articles[ai] if isinstance(ai, int) and 0 <= ai < len(articles) else None
        certainty = rec.get("certainty") if rec.get("certainty") in ("confirmed", "reported", "rumor") else "reported"
        source_tier = rec.get("source_tier") if rec.get("source_tier") in ("primary", "secondary") else "secondary"
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
            },
            "body_fetched": bool(art["body_fetched"]) if art else False,
            "model": model,
            "related_ids": [],
        }
        if valid_record_schema(obj):
            internal_records.append({"id": rid, "certainty": certainty,
                                     "headline": obj["headline"]})
            new_records.append(obj)
        else:
            print("record スキーマ不正のため除外:", repr(obj)[:160])

    # --- hunches: 検証ゲート + 再生成 + 採番 ---
    new_hunches = []
    fakes = gen.get("_fake_regens", [])  # テスト用: 再生成応答の順次差し込み
    for hraw in gen.get("hunches", []):
        if not isinstance(hraw, dict):
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
            regen = regen_hunch(getattr(process, "_client", None), new_records, reason,
                                date_str, fake_response=fake)
            if not isinstance(regen, dict):
                # 再生成できなければ現案のまま次周(3回目で unscorable)
                continue
            current = regen

        # based_on(index)→ 実 record id へ変換
        based_idx = [i for i in (current.get("based_on") or []) if isinstance(i, int)
                     and 0 <= i < len(internal_records)]
        based_ids = [internal_records[i]["id"] for i in based_idx]

        res = current.get("resolution") if isinstance(current.get("resolution"), dict) else {}
        try:
            conf = float(current.get("confidence"))
        except Exception:
            conf = 0.5
        conf = max(0.50, min(0.95, conf))

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
            "status": status,
            "resolved_at": None,
            "result": None,
            "evidence": None,
            "rejected": rejected,
            "model": model,
            "schema_version": SCHEMA_VERSION,
            "generator_ver": GENERATOR_VER,
        }
        if valid_hunch_schema(obj):
            new_hunches.append(obj)
        else:
            print("hunch スキーマ不正のため除外:", repr(obj)[:160])

    return new_records, new_hunches


# ---- メイン --------------------------------------------------------------

def main():
    now = datetime.now(JST)
    date_str = now.strftime("%Y-%m-%d")
    runs_path = os.path.join(RUNS_DIR, date_str + ".json")

    # 冪等: runs ファイルがあれば全スキップ(id走査はしない)
    if os.path.exists(runs_path):
        print("recorder: runs/" + date_str + ".json が存在。記録処理をスキップ")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(RUNS_DIR, exist_ok=True)
    for p in (RECORDS_PATH, HUNCHES_PATH):
        if not os.path.exists(p):
            dump_json(p, [])

    started_at = now.isoformat()

    # --- 記事収集 + 本文取得(分岐1) ---
    items = fetch_hn() + fetch_tc()
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
    print("recorder: done records=%d hunches=%d status=%s" % (
        len(new_records), len(new_hunches), status))


if __name__ == "__main__":
    main()
