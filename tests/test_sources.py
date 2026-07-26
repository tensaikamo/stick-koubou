"""情報源フェッチャのパース検証(common.http をモック=ネットワーク不使用)。
一次情報(T1)を読めること・失敗しても [] を返して他を止めないこと・
1ソースが候補枠を独占しないこと(T3=空気が消えない)を守る。"""
import json

import common


class FakeHTTP:
    """URL の部分一致で応答を返すスタブ。未登録URLは例外(=取得失敗の再現)。"""
    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, url, data=None, headers=None, timeout=90):
        self.calls.append(url)
        for key, body in self.table.items():
            if key in url:
                return body if isinstance(body, bytes) else body.encode()
        raise OSError("no stub for " + url)


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><title>Scaling laws for agents</title><link href="http://arxiv.org/abs/1234"/></entry>
 <entry><title>Sparse attention revisited</title><link href="http://arxiv.org/abs/5678"/></entry>
</feed>"""

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
 <item><title>Introducing something</title><link>https://openai.com/x</link></item>
 <item><title>Second post</title><link>https://openai.com/y</link></item>
</channel></rss>"""


def test_feed_parses_atom_and_rss():
    assert common._feed_items(ATOM.encode(), 5)[0] == ("Scaling laws for agents", "http://arxiv.org/abs/1234")
    assert common._feed_items(RSS.encode(), 5)[0] == ("Introducing something", "https://openai.com/x")


def test_arxiv_tier1(monkeypatch):
    monkeypatch.setattr(common, "http", FakeHTTP({"arxiv.org": ATOM}))
    got = common.fetch_arxiv()
    assert got and got[0]["tier"] == 1 and got[0]["src"] == "arXiv"


def test_failed_source_returns_empty_not_raise(monkeypatch):
    monkeypatch.setattr(common, "http", FakeHTTP({}))  # 全滅
    assert common.fetch_arxiv() == [] and common.fetch_blogs() == [] and common.fetch_gov() == []
    assert common.fetch_jobs() == [] and common.fetch_hf() == []


def test_gov_filters_unrelated_titles(monkeypatch):
    body = json.dumps({"results": [
        {"title": "Caribbean Fishery Management Council Meeting", "html_url": "u1"},
        {"title": "Framework for Artificial Intelligence Procurement", "html_url": "u2"}]})
    monkeypatch.setattr(common, "http", FakeHTTP({"federalregister.gov": body}))
    got = common.fetch_gov()
    assert len(got) == 1 and "Artificial Intelligence" in got[0]["title"]  # 漁業のノイズを混ぜない


def test_jobs_picks_newest_first(monkeypatch):
    body = json.dumps({"jobs": [
        {"title": "Old Role", "absolute_url": "a", "first_published": "2020-01-01T00:00:00-04:00"},
        {"title": "Code RL Engineer", "absolute_url": "b", "first_published": "2026-07-20T00:00:00-04:00",
         "location": {"name": "SF"}}]})
    monkeypatch.setattr(common, "http", FakeHTTP({"greenhouse.io": body}))
    got = common.fetch_jobs()
    assert got and "Code RL Engineer" in got[0]["title"] and got[0]["tier"] == 1


def test_pkg_title_has_package_name(monkeypatch):
    monkeypatch.setattr(common, "http", FakeHTTP({"pypi.org": RSS}))
    got = common.fetch_pkg()
    assert got and got[0]["title"].startswith("openai Python SDK v")  # 「2.48.0」だけの無情報を防ぐ


def test_fetch_all_keeps_secondary_sources(monkeypatch):
    # T1が大量にあってもT3(空気)が枠から消えないこと(実測で起きた事故の回帰テスト)
    monkeypatch.setattr(common, "fetch_arxiv", lambda: [
        common._art("a%d" % i, "u", "arXiv", 1) for i in range(30)])
    for name in ("fetch_blogs", "fetch_gh", "fetch_pkg", "fetch_jobs", "fetch_gov", "fetch_sec", "fetch_hf"):
        monkeypatch.setattr(common, name, lambda: [])
    monkeypatch.setattr(common, "fetch_hn", lambda: [
        {"title": "hn%d" % i, "url": "u", "meta": "", "src": "HN", "points": 100} for i in range(20)])
    monkeypatch.setattr(common, "fetch_tc", lambda: [])
    got = common.fetch_all(limit=20)
    assert sum(1 for a in got if a["src"] == "arXiv") <= common.SRC_CAP[1]   # 1ソースが独占しない
    assert any(a["src"] == "HN" for a in got)                                # 空気が残る


JP_RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
 <item><title>Screenpipeが国内で話題に</title><link>https://a</link></item>
 <item><title>先週あなたがやると言ったことは何ですか</title><link>https://b</link></item>
