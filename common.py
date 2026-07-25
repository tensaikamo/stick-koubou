"""サイト生成(sanbo.py)と予測記録層(recorder.py)で共有するヘルパ群。
標準ライブラリのみに依存する(依存追加は recorder.py 側の requests のみ)。"""
import os, json, re, random, urllib.request, urllib.parse, urllib.error, time
import xml.etree.ElementTree as ET

# 無料枠は Flash 系のみ(Pro は2026-04以降 無料枠外、gemini-2.0/2.5系は退役・退役予定)。
# 常に現行の flash を指す公式エイリアス gemini-flash-latest を第一候補、別容量プールの
# flash-lite をフォールバックにする。退役済みの固定バージョン名は候補から外す(429の無駄撃ち防止)。
MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]

# 過負荷(429/503)時の待機秒(指数+ジッタ)。同一モデルでこの回数試す。無料枠の一時的な
# 過負荷は数十秒で解けることが多く、当日ブリーフィングの生成失敗を実質的に減らす。
BACKOFFS = [0, 10, 30]


def http(url, data=None, headers=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---- 情報源の層 ---------------------------------------------------------
# T1=一次情報・発表前の兆候(誰も見ていない) / T2=実勢(数字で分かる) / T3=二次(空気の確認)。
# 「先回り」を名乗る以上、話題化した二次情報だけを読む構造から抜ける。
# 各フェッチャは失敗時に [] を返す(1つ落ちても他が生きる)。全て無料・APIキー不要。
FEED_TIMEOUT = 12  # フィードは短く切る(1つ遅くても全体を止めない)


def _feed_items(raw, limit):
    """RSS/Atom いずれの形でも (タイトル, リンク) を抜く。名前空間は無視する。"""
    out = []
    root = ET.fromstring(raw)
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title, link = "", ""
        for c in el:
            t = c.tag.split("}")[-1]
            if t == "title" and not title:
                title = " ".join((c.text or "").split())
            elif t == "link" and not link:
                link = (c.get("href") or c.text or "").strip()
        if title:
            out.append((title, link))
        if len(out) >= limit:
            break
    return out


def _art(title, url, src, tier, meta="", points=0):
    return {"title": title, "url": url, "meta": meta, "src": src, "points": points, "tier": tier}


def _feed_source(url, src, tier, limit=6, meta=""):
    try:
        return [_art(t, u, src, tier, meta) for t, u in _feed_items(http(url, timeout=FEED_TIMEOUT), limit)]
    except Exception as e:
        print("feed", src, repr(e)[:90])
        return []


def fetch_arxiv():
    """arXiv 新着(cs.AI/cs.CL)。研究は製品の6〜18ヶ月先を走る=未到達の源泉。"""
    return _feed_source(
        "http://export.arxiv.org/api/query?search_query=cat:cs.AI+OR+cat:cs.CL"
        "&sortBy=submittedDate&sortOrder=descending&max_results=6", "arXiv", 1, 6)


BLOG_FEEDS = [
    ("https://openai.com/blog/rss.xml", "OpenAI公式"),
    ("https://deepmind.google/blog/rss.xml", "DeepMind公式"),
    ("https://huggingface.co/blog/feed.xml", "HF公式"),
    ("https://blog.google/technology/ai/rss/", "Google AI公式"),
    ("https://qwenlm.github.io/blog/index.xml", "Qwen公式"),      # 中国勢=日本語圏で最も未到達
    ("https://blog.mozilla.ai/rss/", "Mozilla.ai"),
]


def fetch_blogs():
    """各社公式ブログ(一次情報)。中国勢を含めるのは日本語圏で最も遅れる領域だから。"""
    out = []
    for url, name in BLOG_FEEDS:
        out += _feed_source(url, name, 1, 2)
    return out


GH_REPOS = ["vllm-project/vllm", "ggml-org/llama.cpp", "ollama/ollama", "huggingface/transformers"]


def fetch_gh():
    """主要リポの Releases。実装は公式発表より先に出ることが多い。"""
    out = []
    for r in GH_REPOS:
        out += _feed_source("https://github.com/" + r + "/releases.atom", "GH:" + r.split("/")[-1], 1, 2)
    return out


def fetch_pkg():
    """公式SDKの最新リリース。新機能のフラグは発表前にSDKへ現れることがある。
    RSSのタイトルはバージョン番号だけなので、パッケージ名を補って情報にする。"""
    out = []
    for p in ["openai", "anthropic", "google-genai"]:
        for a in _feed_source("https://pypi.org/rss/project/" + p + "/releases.xml",
                              "PyPI:" + p, 1, 1, meta="SDK"):
            a["title"] = p + " Python SDK v" + a["title"] + " リリース"
            out.append(a)
    return out


JOB_BOARDS = ["anthropic", "xai"]  # 公開Greenhouseボード(存在しないものは404で静かにスキップ)


def fetch_jobs():
    """公開求人ボード。求人は企業戦略の最速の先行指標(この職種の募集開始=この方向に賭けた)。
    公開日の新しい順に取る=「今この会社が新たに賭けた領域」が見える。"""
    out = []
    for b in JOB_BOARDS:
        try:
            d = json.loads(http("https://boards-api.greenhouse.io/v1/boards/" + b + "/jobs",
                                timeout=FEED_TIMEOUT).decode())
            jobs = [j for j in d.get("jobs", []) if j.get("title")]
            jobs.sort(key=lambda j: str(j.get("first_published") or j.get("updated_at") or ""), reverse=True)
            for j in jobs[:3]:
                loc = ((j.get("location") or {}).get("name") or "")
                out.append(_art("[新規求人] " + b + ": " + str(j["title"]) + (" (" + loc + ")" if loc else ""),
                                j.get("absolute_url", ""), "求人:" + b, 1,
                                str(j.get("first_published") or "")[:10]))
        except Exception as e:
            print("jobs", b, repr(e)[:90])
    return out


# Federal Register は全文一致のため無関係文書(漁業委員会等)が混ざる。表題でAI関連に絞る。
_AI_TITLE = re.compile(r"(?i)artificial intelligence|\bAI\b|machine learning|frontier model|compute")


def fetch_gov():
    """Federal Register(米国の規則・大統領令)。一次かつ日本語圏未到達度が高い。
    表題にAI語が無いものはノイズなので落とす(0件なら0件でよい=雑音を混ぜない)。"""
    try:
        d = json.loads(http("https://www.federalregister.gov/api/v1/documents.json?"
                            "conditions%5Bterm%5D=%22artificial+intelligence%22&order=newest&per_page=20"
                            "&fields%5B%5D=title&fields%5B%5D=html_url&fields%5B%5D=publication_date",
                            timeout=FEED_TIMEOUT).decode())
        out = []
        for x in d.get("results", []):
            t = str(x.get("title", ""))
            if _AI_TITLE.search(t):
                out.append(_art("[米規制] " + t, x.get("html_url", ""), "FederalRegister", 1,
                                str(x.get("publication_date", ""))))
            if len(out) >= 3:
                break
        return out
    except Exception as e:
        print("gov", repr(e)[:90])
        return []


def fetch_sec():
    """SEC EDGAR 全文検索。8-K/S-1 等で資金・提携が一次で出る(UA制限時は静かにスキップ)。"""
    try:
        d = json.loads(http('https://efts.sec.gov/LATEST/search-index?q=%22artificial+intelligence%22'
                            '&forms=8-K&dateRange=custom', timeout=FEED_TIMEOUT,
                            headers={"User-Agent": "sanbo-research contact@example.com"}).decode())
        hits = ((d.get("hits") or {}).get("hits") or [])[:5]
        out = []
        for h in hits:
            s = h.get("_source") or {}
            name = (s.get("display_names") or [""])[0]
            out.append(_art("[SEC 8-K] " + str(name), "https://www.sec.gov/edgar/search/#/q=artificial%20intelligence",
                            "SEC", 1, str(s.get("file_date", ""))))
        return out
    except Exception as e:
        print("sec", repr(e)[:90])
        return []


def fetch_hf():
    """Hugging Face: 日次論文(キュレーション済み)とトレンドモデル(オープン側の実勢)。"""
    out = []
    try:
        d = json.loads(http("https://huggingface.co/api/daily_papers?limit=6", timeout=FEED_TIMEOUT).decode())
        for x in d[:6]:
            p = x.get("paper") or {}
            t = p.get("title") or x.get("title") or ""
            if t:
                out.append(_art("[論文] " + t, "https://huggingface.co/papers/" + str(p.get("id", "")),
                                "HF日次論文", 2, str(p.get("upvotes", "")) + "up", p.get("upvotes", 0) or 0))
    except Exception as e:
        print("hf papers", repr(e)[:90])
    try:
        d = json.loads(http("https://huggingface.co/api/models?sort=trendingScore&limit=6",
                            timeout=FEED_TIMEOUT).decode())
        for m in d[:6]:
            mid = m.get("id") or m.get("modelId") or ""
            if mid:
                out.append(_art("[HFトレンド] " + mid, "https://huggingface.co/" + mid, "HFトレンド", 2,
                                str(m.get("downloads", 0)) + "DL", m.get("likes", 0) or 0))
    except Exception as e:
        print("hf models", repr(e)[:90])
    return out


def fetch_reddit():
    """r/LocalLLaMA。オープンモデル実勢の最前線(403等は静かにスキップ)。"""
    try:
        d = json.loads(http("https://www.reddit.com/r/LocalLLaMA/top.json?t=day&limit=8",
                            timeout=FEED_TIMEOUT).decode())
        out = []
        for c in (d.get("data") or {}).get("children", [])[:8]:
            x = c.get("data") or {}
            if x.get("title"):
                out.append(_art(x["title"], "https://reddit.com" + str(x.get("permalink", "")),
                                "r/LocalLLaMA", 2, str(x.get("ups", 0)) + "up", x.get("ups", 0) or 0))
        return out
    except Exception as e:
        print("reddit", repr(e)[:90])
        return []


def _key_terms(q):
    """固有名詞らしき語(英数4文字以上・カタカナ4文字以上)を抜く。日本語ヒットの照合に使う。"""
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9.\-]{3,}|[ァ-ヴー]{4,}", q or "")
            if w.lower() not in ("that", "with", "this", "from", "have", "する", "ため")]


