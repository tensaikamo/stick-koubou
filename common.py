"""サイト生成(sanbo.py)と予測記録層(recorder.py)で共有するヘルパ群。
標準ライブラリのみに依存する(依存追加は recorder.py 側の requests のみ)。"""
import json, re, random, urllib.request, urllib.parse, urllib.error, time
import xml.etree.ElementTree as ET

# 無料枠は Flash 系のみ(Pro は2026-04以降 無料枠外、gemini-2.0/2.5系は退役・退役予定)。
# 常に現行の flash を指す公式エイリアス gemini-flash-latest を第一候補、別容量プールの
# flash-lite をフォールバックにする。退役済みの固定バージョン名は候補から外す(429の無駄撃ち防止)。
MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]

# 過負荷(429/503)時の待機秒(指数+ジッタ)。同一モデルでこの回数試す。無料枠の一時的な
# 過負荷は数十秒で解けることが多く、当日ブリーフィングの生成失敗を実質的に減らす。
BACKOFFS = [0, 10, 30]


def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


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
        self.last_model_version = None  # 直近レスポンスの実モデルID(APIレスポンス由来)

    def generate(self, prompt, response_schema=None):
        """プロンプトを投げ、生成テキストを返す。実際に応答したモデルIDは
        self.last_model_version に保存する(呼び出し側が model フィールドに使う)。
        response_schema を渡すと構造化出力(JSON準拠保証)を要求する(任意・後方互換)。"""
        cfg = {"responseMimeType": "application/json"}
        if response_schema is not None:
            cfg["responseSchema"] = response_schema
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": cfg}).encode()
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
                    return d["candidates"][0]["content"]["parts"][0]["text"]
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


# サイト(index)と台帳(records/hunches)ページで共有するCSS。3ページで見た目を統一する。
PAGE_CSS = """:root{--bg:#0b0f14;--fg:#dbe4ec;--teal:#5fd7c8;--gold:#d6a24c;--line:#1a2431;--dim:#586a7a}
*{box-sizing:border-box}
body{background:radial-gradient(1200px 600px at 50% -10%,#0f1720 0%,var(--bg) 60%) no-repeat,var(--bg);
color:var(--fg);font-family:system-ui,'Hiragino Sans','Hiragino Kaku Gothic ProN',sans-serif;
margin:0;padding:24px 18px 64px;line-height:1.9;-webkit-font-smoothing:antialiased;
text-rendering:optimizeLegibility}
main{max-width:640px;margin:0 auto}
.hd{margin-bottom:28px}
h1{font-size:15px;letter-spacing:.3em;color:var(--teal);font-weight:400;margin:0;
text-shadow:0 0 18px rgba(95,215,200,.35)}
.d{color:var(--dim);font-size:12px;letter-spacing:.15em;margin-top:8px}
h2{font-size:13px;letter-spacing:.25em;color:var(--gold);border-bottom:1px solid var(--line);
padding-bottom:8px;margin-top:0;font-weight:400;position:relative}
h2::after{content:"";position:absolute;left:0;bottom:-1px;width:38px;height:1px;
background:linear-gradient(90deg,var(--gold),transparent)}
section{margin-top:36px}
p{font-size:15px}
.lb{color:#04121a;font-size:11px;letter-spacing:.2em;border-radius:5px;
padding:2px 10px;margin-right:12px;white-space:nowrap;font-weight:600;display:inline-block;
transform:translateY(-1px);background:linear-gradient(180deg,#7fe6d8,#41b6a7);
box-shadow:0 2px 10px rgba(95,215,200,.25)}
.mj{border:1px solid var(--line);border-radius:10px;padding:13px 15px;margin:14px 0;
background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,0))}
.mjt{font-size:14px;color:var(--fg)}
.mj p{font-size:13px;margin:6px 0}
.mjw{color:#8fb8d8}
t{border-bottom:1px dotted var(--teal);cursor:pointer;transition:color .15s,border-color .15s;
-webkit-tap-highlight-color:transparent}
t:active,t:hover{color:var(--teal);border-bottom-style:solid}
.tip{position:fixed;margin:0;z-index:50;background:#101a26;border:1px solid #2b4a58;
border-radius:10px;padding:10px 13px;font-size:13px;line-height:1.7;color:#c7dbe8;
max-width:calc(100vw - 24px);box-shadow:0 10px 34px rgba(0,0,0,.55);
opacity:0;transform:translateY(-6px) scale(.98);transition:opacity .22s ease,transform .22s ease}
.tip.show{opacity:1;transform:none}
[popover].tip{inset:unset}
[popover].tip:popover-open{opacity:1;transform:none}
@starting-style{[popover].tip:popover-open{opacity:0;transform:translateY(-6px) scale(.98)}}
ul{padding-left:0;list-style:none}
li{margin:14px 0;font-size:14px}
a{color:#8fb8d8;text-decoration:none}
li a{transition:color .15s}
li a:hover{color:var(--teal)}
.m{color:var(--dim);font-size:11px;margin-left:6px}
.progress{position:fixed;top:0;left:0;height:2px;width:100%;transform:scaleX(0);
transform-origin:0 50%;background:linear-gradient(90deg,var(--teal),var(--gold));z-index:60}
.hd{animation:rise .8s ease both}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.js .reveal{opacity:0;transform:translateY(18px);
transition:opacity .7s cubic-bezier(.2,.7,.2,1),transform .7s cubic-bezier(.2,.7,.2,1)}
.js .reveal.in{opacity:1;transform:none}
.nav{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap}
.nav a{color:var(--gold);border:1px solid var(--line);border-radius:20px;padding:5px 14px;
font-size:12px;letter-spacing:.08em;transition:color .15s,border-color .15s}
.nav a:hover{color:var(--teal);border-color:#2b4a58}
.nav a.refresh{color:#04121a;font-weight:600;border-color:transparent;
background:linear-gradient(180deg,#7fe6d8,#41b6a7);box-shadow:0 2px 12px rgba(95,215,200,.3)}
.nav a.refresh:hover{color:#04121a;filter:brightness(1.08)}
.hint{color:var(--dim);font-size:11px;margin-top:10px;line-height:1.7}
.card{border:1px solid var(--line);border-radius:12px;padding:15px 16px;margin:16px 0;
background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,0))}
.card h3{font-size:15px;margin:0 0 8px;color:var(--fg);font-weight:600;line-height:1.65}
.card p{font-size:13.5px;margin:8px 0;color:#c2d0dc}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:6px 0}
.badge{font-size:10.5px;letter-spacing:.08em;border-radius:20px;padding:2px 10px;
border:1px solid var(--line);color:#9aa7b3;white-space:nowrap}
.b-confirmed,.b-pending{color:#7fe6d8;border-color:#2b4a58}
.b-reported{color:var(--gold);border-color:#3a3320}
.b-hit{color:#0a1410;background:linear-gradient(180deg,#7fe6a8,#41b673);border-color:transparent;font-weight:600}
.b-miss{color:#f0b3b3;border-color:#5a2b2b;background:rgba(120,40,40,.18)}
.b-review{color:var(--gold);border-color:#3a3320}
.hitrate{color:var(--teal);font-size:22px;font-weight:600;letter-spacing:.05em;
text-shadow:0 0 16px rgba(95,215,200,.35)}
.kv{font-size:12px;color:var(--dim)}
.kv b{color:#aec4d4;font-weight:500}
.deadline{color:var(--gold)}
.empty{color:var(--dim);font-size:13px;margin-top:24px}
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