</channel></rss>"""


def test_jp_hits_requires_proper_noun_match(monkeypatch):
    # 固有名詞を含む見出しだけを「上陸済み」と数える(緩い全文一致の誤判定を防ぐ)
    monkeypatch.setattr(common, "http", FakeHTTP({"news.google.com": JP_RSS}))
    assert common.fetch_jp_hits("Screenpipe local agent") == ["Screenpipeが国内で話題に"]
    assert common.fetch_jp_hits("Nunchaku quantization") == []  # 無関係な見出しは数えない
    monkeypatch.setattr(common, "http", FakeHTTP({}))
    assert common.fetch_jp_hits("test") == []  # 取得失敗でも落ちない


# ---- 差分検知(時間優位) ----
def _snap(pid="anthropic/claude-x", p="0.00001", c="0.00002", exp="", name="Claude X"):
    return {pid: {"p": p, "c": c, "ctx": 100, "exp": exp, "name": name}}


def test_watch_first_run_reports_nothing():
    # 初回は全件を「新着」と誤報しない
    assert common.watch_diff({}, _snap()) == []


def test_watch_detects_price_cut_and_retirement_and_new_model():
    prev = _snap()
    cur = _snap(p="0.000005")                       # 半額
    d = common.watch_diff(prev, cur)
    assert d and "値下げ" in d[0]["title"] and d[0]["tier"] == 1

    d = common.watch_diff(prev, _snap(exp="2026-10-16"))
    assert d and "退役予定日" in d[0]["title"]        # 退役予告が最優先

    cur2 = dict(prev); cur2.update(_snap("openai/gpt-9", name="GPT-9"))
    d = common.watch_diff(prev, cur2)
    assert any("新モデル" in x["title"] and "GPT-9" in x["title"] for x in d)


def test_watch_ignores_minor_labs_and_noise():
    prev = _snap()
    cur = dict(prev); cur.update(_snap("tinylab/nano-7b", name="Nano"))   # 無名ラボの追加はノイズ
    assert common.watch_diff(prev, cur) == []
    assert common.watch_diff(prev, _snap(p="0.0000100001")) == []          # 1%未満は報じない


# ---- コンセンサスの値段(予測市場) ----
def test_markets_parse_and_filter(monkeypatch):
    manifold = json.dumps([
        {"question": "Will OpenAI ship X?", "probability": 0.23, "outcomeType": "BINARY", "url": "u1"},
        {"question": "解決済み", "probability": 0.5, "outcomeType": "BINARY", "isResolved": True},
        {"question": "多肢の市場", "probability": 0.0, "outcomeType": "MULTIPLE_CHOICE"},  # 確率が無意味
        {"question": "ほぼ確定", "probability": 0.995, "outcomeType": "BINARY"},           # 妙味なし
    ])
    poly = json.dumps([{"question": "Will Anthropic raise?", "outcomePrices": "[\"0.61\", \"0.39\"]"},
                       {"question": "サッカーの試合", "outcomePrices": "[\"0.5\",\"0.5\"]"}])  # AI無関係
    monkeypatch.setattr(common, "http", FakeHTTP({"manifold.markets": manifold,
                                                  "gamma-api.polymarket.com": poly}))
    got = common.fetch_markets()
    qs = [m["q"] for m in got]
    assert "Will OpenAI ship X?" in qs and "Will Anthropic raise?" in qs
    assert "解決済み" not in qs and "多肢の市場" not in qs and "ほぼ確定" not in qs and "サッカーの試合" not in qs
    assert all(0 < m["p"] < 1 for m in got)


def test_markets_survive_failure(monkeypatch):
    monkeypatch.setattr(common, "http", FakeHTTP({}))
    assert common.fetch_markets() == []


def test_median_resists_outlier():
    assert common.median([0.3, 0.35, 0.95]) == 0.35     # 外れ値1票に引きずられない
    assert common.median([0.2, 0.4]) == 0.30000000000000004 or abs(common.median([0.2, 0.4]) - 0.3) < 1e-9
    assert common.median([]) is None


def test_watch_step_saves_snapshot_and_survives_failure(workdir, monkeypatch):
    monkeypatch.setattr(common, "watch_fetch", lambda: _snap())
    assert common.watch_step() == []                     # 初回は差分なし
    monkeypatch.setattr(common, "watch_fetch", lambda: _snap(p="0.000002"))
    d = common.watch_step()                              # 2回目は保存済みと比較して検知
    assert d and "値下げ" in d[0]["title"]
    monkeypatch.setattr(common, "watch_fetch", lambda: {})   # 取得失敗
    assert common.watch_step() == []                     # 落ちない