def fetch_jp_hits(query, limit=3):
    """Google News(日本語)で当該話題の日本語記事を引く。「未上陸」をLLMの主観でなく実測にする。
    固有名詞が見出しに含まれるものだけを数える(緩い全文一致で無関係記事を「上陸済み」と誤判定しないため)。
    戻り: 日本語見出しのリスト(空=日本語圏でほぼ未到達)。"""
    try:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
               + "&hl=ja&gl=JP&ceid=JP:ja")
        titles = [t for t, _ in _feed_items(http(url, timeout=FEED_TIMEOUT), 10)]
        terms = _key_terms(query)
        if terms:  # 固有名詞があるなら、それを含む見出しだけを本物のヒットとする
            titles = [t for t in titles if any(w.lower() in t.lower() for w in terms)]
        return titles[:limit]
    except Exception as e:
        print("jp_hits", repr(e)[:90])
        return []


# ---- 時間優位: 差分検知 -------------------------------------------------
# 「発表」ではなく「静かに変わったもの」を捕まえる。OpenRouter のモデル一覧は全社のモデル・
# 価格・退役予定日を1本で見られるため、値下げ/新モデル/非推奨化が公式発表より先に現れる。
WATCH_PATH = os.path.join("data", "watch.json")
# 差分として報じる価値のある主要ラボ(無名の小型モデル追加はノイズなので除く)
MAJOR_LABS = ("anthropic/", "openai/", "google/", "meta-llama/", "mistralai/", "deepseek/",
              "qwen/", "x-ai/", "moonshotai/", "amazon/", "microsoft/", "nvidia/")


