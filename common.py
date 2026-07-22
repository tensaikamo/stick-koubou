"""サイト生成(sanbo.py)と予測記録層(recorder.py)で共有するヘルパ群。
標準ライブラリのみに依存する(依存追加は recorder.py 側の requests のみ)。"""
import json, re, urllib.request, urllib.parse, urllib.error, time
import xml.etree.ElementTree as ET

# gemini-2.5-flash は 2026-07 時点で API が 404 を返す(提供終了)ため候補から除外。
# 常に現行の flash 系を指す公式エイリアス gemini-flash-latest を第一候補にし、
# 過負荷時は旧名 → 別容量プールの flash-lite へ順にフォールバックする(全て無料のflash系)
MODELS = ["gemini-flash-latest", "gemini-2.0-flash", "gemini-flash-lite-latest"]


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

    def generate(self, prompt):
        """プロンプトを投げ、生成テキストを返す。実際に応答したモデルIDは
        self.last_model_version に保存する(呼び出し側が model フィールドに使う)。"""
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"responseMimeType": "application/json"}}).encode()
        last = None
        for m in (self._model_ok or MODELS):
            url = "https://generativelanguage.googleapis.com/v1beta/models/" + m + ":generateContent"
            # 429/503(一時的な過負荷)は20秒待って同モデルに1回だけ再試行(計2回)。
            for wait in (0, 20):
                if wait:
                    time.sleep(wait)
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
                        print("model", m, "-> HTTP", e.code, "(過負荷) retrying")
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
