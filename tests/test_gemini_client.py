"""GeminiClient の堅牢性(オフライン・http をモック)。モデルフォールバック・429リトライ・
回数上限・構造化出力の指定を検証。time.sleep は無効化して即時に走らせる。"""
import json
import urllib.error

import pytest

import common


def _resp(text="OK", ver="gemini-flash-latest-001"):
    return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}],
                       "modelVersion": ver}).encode()


def _err(code):
    return urllib.error.HTTPError("https://x", code, "err", None, None)


class Seq:
    """呼ばれるたびに用意した応答/例外を順に返す http スタブ。"""
    def __init__(self, items):
        self.items = list(items)
        self.bodies = []

    def __call__(self, url, data=None, headers=None):
        self.bodies.append(data)
        x = self.items.pop(0)
        if isinstance(x, Exception):
            raise x
        return x


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda *a, **k: None)


def test_falls_back_to_next_model_on_404(monkeypatch):
    monkeypatch.setattr(common, "http", Seq([_err(404), _resp(ver="gemini-flash-lite-latest-9")]))
    c = common.GeminiClient("k", call_limit=10)
    assert c.generate("p") == "OK"
    assert c.last_model_version == "gemini-flash-lite-latest-9"


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(common, "http", Seq([_err(429), _err(503), _resp()]))
    c = common.GeminiClient("k", call_limit=10)
    assert c.generate("p") == "OK"
    assert c.calls == 3


def test_call_limit_enforced(monkeypatch):
    monkeypatch.setattr(common, "http", Seq([_err(429)] * 20))
    c = common.GeminiClient("k", call_limit=3)
    with pytest.raises(RuntimeError):
        c.generate("p")
    assert c.calls <= 3


def test_response_schema_included_in_request(monkeypatch):
    stub = Seq([_resp()])
    monkeypatch.setattr(common, "http", stub)
    c = common.GeminiClient("k", call_limit=10)
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    c.generate("p", response_schema=schema)
    sent = json.loads(stub.bodies[0].decode())
    assert sent["generationConfig"]["responseSchema"] == schema


def test_non_overload_error_raises(monkeypatch):
    monkeypatch.setattr(common, "http", Seq([_err(400)]))
    c = common.GeminiClient("k", call_limit=10)
    with pytest.raises(urllib.error.HTTPError):
        c.generate("p")


def _grounded_resp(text="判定します\n{\"result\":\"hit\"}", uri="https://src"):
    return json.dumps({"candidates": [{"content": {"parts": [{"text": text}]},
                                       "groundingMetadata": {"groundingChunks": [{"web": {"uri": uri, "title": "t"}}]}}],
                       "modelVersion": "gemini-flash-latest-1"}).encode()


def test_grounding_falls_back_to_alt_tool_format(monkeypatch):
    # 第一形式 {"google_search":{}} が 400 → 別形式 {"type":"google_search"} で成功
    stub = Seq([_err(400), _grounded_resp()])
    monkeypatch.setattr(common, "http", stub)
    c = common.GeminiClient("k", call_limit=10)
    text = c.generate_grounded("p")
    assert "hit" in text
    assert c.last_grounding_urls and c.last_grounding_urls[0]["url"] == "https://src"
    # 通った形式(2番目)がキャッシュされる
    assert c._grounding_tools == {"type": "google_search"}
    sent = json.loads(stub.bodies[1].decode())
    assert sent["tools"] == [{"type": "google_search"}]


def test_grounding_caches_working_format(monkeypatch):
    stub = Seq([_grounded_resp(), _grounded_resp()])  # 1回目で {"google_search":{}} が通る
    monkeypatch.setattr(common, "http", stub)
    c = common.GeminiClient("k", call_limit=10)
    c.generate_grounded("p")
    c.generate_grounded("p")  # 2回目はキャッシュ形式のみ(400試行なし=1リクエスト)
    assert c._grounding_tools == {"google_search": {}}
    assert len(stub.bodies) == 2  # 各回1リクエストのみ