def watch_fetch():
    """監視対象の現在値を {model_id: {p,c,ctx,exp,name}} で返す。失敗時は {}。"""
    try:
        d = json.loads(http("https://openrouter.ai/api/v1/models", timeout=FEED_TIMEOUT).decode())
    except Exception as e:
        print("watch_fetch", repr(e)[:90])
        return {}
    out = {}
    for m in d.get("data") or []:
        mid = m.get("id")
        if not mid:
            continue
        p = m.get("pricing") or {}
        out[mid] = {"p": str(p.get("prompt", "")), "c": str(p.get("completion", "")),
                    "ctx": m.get("context_length"), "exp": str(m.get("expiration_date") or ""),
                    "name": str(m.get("name") or mid)}
    return out


def _price_move(a, b):
    """価格の増減を「値下げ/値上げ」の日本語にする。数値化できなければ None。"""
    try:
        x, y = float(a), float(b)
    except Exception:
        return None
    if x == y:
        return None
    if x == 0:
        return "有料化"
    pct = (y - x) / x * 100
    if abs(pct) < 1:
        return None
    return ("値下げ %.0f%%" % -pct) if y < x else ("値上げ %.0f%%" % pct)


def watch_diff(prev, cur, limit=6):
    """前回スナップショットとの差分を記事形式で返す。優先度: 退役予告 > 価格変更 > 新モデル > 消滅。
    初回(prev が空)は何も報じない(全件を「新着」と誤報しないため)。"""
    if not prev or not cur:
        return []
    exp_, price_, add_, del_ = [], [], [], []
    for mid, c in cur.items():
        p = prev.get(mid)
        if not p:
            if mid.startswith(MAJOR_LABS):
                add_.append(_art("[差分] 新モデルが登場: " + c["name"] + " (" + mid + ")",
                                 "https://openrouter.ai/models", "差分:新モデル", 1, "静かな変化"))
            continue
        if c["exp"] and c["exp"] != p.get("exp", ""):
            exp_.append(_art("[差分] 退役予定日が設定された: " + c["name"] + " → " + c["exp"],
                             "https://openrouter.ai/models", "差分:退役", 1, "静かな変化"))
        mv = _price_move(p.get("p", ""), c["p"]) or _price_move(p.get("c", ""), c["c"])
        if mv:
            price_.append(_art("[差分] 価格が動いた: " + c["name"] + " " + mv,
                               "https://openrouter.ai/models", "差分:価格", 1, "静かな変化"))
    for mid, p in prev.items():
        if mid not in cur and mid.startswith(MAJOR_LABS):
            del_.append(_art("[差分] 提供が終了した: " + p.get("name", mid),
                             "https://openrouter.ai/models", "差分:終了", 1, "静かな変化"))
    return (exp_ + price_ + add_ + del_)[:limit]


