from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import memory

JST = ZoneInfo("Asia/Tokyo")


def _h(hid, claim, result=None, conf=0.7, status="pending", subj="OpenAI"):
    now = datetime.now(JST)
    return {"id": hid, "created_at": (now - timedelta(days=5)).isoformat(), "claim": claim,
            "prose": claim + "と見る。", "subject": subj, "confidence": conf, "status": status,
            "result": result, "deadline": (now + timedelta(days=5)).strftime("%Y-%m-%d"),
            "based_on": ["r1"], "resolution": {"decider": "公式発表"},
            "evidence": ({"summary": "公式が告知", "url": "https://x"} if result else None)}


def _r(rid, headline, title="", days_ago=0):
    now = datetime.now(JST)
    return {"id": rid, "created_at": (now - timedelta(days=days_ago)).isoformat(),
            "headline": headline, "source": {"title": title}}


def test_hit_stats_excludes_unclear_and_pending():
    huns = [_h("a", "x", "hit", 0.8, "resolved"), _h("b", "y", "miss", 0.85, "resolved"),
            _h("c", "z", None, 0.7, "pending"), _h("d", "w", "unclear", 0.3, "pending")]
    st = memory.hit_stats(huns)
    assert st["hit"] == 1 and st["miss"] == 1 and st["total"] == 2
    assert round(st["rate"] * 100) == 50 and st["pending"] == 2


def test_brier_scores_confidence_not_just_hits():
    # 確度0.9でhit → (0.9-1)^2=0.01 / 確度0.8でmiss → 0.64 → 平均0.325
    huns = [_h("a", "x", "hit", 0.9, "resolved"), _h("b", "y", "miss", 0.8, "resolved")]
    b = memory.brier(huns)
    assert b["n"] == 2 and abs(b["score"] - 0.325) < 1e-9
    # 自信満々で当てる方が良いスコア(小さい)になる
    assert memory.brier([_h("c", "z", "hit", 0.95, "resolved")])["score"] < \
           memory.brier([_h("d", "w", "hit", 0.55, "resolved")])["score"]
    # 未確定・判定不能は母数外
    assert memory.brier([_h("e", "v", None, 0.7, "pending"),
                         _h("f", "u", "unclear", 0.3, "pending")]) == {"score": None, "n": 0}


def test_digest_zero_data_is_safe_string():
    d = memory.build_digest([], [])
    assert isinstance(d, str)  # 空データでも例外なく文字列


def test_digest_with_results_mentions_calibration():
    huns = [_h("a", "x", "hit", 0.8, "resolved"), _h("b", "y", "miss", 0.85, "resolved")]
    d = memory.build_digest([], huns)
    assert "的中率" in d and "較正" in d


def test_related_ids_matches_same_subject_only():
    recs = [_r("2026-07-22-r01", "OpenAI launches tier", "OpenAI")]
    assert memory.related_ids_for("OpenAI adds a feature", "OpenAI", recs) == ["2026-07-22-r01"]
    assert memory.related_ids_for("Totally unrelated topic", "", recs) == []


def test_threads_group_by_subject():
    recs = [_r("r1", "OpenAI ships A"), _r("r2", "OpenAI ships B")]
    th = memory.threads(recs)
    assert any(s == "OpenAI" and len(evs) >= 2 for s, evs in th)


def test_entities_word_boundary_no_false_positive():
    assert memory.entities_of("New metadata format for schemas") == set()
    assert memory.entities_of("Meta releases a model") == {"Meta"}


def test_entities_alias_resolution():
    assert memory.entities_of("Alphabet earnings and Gemini update") == {"Google"}
    assert memory.entities_of("Kimi K3 by Moonshot") == {"Moonshot"}
    assert memory.entities_of("ChatGPT gets ads") == {"OpenAI"}
    assert "Meta" in memory.entities_of("Llama 4 released")


def _rs(rid, headline, title, url, days_ago):
    now = datetime.now(JST)
    return {"id": rid, "created_at": (now - timedelta(days=days_ago)).isoformat(),
            "headline": headline, "source": {"title": title, "url": url}}


def test_all_threads_groups_orders_and_carries_url():
    now = datetime.now(JST)
    d0 = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    d1 = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    recs = [_rs("a", "OpenAI ships A", "OpenAI", "https://o/a", 3),
            _rs("b", "OpenAI ships B", "OpenAI", "https://o/b", 1),
            _rs("c", "Anthropic ships once", "Anthropic", "", 0)]
    th = dict(memory.all_threads(recs))
    # 2件以上ある主体だけがスレッド化(単発の Anthropic は除外)
    assert "OpenAI" in th and "Anthropic" not in th
    assert len(th["OpenAI"]) == 2
    # 時系列昇順(古い→新しい)で url を保持
    assert th["OpenAI"][0][0] == d0 and th["OpenAI"][0][2] == "https://o/a"
    assert th["OpenAI"][1][0] == d1 and th["OpenAI"][1][2] == "https://o/b"


def test_next_due_returns_nearest_future():
    today = datetime.now(JST).date()
    huns = [_h("a", "x", status="pending"),   # deadline = today+5
            _h("b", "y", status="pending")]
    huns[1]["deadline"] = (datetime.now(JST) + timedelta(days=2)).strftime("%Y-%m-%d")
    nd = memory.next_due(huns, today)
    assert nd is not None and nd[1] == 2  # 最近接=2日後


def test_next_due_none_when_all_past_or_empty():
    today = datetime.now(JST).date()
    assert memory.next_due([], today) is None
    h = _h("a", "x", status="pending")
    h["deadline"] = (datetime.now(JST) - timedelta(days=3)).strftime("%Y-%m-%d")  # 過去
    assert memory.next_due([h], today) is None
