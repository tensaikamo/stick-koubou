"""resolver の答え合わせ(fake seam・オフライン)。ハイブリッド判定=証拠明確時のみ自動○×、
曖昧/低確度/未到来は保留(偽×を出さない)。冪等。的中率は確定分のみ。"""
import json
from datetime import datetime, timedelta

import resolver
import memory


def _mk(hid, claim, deadline, conf=0.7):
    now = datetime.now(resolver.JST)
    return {"id": hid, "created_at": (now - timedelta(days=20)).isoformat(), "based_on": ["r1"],
            "prose": claim, "claim": claim, "subject": "OpenAI",
            "resolution": {"source": "公式", "check_query": "openai x", "decider": "公式発表"},
            "deadline": deadline, "confidence": conf, "status": "pending", "resolved_at": None,
            "result": None, "evidence": None, "rejected": [], "model": "t",
            "schema_version": "1", "generator_ver": "v1"}


def _setup(workdir):
    today = datetime.now(resolver.JST).date()
    y = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    future = (today + timedelta(days=10)).strftime("%Y-%m-%d")
    hunches = [
        _mk("h-hit", "OpenAIが新機能をGAする", y),
        _mk("h-miss", "OpenAIが撤回する", y),
        _mk("h-unclear", "OpenAIが何かする", y),
        _mk("h-lowmiss", "OpenAIが値下げする", y, conf=0.5),
        _mk("h-future", "OpenAIが将来動く", future),
    ]
    (workdir / "data/hunches.json").write_text(json.dumps(hunches, ensure_ascii=False), encoding="utf-8")
    (workdir / "data/records.json").write_text("[]", encoding="utf-8")
    fake = {
        "h-hit": {"result": "hit", "confidence": 0.9, "evidence": {"summary": "公式がGAを告知", "url": "https://openai.com/x"}},
        "h-miss": {"result": "miss", "confidence": 0.85, "evidence": {"summary": "公式が撤回でなく拡大", "url": "https://openai.com/y"}},
        "h-unclear": {"result": "unclear", "confidence": 0.2, "evidence": {"summary": "証拠なし", "url": ""}},
        "h-lowmiss": {"result": "miss", "confidence": 0.5, "evidence": {"summary": "根拠薄い", "url": ""}},
    }
    fp = workdir / "fake.json"
    fp.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")
    return fp


def test_hybrid_no_false_negative(workdir, monkeypatch):
    fp = _setup(workdir)
    monkeypatch.setenv("RESOLVER_FAKE_RESPONSE", str(fp))
    resolver.main()
    h = {x["id"]: x for x in json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))}

    assert h["h-hit"]["status"] == "resolved" and h["h-hit"]["result"] == "hit"
    assert h["h-miss"]["status"] == "resolved" and h["h-miss"]["result"] == "miss"
    # 低確度 miss と unclear は自動確定しない(偽×を出さない)
    assert h["h-unclear"]["status"] == "pending" and h["h-unclear"]["result"] is None and h["h-unclear"]["needs_review"]
    assert h["h-lowmiss"]["status"] == "pending" and h["h-lowmiss"]["result"] is None
    # 未到来は不変
    assert h["h-future"]["status"] == "pending" and not h["h-future"].get("needs_review")

    st = memory.hit_stats(list(h.values()))
    assert st["hit"] == 1 and st["miss"] == 1 and round(st["rate"] * 100) == 50


class _GroundingClient:
    """generate_grounded が散文+末尾JSONを返すダミー(ネットワーク不使用)。"""
    def __init__(self):
        self.last_grounding_urls = [{"title": "OpenAI Blog", "url": "https://openai.com/src"}]

    def generate_grounded(self, prompt):
        return ("検索の結果、公式ブログで一般提供が告知されていた。\n"
                '{"result":"hit","evidence":{"summary":"公式が一般提供を告知","url":""},"confidence":0.9}')


def test_grounding_verdict_and_source_fill():
    now = datetime.now(resolver.JST)
    h = _mk("g1", "OpenAIがGAする", (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    v = resolver.judge(_GroundingClient(), h)
    assert v and v["result"] == "hit"
    # evidence.url が空でもグラウンディング出典で補完される
    assert v["evidence"]["url"] == "https://openai.com/src"


def test_last_json_takes_final_object():
    assert resolver._last_json('前置き {"a":1} 途中 {"result":"hit"}')["result"] == "hit"


def test_resolved_are_idempotent(workdir, monkeypatch):
    fp = _setup(workdir)
    monkeypatch.setenv("RESOLVER_FAKE_RESPONSE", str(fp))
    resolver.main()
    before = (workdir / "data/hunches.json").read_text(encoding="utf-8")
    resolver.main()
    after = (workdir / "data/hunches.json").read_text(encoding="utf-8")
    assert before == after  # 確定分は再実行で不変
