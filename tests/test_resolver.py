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


def test_hn_search_adds_upper_bound(monkeypatch):
    cap = {}

    def fake_http(url, data=None, headers=None):
        cap["url"] = url
        return json.dumps({"hits": []}).encode()

    monkeypatch.setattr(resolver, "http", fake_http)
    since = datetime(2026, 7, 1, tzinfo=resolver.JST)
    until = datetime(2026, 8, 1, tzinfo=resolver.JST)
    resolver.hn_search("openai ga", since, until)
    assert cap["url"].count("created_at_i") == 2  # 下限+上限
    resolver.hn_search("openai ga", since)  # until 無しなら下限のみ
    assert cap["url"].count("created_at_i") == 1


def test_no_source_url_is_not_auto_resolved(workdir, monkeypatch):
    # #1: 出典URLが無い高確度hitは自動確定させない(needs_reviewで保留)
    today = datetime.now(resolver.JST).date()
    y = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    (workdir / "data/hunches.json").write_text(
        json.dumps([_mk("h-nosrc", "OpenAIがGAする", y)], ensure_ascii=False), encoding="utf-8")
    (workdir / "data/records.json").write_text("[]", encoding="utf-8")
    fake = {"h-nosrc": {"result": "hit", "confidence": 0.95,
                        "evidence": {"summary": "たぶんGAした", "url": ""}}}
    fp = workdir / "fake.json"; fp.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RESOLVER_FAKE_RESPONSE", str(fp))
    resolver.main()
    h = json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))[0]
    assert h["status"] == "pending" and h["result"] is None and h["needs_review"]


def test_needs_review_not_rejudged_and_terminal(workdir, monkeypatch):
    # #3: 既 needs_review は再判定しない。期日+STALE_DAYS 超で unscorable 終端。
    now = datetime.now(resolver.JST)
    fresh = _mk("h-fresh", "近い期日", (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    fresh["needs_review"] = True                       # 期日超過わずか → 再判定しないだけ
    stale = _mk("h-stale", "古い期日", (now - timedelta(days=resolver.STALE_DAYS + 2)).strftime("%Y-%m-%d"))
    stale["needs_review"] = True                        # 期日+STALE_DAYS 超 → unscorable
    (workdir / "data/hunches.json").write_text(json.dumps([fresh, stale], ensure_ascii=False), encoding="utf-8")
    (workdir / "data/records.json").write_text("[]", encoding="utf-8")
    # fake_map は空でも良い(needs_review は judge を呼ばない)。空 map で密閉も確認。
    fp = workdir / "fake.json"; fp.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RESOLVER_FAKE_RESPONSE", str(fp))
    resolver.main()
    h = {x["id"]: x for x in json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))}
    assert h["h-fresh"]["status"] == "pending" and h["h-fresh"]["needs_review"]   # 触らない
    assert h["h-stale"]["status"] == "unscorable" and not h["h-stale"]["needs_review"]  # 終端


def test_kill_signal_does_not_remove_from_scoring(workdir, monkeypatch):
    """生存者バイアス防止(B1): 死亡シグナルが点灯した予測も、期日には通常どおり採点される。
    watchdog が needs_review を立てると resolver が二度と判定せず unscorable 化し、
    『外れそうな予測ほど成績から消える』=打率が実際より良く見える、という穴があった。"""
    today = datetime.now(resolver.JST).date()
    y = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    h = _mk("h-alert", "OpenAIがGAする", y)
    h["alert"] = True                      # 死亡シグナル点灯(表示用フラグ)
    h["signals"] = [{"date": y, "sign": "公式が延期を告知", "dir": "kill", "why": "延期が公式に出た"}]
    (workdir / "data/hunches.json").write_text(json.dumps([h], ensure_ascii=False), encoding="utf-8")
    (workdir / "data/records.json").write_text("[]", encoding="utf-8")
    fake = {"h-alert": {"result": "miss", "confidence": 0.9,
                        "evidence": {"summary": "期日までにGAされなかった", "url": "https://x/y"}}}
    fp = workdir / "fake.json"; fp.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("RESOLVER_FAKE_RESPONSE", str(fp))
    resolver.main()
    got = json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))[0]
    assert got["status"] == "resolved" and got["result"] == "miss"   # 採点された(消えていない)
    assert memory.hit_stats([got])["total"] == 1                     # 打率の母数に載る


