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


def test_jp_hits_measures_untranslated(monkeypatch):
    monkeypatch.setattr(common, "http", FakeHTTP({"news.google.com": RSS}))
    assert common.fetch_jp_hits("test") == ["Introducing something", "Second post"]
    monkeypatch.setattr(common, "http", FakeHTTP({}))
    assert common.fetch_jp_hits("test") == []  # 取得失敗でも落ちない
