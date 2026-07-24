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