def watch_step(limit=6):
    """前回スナップショットを読み→現在値と差分→スナップショットを更新し、差分記事を返す。
    失敗しても [] を返すだけ(サイト生成を止めない)。"""
    prev = {}
    try:
        with open(WATCH_PATH, encoding="utf-8") as f:
            prev = json.load(f) or {}
    except Exception:
        pass
    cur = watch_fetch()
    if not cur:
        return []
    diffs = watch_diff(prev, cur, limit)
    try:
        os.makedirs(os.path.dirname(WATCH_PATH) or ".", exist_ok=True)
        with open(WATCH_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
        print("watch: %d件を記録(前回 %d件) / 差分 %d件" % (len(cur), len(prev), len(diffs)))
    except Exception as e:
        print("watch save", repr(e)[:90])
    return diffs


def _sources():
    # fetch_hn/fetch_tc は下方で定義されるため、呼び出し時に解決する
    return [("arxiv", fetch_arxiv), ("blogs", fetch_blogs), ("gh", fetch_gh), ("pkg", fetch_pkg),
            ("jobs", fetch_jobs), ("gov", fetch_gov), ("sec", fetch_sec),
            ("hf", fetch_hf), ("hn", fetch_hn), ("tc", fetch_tc)]

# 1ソースが候補枠を独占しないための上限(層ごと)。T1を優先しつつ、T3(空気)も必ず残す。
SRC_CAP = {1: 3, 2: 4, 3: 14}


def fetch_all(limit=56):
    """全ソースを並列取得し、重複除去→ソース毎に上限→T1優先で並べて候補を返す。
    1つが遅くても・落ちても全体は止まらない(各フェッチャは失敗時 [])。"""
    import concurrent.futures
    got = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(f): n for n, f in _sources()}
        try:
            for fu in concurrent.futures.as_completed(futs, timeout=90):
                n = futs[fu]
                try:
                    r = fu.result() or []
                    print("source %s: %d件" % (n, len(r)))
                    got += r
                except Exception as e:
                    print("source", n, repr(e)[:90])
        except Exception as e:  # 全体タイムアウト時も取れた分で続行
            print("fetch_all timeout", repr(e)[:90])
    seen, per, out = set(), {}, []
    for a in got:
        k = (a.get("title") or "").lower().strip()
        if not k or k in seen:
            continue
        s = a.get("src", "")
        cap = SRC_CAP.get(a.get("tier", 3), 3)
        if per.get(s, 0) >= cap:      # 1ソースの独占を防ぐ(T1の雑多な項目でT3が消える事故の防止)
            continue
        seen.add(k)
        per[s] = per.get(s, 0) + 1
        out.append(a)
    out.sort(key=lambda a: (a.get("tier", 3), -(a.get("points", 0) or 0)))
    return out[:limit]


