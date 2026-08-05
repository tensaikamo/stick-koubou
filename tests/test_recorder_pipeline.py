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
             "resolution": {"source": "https://openai.com/blog", "check_query": "openai enterprise GA",
                            "decider": "公式が新階層の一般提供開始を告知"}, "deadline_days": 10, "base_rate": 0.25,
             "base_rate_class": "大手AI企業が10日窓で新階層をGAする頻度",
             "confidence": 0.32, "confidence_why": "価格と時期が示されており基準率よりやや上",
             "counter": "提供が限定プレビューに留まりGAが遅れる可能性",
             "indicators": [{"sign": "料金ページに新階層が載る", "dir": "confirm"},
                            {"sign": "公式が延期を告知する", "dir": "kill"},
                            {"sign": "", "dir": "confirm"}]},   # 空の指標は捨てられる
            {"based_on": [1], "prose": "Anthropicは資金を計算資源に振ると見る。",
             "claim": "Anthropicが話題になる", "subject": "Anthropic",
             "resolution": {"source": "https://www.anthropic.com/news", "check_query": "anthropic news", "decider": "広く話題になる"},
             "deadline_days": 10, "base_rate": 0.3, "base_rate_class": "資金調達後10日で話題化する頻度",
             "confidence": 0.3, "confidence_why": "基準率どおり"},
            {"based_on": [0], "prose": "何かが動くと見る。", "claim": "何かが起きる", "subject": "",
             "resolution": {"source": "https://example.com/news", "check_query": "x", "decider": "何かが起きる"},
             "deadline_days": 10, "base_rate": 0.4, "base_rate_class": "何かが起きる頻度",
             "confidence": 0.4, "confidence_why": "基準率どおり"},
        ],
        "_fake_regens": [
            {"based_on": [1], "prose": "Anthropicは新製品を近く出すと見る。",
             "claim": "Anthropicが新モデル/新製品を正式発表する", "subject": "Anthropic",
             "resolution": {"source": "https://www.anthropic.com/news", "check_query": "anthropic launch", "decider": "公式が提供開始を告知"},
             "deadline_days": 12, "base_rate": 0.2, "base_rate_class": "大手AI企業が12日窓で新製品を正式発表する頻度",
             "confidence": 0.26, "confidence_why": "資金余力が増したぶん基準率よりやや上"},
            {"based_on": [0], "prose": "まだ曖昧.", "claim": "曖昧", "subject": "",
             "resolution": {"source": "https://example.com/news", "check_query": "x", "decider": "何かが起きる"},
             "deadline_days": 10, "base_rate": 0.4, "base_rate_class": "何かが起きる頻度",
             "confidence": 0.4, "confidence_why": "基準率どおり"},
            {"based_on": [0], "prose": "やはり曖昧.", "claim": "曖昧", "subject": "",
             "resolution": {"source": "https://example.com/news", "check_query": "y", "decider": "話題になる"},
             "deadline_days": 10, "base_rate": 0.4, "base_rate_class": "何かが起きる頻度",
             "confidence": 0.4, "confidence_why": "基準率どおり"},
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

    # 記憶の物語ページが生成され壊れない(主体はどれも単発=スレッドなしでも空表示で成立)
    tp = (workdir / "docs/threads.html").read_text(encoding="utf-8")
    assert "記憶の物語" in tp and "<html" in tp

    # 指標監視(I&W): 空の指標は捨て、confirm/kill を保持し、点灯履歴は空で初期化される
    assert [i["sign"] for i in h1["indicators"]] == ["料金ページに新階層が載る", "公式が延期を告知する"]
    assert [i["dir"] for i in h1["indicators"]] == ["confirm", "kill"]
    assert h1["signals"] == []
    assert "見張り" in hp and "料金ページに新階層が載る" in hp


def test_signal_display_and_no_false_verdict(workdir, monkeypatch):
    # 点灯履歴が台帳に出ること。kill が点灯しても result は None のまま(偽×を出さない)
    import recorder as R
    _run(workdir, monkeypatch)
    huns = json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))
    h = next(x for x in huns if x["status"] == "pending" and x.get("indicators"))
    h["signals"] = [{"date": "2026-07-25", "sign": "公式が延期を告知する", "dir": "kill",
                     "why": "公式ブログが延期を明記した"}]
    h["needs_review"] = True
    R.dump_json(str(workdir / "data/hunches.json"), huns)
    R.render_pages()
    page = (workdir / "docs/hunches.html").read_text(encoding="utf-8")
    assert "死亡シグナル" in page and "公式ブログが延期を明記した" in page
    after = json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))
    hit = next(x for x in after if x["id"] == h["id"])
    assert hit["result"] is None and hit["status"] == "pending"   # 自動で×にしない


