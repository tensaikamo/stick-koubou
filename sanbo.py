import json, os, re, html, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = "gemini-2.5-flash"

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
                            "meta": str(h.get("points", 0)) + "pt", "src": "HN"})
        except Exception as e:
            print("hn", e)
    return out

def fetch_tc():
    try:
        raw = http("https://techcrunch.com/category/artificial-intelligence/feed/")
        root = ET.fromstring(raw)
        return [{"title": i.findtext("title") or "", "url": i.findtext("link") or "",
                 "meta": "", "src": "TechCrunch"} for i in root.iter("item")][:15]
    except Exception as e:
        print("tc", e)
        return []

def gemini(prompt):
    url = "https://generativelanguage.googleapis.com/v1beta/models/" + MODEL + ":generateContent?key=" + API_KEY
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    d = json.loads(http(url, data=body, headers={"Content-Type": "application/json"}).decode())
    return d["candidates"][0]["content"]["parts"][0]["text"]

def parse_json(text):
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    return json.loads(m.group(0)) if m else None

items = fetch_hn() + fetch_tc()
seen, arts = set(), []
for a in items:
    k = a["title"].lower().strip()
    if k and k not in seen:
        seen.add(k); arts.append(a)
arts = arts[:40]

lst = "\n".join(str(i) + ". [" + a["src"] + "] " + a["title"] for i, a in enumerate(arts))

sel = None
try:
    sel = parse_json(gemini(
        "あなたはシリコンバレー駐在の情報参謀。以下は過去24時間の英語ヘッドライン。\n"
        "『シリコンバレーのAI業界の空気を掴む』観点で重要な記事を6〜8本選び、番号だけをJSON配列で返せ。\n"
        "説明不要、JSON配列のみ。\n\n" + lst))
except Exception as e:
    print("sel", e)
if not isinstance(sel, list):
    sel = list(range(min(7, len(arts))))
picked = [arts[i] for i in sel if isinstance(i, int) and 0 <= i < len(arts)]

plist = "\n".join("- [" + a["src"] + "] " + a["title"] + " (" + a["url"] + ")" for a in picked)
brief = None
try:
    brief = parse_json(gemini(
        "あなたは日本人経営者に仕えるシリコンバレー駐在の情報参謀。以下が今日の重要記事。\n"
        "日本語で簡潔かつ具体的に、次のJSONだけを返せ:\n"
        '{"kuki": "今日の空気(現地で何が騒がれ、金と注目がどこに動いているか。3〜5文)", '
        '"dousuru": "で、どうする(この動きが日本と個人にどう波及するか、注視すべき点。2〜4文)"}\n\n' + plist))
except Exception as e:
    print("brief", e)
if not isinstance(brief, dict):
    brief = {"kuki": "本日の生成に失敗。次回実行で回復します。", "dousuru": "-"}

jst = datetime.now(timezone(timedelta(hours=9)))
links = "\n".join('<li><a href="' + html.escape(a["url"]) + '">' + html.escape(a["title"])
                  + '</a> <span class="m">' + a["src"] + " " + a["meta"] + "</span></li>" for a in picked)

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
ul{padding-left:0;list-style:none}
li{margin:14px 0;font-size:14px}
a{color:#8fb8d8;text-decoration:none}
.m{color:#586a7a;font-size:11px;margin-left:6px}
</style></head><body><main>
<h1>◇ シリコンバレー参謀</h1>
<div class="d">""" + jst.strftime("%Y.%m.%d %H:%M") + """ JST</div>
<h2>今日の空気</h2><p>""" + html.escape(brief.get("kuki", "")) + """</p>
<h2>で、どうする</h2><p>""" + html.escape(brief.get("dousuru", "")) + """</p>
<h2>今日の重要記事</h2><ul>""" + links + """</ul>
</main></body></html>"""

os.makedirs("docs", exist_ok=True)
open("docs/index.html", "w", encoding="utf-8").write(page)
print("done", len(picked))