def fetch_hn():
    out = []
    for q in ["AI", "OpenAI", "Anthropic", "Gemini", "LLM"]:
        try:
            url = ("https://hn.algolia.com/api/v1/search_by_date?query=" + urllib.parse.quote(q)
                   + "&tags=story&numericFilters=" + urllib.parse.quote("points>60") + "&hitsPerPage=8")
            d = json.loads(http(url).decode())
            for h in d.get("hits", []):
                out.append({"title": h.get("title") or "",
                            "url": h.get("url") or "https://news.ycombinator.com/item?id=" + str(h.get("objectID")),
                            "meta": str(h.get("points", 0)) + "pt", "src": "HN",
                            "points": h.get("points", 0)})
        except Exception as e:
            print("hn", e)
    return out


def fetch_tc():
    try:
        raw = http("https://techcrunch.com/category/artificial-intelligence/feed/")
        root = ET.fromstring(raw)
        return [{"title": i.findtext("title") or "", "url": i.findtext("link") or "",
                 "meta": "", "src": "TechCrunch", "points": 0} for i in root.iter("item")][:15]
    except Exception as e:
        print("tc", e)
        return []


class GeminiClient:
    """モデルフォールバック + 過負荷再試行 + API呼び出し回数ガードを持つ Gemini REST クライアント。
    キーは呼び出し側から受け取る(平文でログ出力しない)。"""

    def __init__(self, api_key, call_limit=10):
        self.api_key = api_key
        self.call_limit = call_limit
        self.calls = 0
        self._model_ok = []
        self._grounding_tools = None  # 通った google_search ツール指定形式のキャッシュ
        self._grounding_disabled = False  # 全形式400=grounding未対応と判明したら以後スキップ
        self.last_grounding_urls = []
        self.last_model_version = None  # 直近レスポンスの実モデルID(APIレスポンス由来)

    def _request(self, payload):
        """モデルフォールバック+過負荷バックオフでリクエストし、生の応答dictを返す。
        実応答モデルIDは last_model_version に保存。generate / generate_grounded の共通土台。"""
        body = json.dumps(payload).encode()
        last = None
        for m in (self._model_ok or MODELS):
            url = "https://generativelanguage.googleapis.com/v1beta/models/" + m + ":generateContent"
            # 429/503(一時的な過負荷)は指数バックオフ+ジッタで同モデルを複数回試す。
            for i, wait in enumerate(BACKOFFS):
                if wait:
                    time.sleep(wait + random.uniform(0, wait * 0.3))
                if self.calls >= self.call_limit:
                    raise RuntimeError("API呼び出し上限(" + str(self.call_limit) + "回/実行)に到達")
                self.calls += 1
                try:
                    d = json.loads(http(url, data=body,
                                        headers={"Content-Type": "application/json",
                                                 "x-goog-api-key": self.api_key}).decode())
                    self._model_ok[:] = [m]
                    self.last_model_version = d.get("modelVersion") or m
                    return d
                except urllib.error.HTTPError as e:
                    last = e
                    if e.code in (429, 503):
                        print("model", m, "-> HTTP", e.code, "(過負荷) retry", i + 1, "/", len(BACKOFFS))
                        continue
                    if e.code != 404:
                        raise
                    print("model", m, "-> 404, trying next")
                    break
        if getattr(last, "code", None) == 404:
            # 全候補が404: 利用可能なflash系モデル名を診断出力(モデル名のみ。キーは出力しない)
            try:
                d = json.loads(http("https://generativelanguage.googleapis.com/v1beta/models",
                                    headers={"x-goog-api-key": self.api_key}).decode())
                print("available flash models:",
                      [mm.get("name") for mm in d.get("models", []) if "flash" in (mm.get("name") or "")])
            except Exception as e2:
                print("listmodels", e2)
        raise last

    def generate(self, prompt, response_schema=None):
        """プロンプトを投げ、生成テキストを返す。response_schema を渡すと構造化出力
        (JSON準拠保証)を要求する(任意・後方互換)。"""
        cfg = {"responseMimeType": "application/json"}
        if response_schema is not None:
            cfg["responseSchema"] = response_schema
        d = self._request({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg})
        return d["candidates"][0]["content"]["parts"][0]["text"]

    # google_search ツールの指定形式は API 版で揺れる(現行 {"google_search":{}} / 別表記
    # {"type":"google_search"})。400 が返ったら別形式に切替し、通った形式をキャッシュする。
    GROUNDING_TOOLS = [{"google_search": {}}, {"type": "google_search"}]

    def generate_grounded(self, prompt):
        """Google検索グラウンディング付き生成。実Web検索の根拠付きでテキストを返し、
        出典URLを self.last_grounding_urls に格納する。grounding と responseSchema/JSON強制は
        併用不可のため、ここでは付けない(呼び出し側が末尾JSONを parse する)。"""
        if self._grounding_disabled:  # 既に未対応と判明 → 無駄試行せず即フォールバックさせる
            raise RuntimeError("grounding unavailable (cached)")
        self.last_grounding_urls = []
        candidates = [self._grounding_tools] if self._grounding_tools else self.GROUNDING_TOOLS
        last = None
        for tool in candidates:
            try:
                d = self._request({"contents": [{"parts": [{"text": prompt}]}], "tools": [tool]})
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 400:  # ツール指定形式が不正 → 別形式を試す
                    print("grounding tools 形式を切替(HTTP 400)")
                    continue
                raise
            self._grounding_tools = tool  # 通った形式をキャッシュ
            cand = (d.get("candidates") or [{}])[0]
            gm = cand.get("groundingMetadata") or {}
            for ch in gm.get("groundingChunks", []):
                w = ch.get("web") or {}
                if w.get("uri"):
                    self.last_grounding_urls.append({"title": w.get("title", ""), "url": w["uri"]})
            parts = (cand.get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts)
        # 全形式が400 = この環境では grounding 未対応。以後はスキップして HN 判定に委ねる。
        self._grounding_disabled = True
        raise last
        raise last


