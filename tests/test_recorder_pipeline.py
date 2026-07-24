"""recorder の受け入れ(fake seam・オフライン)。records/hunches 追記、based_on→実id変換、
deadline確定、検証ゲート不合格→再生成→unscorable+rejected蓄積、冪等スキップ、日本語非エスケープ。"""
import json
from datetime import datetime

import recorder

FAKE_ARTS = [
    {"title": "OpenAI launches new enterprise model tier", "url": "https://openai.com/blog/x",
     "meta": "600pt", "src": "HN", "points": 600},
    {"title": "Anthropic closes new funding round", "url": "https://anthropic.com/news/y",
     "meta": "500pt", "src": "HN", "points": 500},
    {"title": "Rumor: Google may restrict Gemini API access", "url": "https://example.com/z",
     "meta": "400pt", "src": "HN", "points": 400},
]


def _fake_response():
    return {
        "_model": "test-model",
        "records": [
            {"article_index": 0, "headline": "OpenAIが新エンタープライズ階層を発表",
             "what_happened": "OpenAIは企業向けの新しいモデル階層を発表した。価格と提供時期が示された。移行案内も同時に行われた。公式ブログで告知された。",
             "background": "OpenAIは企業向け収益を拡大中。", "changed": "選択肢が増えた。",
             "certainty": "confirmed", "source_tier": "primary"},
            {"article_index": 1, "headline": "Anthropicが資金調達を実施",
             "what_happened": "Anthropicが新ラウンドを完了と報じられた。金額と投資家が伝えられた。用途は計算資源。",
             "background": "資金競争が続く。", "changed": "資金余力が増した。",
             "certainty": "reported", "source_tier": "secondary"},
            {"article_index": 2, "headline": "GoogleがGemini API制限の噂",
             "what_happened": "制限の噂が流れている。公式発表はない。",
             "background": "囲い込みが強まる。", "changed": "事実は不確定。",
             "certainty": "rumor", "source_tier": "secondary"},
        ],
        "hunches": [
            {"based_on": [0], "prose": "OpenAIは新階層で企業取り込みを進めると見る。",
             "claim": "OpenAIは新エンタープライズ階層を一般提供(GA)する", "subject": "OpenAI",
             "resolution": {"source": "公式ブログ", "check_query": "openai enterprise GA",
                            "decider": "公式が新階層の一般提供開始を告知"}, "deadline_days": 10, "confidence": 0.72,
             "counter": "提供が限定プレビューに留まりGAが遅れる可能性"},
            {"based_on": [1], "prose": "Anthropicは資金を計算資源に振ると見る。",
             "claim": "Anthropicが話題になる", "subject": "Anthropic",
             "resolution": {"source": "報道", "check_query": "anthropic news", "decider": "広く話題になる"},
             "deadline_days": 10, "confidence": 0.6},
            {"based_on": [0], "prose": "何かが動くと見る。", "claim": "何かが起きる", "subject": "",
             "resolution": {"source": "報道", "check_query": "x", "decider": "何かが起きる"},
             "deadline_days": 10, "confidence": 0.55},
        ],
        "_fake_regens": [
            {"based_on": [1], "prose": "Anthropicは新製品を近く出すと見る。",
             "claim": "Anthropicが新モデル/新製品を正式発表する", "subject": "Anthropic",
             "resolution": {"source": "公式", "check_query": "anthropic launch", "decider": "公式が提供開始を告知"},
             "deadline_days": 12, "confidence": 0.66},
            {"based_on": [0], "prose": "まだ曖昧.", "claim": "曖昧", "subject": "",
             "resolution": {"source": "報道", "check_query": "x", "decider": "何かが起きる"},
             "deadline_days": 10, "confidence": 0.5},
            {"based_on": [0], "prose": "やはり曖昧.", "claim": "曖昧", "subject": "",
             "resolution": {"source": "報道", "check_query": "y", "decider": "話題になる"},
             "deadline_days": 10, "confidence": 0.5},
        ],
    }


def _run(workdir, monkeypatch):
    monkeypatch.setattr(recorder, "fetch_hn", lambda: [dict(a) for a in FAKE_ARTS])
    monkeypatch.setattr(recorder, "fetch_tc", lambda: [])
    fp = workdir / "fake.json"
    fp.write_text(json.dumps(_fake_response(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RECORDER_FAKE_RESPONSE", str(fp))
    recorder.main()


def test_pipeline(workdir, monkeypatch):
    _run(workdir, monkeypatch)
    records = json.loads((workdir / "data/records.json").read_text(encoding="utf-8"))
    hunches = json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))

    assert len(records) == 3 and len(hunches) == 3

    # based_on が実在 record id を指す(pending のみ)
    ids = {r["id"] for r in records}
    pend = [h for h in hunches if h["status"] == "pending"]
    assert pend and all(all(b in ids for b in h["based_on"]) for h in pend)

    # deadline は +3〜+30日
    today = datetime.now(recorder.JST).date()
    for h in pend:
        d = datetime.strptime(h["deadline"], "%Y-%m-%d").date()
        assert 3 <= (d - today).days <= 30

    # ゲート: 1件は unscorable(subject欠落を3回)で rejected 3件蓄積
    us = [h for h in hunches if h["status"] == "unscorable"]
    assert len(us) == 1 and len(us[0]["rejected"]) == 3

    # 日本語が非エスケープ
    raw = (workdir / "data/records.json").read_text(encoding="utf-8")
    assert "\\u" not in raw and "発表" in raw

    # runs ファイル
    assert (workdir / ("data/runs/%s.json" % today.strftime("%Y-%m-%d"))).exists()

    # 反証条件(counter)が hunch に格納され、台帳HTMLに「外れるとすれば」で載る
    h1 = next(h for h in hunches if h["status"] == "pending" and h["subject"] == "OpenAI")
    assert h1.get("counter") and "限定プレビュー" in h1["counter"]
    hp = (workdir / "docs/hunches.html").read_text(encoding="utf-8")
    assert "外れるとすれば" in hp


def test_idempotent_second_run(workdir, monkeypatch):
    _run(workdir, monkeypatch)
    r1 = (workdir / "data/records.json").read_text(encoding="utf-8")
    h1 = (workdir / "data/hunches.json").read_text(encoding="utf-8")
    _run(workdir, monkeypatch)  # 2回目: runs 存在でスキップ
    assert (workdir / "data/records.json").read_text(encoding="utf-8") == r1
    assert (workdir / "data/hunches.json").read_text(encoding="utf-8") == h1