def test_signals_are_passed_to_judge():
    # 観測された兆候が判定プロンプトに含まれる(一次材料の取りこぼし防止)
    now = datetime.now(resolver.JST)
    h = _mk("s1", "何かが起きる", (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    h["signals"] = [{"date": "2026-07-26", "sign": "料金表に載る", "dir": "confirm",
                     "why": "一覧に追加された"}]
    p = resolver._judge_prompt(h, grounded=False)
    assert "一覧に追加された" in p and "確認寄り" in p


class _QuotaClient:
    """grounding が 429 で落ち、フォールバックの素の生成だけが通るダミー。
    2026-08-05 の実障害(初の答え合わせ2件が429で失われた)の再現。"""
    last_grounding_urls = None

    def generate_grounded(self, prompt):
        raise RuntimeError("HTTPError 429: Too Many Requests")

    def generate(self, prompt, response_schema=None, model=None):
        # 実Web確認ができていないので、モデルは正直に「確認できない」と答える
        return ('{"result":"unclear","evidence":{"summary":"証拠情報を取得できなかったため",'
                '"url":""},"confidence":0.0}')


def _due_one(workdir, hid="h-429"):
    today = datetime.now(resolver.JST).date()
    y = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    h = _mk(hid, "OpenAIがGAする", y)
    (workdir / "data/hunches.json").write_text(json.dumps([h], ensure_ascii=False), encoding="utf-8")
    (workdir / "data/records.json").write_text("[]", encoding="utf-8")
    return h


def test_quota_failure_does_not_freeze_the_verdict(workdir, monkeypatch):
    """決着を落とさない: 429で実Web確認ができなかった日の unclear は needs_review にしない。

    2026-08-05、参謀の初の答え合わせ2件がこれで失われた。needs_review が立つと resolver は
    二度と判定せず、期日+STALE_DAYS で unscorable になり**永久に採点から消える**。
    世界が曖昧だったのではなく、こちらが見に行けなかっただけなので翌日やり直す。"""
    _due_one(workdir)
    monkeypatch.setattr(resolver, "GeminiClient", lambda *a, **k: _QuotaClient())
    monkeypatch.setattr(resolver, "gather_evidence", lambda h: "(証拠なし)")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    resolver.main()
    h = json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))[0]
    assert not h.get("needs_review"), "一時障害で恒久的な保留にしてはいけない"
    assert h["judge_attempts"] == 1 and h["status"] == "pending" and h["result"] is None


def test_quota_failure_gives_up_after_max_attempts(workdir, monkeypatch):
    # 無限リトライはしない。MAX_ATTEMPTS 回続けて駄目なら人に回す(毎日のチャーンを避ける)。
    _due_one(workdir)
    monkeypatch.setattr(resolver, "GeminiClient", lambda *a, **k: _QuotaClient())
    monkeypatch.setattr(resolver, "gather_evidence", lambda h: "(証拠なし)")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    for _ in range(resolver.MAX_ATTEMPTS):
        resolver.main()
    h = json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))[0]
    assert h["judge_attempts"] == resolver.MAX_ATTEMPTS and h["needs_review"]


def test_genuine_unclear_after_grounding_still_holds(workdir, monkeypatch):
    # 対比: 実Web確認が**成功した上で** unclear なら、従来どおり保留(人が確認)にする。
    _due_one(workdir, "h-amb")

    class _AmbiguousClient:
        last_grounding_urls = None

        def generate_grounded(self, prompt):
            return ('検索したが決め手がなかった。'
                    '{"result":"unclear","evidence":{"summary":"賛否が割れている","url":""},"confidence":0.2}')

    monkeypatch.setattr(resolver, "GeminiClient", lambda *a, **k: _AmbiguousClient())
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    resolver.main()
    h = json.loads((workdir / "data/hunches.json").read_text(encoding="utf-8"))[0]
    assert h["needs_review"] and h.get("judge_attempts") is None   # やり直さない


def test_judge_reports_whether_it_reached_the_web():
    now = datetime.now(resolver.JST)
    h = _mk("m1", "OpenAIがGAする", (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    meta = {}
    resolver.judge(_GroundingClient(), h, meta=meta)
    assert meta["grounded"] is True


def test_due_is_ordered_by_deadline(workdir, monkeypatch):
    # 期日を過ぎた順=待たせている順に判定する(上限で古い決着が置き去りにならない)
    today = datetime.now(resolver.JST).date()
    old = _mk("h-old", "古い", (today - timedelta(days=9)).strftime("%Y-%m-%d"))
    new = _mk("h-new", "新しい", (today - timedelta(days=1)).strftime("%Y-%m-%d"))
    (workdir / "data/hunches.json").write_text(json.dumps([new, old], ensure_ascii=False), encoding="utf-8")
    (workdir / "data/records.json").write_text("[]", encoding="utf-8")
    seen = []
    monkeypatch.setattr(resolver, "MAX_PER_RUN", 1)
    monkeypatch.setattr(resolver, "judge", lambda c, h, fake=None, meta=None: seen.append(h["id"]))
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setattr(resolver, "GeminiClient", lambda *a, **k: object())
    resolver.main()
    assert seen == ["h-old"]


def test_resolved_are_idempotent(workdir, monkeypatch):
    fp = _setup(workdir)
    monkeypatch.setenv("RESOLVER_FAKE_RESPONSE", str(fp))
    resolver.main()
    before = (workdir / "data/hunches.json").read_text(encoding="utf-8")
    resolver.main()
    after = (workdir / "data/hunches.json").read_text(encoding="utf-8")
    assert before == after  # 確定分は再実行で不変