# サイト(index)と台帳(records/hunches)ページで共有するCSS。3ページで見た目を統一する。
PAGE_CSS = """:root{--bg:#0b0f14;--fg:#e6edf4;--teal:#5fd7c8;--gold:#e3b366;--line:#202b3a;--dim:#8698a9;--muted:#647688}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{background:radial-gradient(1200px 600px at 50% -10%,#0f1720 0%,var(--bg) 60%) no-repeat,var(--bg);
color:var(--fg);font-family:system-ui,'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;
margin:0;padding:28px 20px 80px;line-height:1.95;font-size:clamp(16px,3.9vw,18px);
-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
main{max-width:min(680px,94vw);margin:0 auto}
.hd{margin-bottom:36px}
h1{font-size:clamp(18px,4.6vw,22px);letter-spacing:.14em;color:var(--teal);font-weight:600;margin:0;
text-wrap:balance;text-shadow:0 0 18px rgba(95,215,200,.35)}
.d{color:var(--dim);font-size:13px;letter-spacing:.1em;margin-top:10px}
h2{font-size:clamp(19px,4.8vw,23px);letter-spacing:.02em;color:var(--gold);
border-bottom:1px solid var(--line);text-wrap:balance;
padding-bottom:12px;margin-top:0;font-weight:700;position:relative}
h2::after{content:"";position:absolute;left:0;bottom:-1px;width:56px;height:2px;
background:linear-gradient(90deg,var(--gold),transparent)}
section{margin-top:52px}
p{font-size:clamp(15.5px,4vw,17.5px);text-wrap:pretty}
.lb{color:#04121a;font-size:13px;letter-spacing:.12em;border-radius:6px;
padding:3px 12px;margin-right:12px;white-space:nowrap;font-weight:700;display:inline-block;
transform:translateY(-2px);background:linear-gradient(180deg,#7fe6d8,#41b6a7);
box-shadow:0 2px 10px rgba(95,215,200,.25)}
.mj{border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:18px 0;
background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,0))}
.mjt{font-size:16px;color:var(--fg)}
.mj p{font-size:15.5px;margin:8px 0}
.mjw{color:#9cc2df}
t{border-bottom:1px dotted var(--teal);cursor:pointer;transition:color .15s,border-color .15s;
-webkit-tap-highlight-color:transparent}
t:active,t:hover{color:var(--teal);border-bottom-style:solid}
.tip{position:fixed;margin:0;z-index:50;background:#101a26;border:1px solid #2b4a58;
border-radius:10px;padding:12px 15px;font-size:14.5px;line-height:1.75;color:#cfe0ec;
max-width:calc(100vw - 24px);box-shadow:0 10px 34px rgba(0,0,0,.55);
opacity:0;transform:translateY(-6px) scale(.98);transition:opacity .22s ease,transform .22s ease}
.tip.show{opacity:1;transform:none}
[popover].tip{inset:unset}
[popover].tip:popover-open{opacity:1;transform:none}
@starting-style{[popover].tip:popover-open{opacity:0;transform:translateY(-6px) scale(.98)}}
ul{padding-left:0;list-style:none}
li{margin:16px 0;font-size:16px}
a{color:#9cc2df;text-decoration:none}
li a{transition:color .15s}
li a:hover{color:var(--teal)}
.m{color:var(--dim);font-size:13px;margin-left:6px}
.progress{position:fixed;top:0;left:0;height:2px;width:100%;transform:scaleX(0);
transform-origin:0 50%;background:linear-gradient(90deg,var(--teal),var(--gold));z-index:60}
.hd{animation:rise .8s ease both}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.js .reveal{opacity:0;transform:translateY(18px);
transition:opacity .7s cubic-bezier(.2,.7,.2,1),transform .7s cubic-bezier(.2,.7,.2,1)}
.js .reveal.in{opacity:1;transform:none}
.nav{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}
.nav a{color:var(--gold);border:1px solid var(--line);border-radius:22px;padding:7px 16px;
font-size:14px;letter-spacing:.03em;transition:color .15s,border-color .15s}
.nav a:hover{color:var(--teal);border-color:#2b4a58}
.nav a.refresh{color:#04121a;font-weight:700;border-color:transparent;
background:linear-gradient(180deg,#7fe6d8,#41b6a7);box-shadow:0 2px 12px rgba(95,215,200,.3)}
.nav a.refresh:hover{color:#04121a;filter:brightness(1.08)}
.hint{color:var(--dim);font-size:13px;margin-top:12px;line-height:1.75}
.card{border:1px solid var(--line);border-radius:14px;padding:19px 20px;margin:22px 0;
background:linear-gradient(180deg,rgba(255,255,255,.025),rgba(255,255,255,0))}
.card h3{font-size:clamp(16.5px,4.3vw,18.5px);margin:0 0 11px;color:var(--fg);font-weight:700;
line-height:1.6;text-wrap:balance}
.card p{font-size:clamp(14.5px,3.9vw,15.5px);margin:10px 0;color:#d2dde8;text-wrap:pretty}
.row{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin:9px 0}
.badge{font-size:12.5px;letter-spacing:.04em;border-radius:20px;padding:3px 11px;
border:1px solid var(--line);color:#aab7c3;white-space:nowrap}
.b-confirmed,.b-pending{color:#7fe6d8;border-color:#2b4a58}
.b-reported{color:var(--gold);border-color:#3a3320}
.b-hit{color:#0a1410;background:linear-gradient(180deg,#7fe6a8,#41b673);border-color:transparent;font-weight:700}
.b-miss{color:#f0b3b3;border-color:#5a2b2b;background:rgba(120,40,40,.18)}
.b-review{color:var(--gold);border-color:#3a3320}
.hitrate{color:var(--teal);font-size:29px;font-weight:700;letter-spacing:.02em;
text-shadow:0 0 16px rgba(95,215,200,.35)}
.kv{font-size:13.5px;color:var(--dim)}
.kv b{color:#bcd0df;font-weight:600}
.deadline{color:var(--gold)}
.empty{color:var(--dim);font-size:15px;margin-top:28px}
@supports (animation-timeline:scroll()){
.progress{animation:grow linear both;animation-timeline:scroll(root)}
@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}}
@media (prefers-reduced-motion:reduce){
.hd{animation:none}
.js .reveal{opacity:1;transform:none;transition:none}
.tip{transition:none;opacity:1;transform:none}
.progress{display:none}}"""


def parse_json(text):
    # 応答にJSON値が複数連続・前置きが混在しても、最初の完全なJSON値だけを取り出す
    # (貪欲正規表現だと複数オブジェクト連結時に "Extra data" で失敗する)
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                v, _ = dec.raw_decode(text[i:])
                return v
            except ValueError:
                continue
    return None