def test_duplicate_urls_do_not_create_new_records(workdir, monkeypatch):
    """記憶の汚染防止(B2): 同じURLの記事が翌日も来た場合に record を増やさない。
    HN上位は数日居座るため、素朴に記録すると重複率54%になり、記憶が
    『同じ話が4日続く重要テーマ』という偽の継続性で汚染されていた。"""
    _run(workdir, monkeypatch)
    first = json.loads((workdir / "data/records.json").read_text(encoding="utf-8"))
    assert len(first) == 3
    # 翌日ぶんとして同じ記事集合をもう一度流す(runs の冪等ガードは外す)
    (workdir / ("data/runs/%s.json" % datetime.now(recorder.JST).strftime("%Y-%m-%d"))).unlink()
    _run(workdir, monkeypatch)
    second = json.loads((workdir / "data/records.json").read_text(encoding="utf-8"))
    assert len(second) == 3, "同一URLで record が増えている(重複)"
    urls = [(r.get("source") or {}).get("url") for r in second]
    assert len(set(urls)) == len(urls), "URLが重複している"
    # 根拠の鎖は保たれる: hunch は既存 record の id を指す
    huns = json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))
    ids = {r["id"] for r in second}
    pend = [h for h in huns if h["status"] == "pending"]
    assert pend and all(all(b in ids for b in h["based_on"]) for h in pend)


def test_duplicate_path_keeps_the_original_certainty(workdir, monkeypatch):
    """重複スキップ時に certainty を "reported" へ決め打ちしない。

    決め打ちすると、昨日 rumor(噂)として記録した事実が今日は報道扱いになり、
    『根拠が rumor の record のみ』の予測を弾くゲート(validate_hunch)をすり抜ける。
    噂を根拠に自信満々の予測が生まれる穴だった。"""
    _run(workdir, monkeypatch)
    recs = json.loads((workdir / "data/records.json").read_text(encoding="utf-8"))
    rumor = next(r for r in recs if r["certainty"] == "rumor")

    seen = {}
    monkeypatch.setattr(recorder.memory, "related_ids_for", lambda *a, **k: [])
    # 重複経路が内部表現に何を積むかを覗く
    orig = recorder.validate_hunch
    monkeypatch.setattr(recorder, "validate_hunch",
                        lambda h, records, *a, **k: (seen.update({"records": records}), orig(h, records, *a, **k))[1])
    (workdir / ("data/runs/%s.json" % datetime.now(recorder.JST).strftime("%Y-%m-%d"))).unlink()
    _run(workdir, monkeypatch)

    passed = seen.get("records") or []
    dup = next((r for r in passed if r.get("id") == rumor["id"]), None)
    assert dup is not None, "重複した record が内部表現に現れていない"
    assert dup["certainty"] == "rumor", "噂が勝手に報道へ格上げされている"


def test_records_page_shows_each_article_once(workdir, monkeypatch):
    """記録の台帳が同じ記事を何度も並べない(実測: 36件中ユニーク13件、AP記事が7回)。
    履歴は台帳に全件残したまま、読む側で1件にまとめる。"""
    _run(workdir, monkeypatch)
    recs = json.loads((workdir / "data/records.json").read_text(encoding="utf-8"))
    # 過去に溜まった重複を模す(同一URLの再掲を2件足す)
    dupes = [dict(recs[0], id="dup-1"), dict(recs[0], id="dup-2")]
    recorder.dump_json(str(workdir / "data/records.json"), recs + dupes)
    recorder.render_pages()
    page = (workdir / "docs/records.html").read_text(encoding="utf-8")
    assert page.count(recs[0]["headline"]) == 1, "同じ記事が複数回描画されている"
    assert "再掲2件は除外" in page          # 何件隠したかは読者に伝える
    # 履歴そのものは改変しない
    assert len(json.loads((workdir / "data/records.json").read_text(encoding="utf-8"))) == len(recs) + 2


def test_idempotent_second_run(workdir, monkeypatch):
    _run(workdir, monkeypatch)
    r1 = (workdir / "data/records.json").read_text(encoding="utf-8")
    h1 = (workdir / "data/hunches.json").read_text(encoding="utf-8")
    _run(workdir, monkeypatch)  # 2回目: runs 存在でスキップ
    assert (workdir / "data/records.json").read_text(encoding="utf-8") == r1
    assert (workdir / "data/hunches.json").read_text(encoding="utf-8") == h1
