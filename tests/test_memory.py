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


def test_dedupe_by_url_removes_fake_continuity():
    """同じ記事を毎日拾い直した分を落とす(実測で重複率54%・スレッドが水増しされていた)。
    履歴自体は消さず、読む側で落とす。"""
    recs = [_rs("a", "和解が承認", "settle", "https://n/1", 3),
            _rs("b", "和解が承認(再掲)", "settle", "https://n/1", 2),   # 同一URL
            _rs("c", "和解が承認(再々掲)", "settle", "https://n/1", 1),  # 同一URL
            _rs("d", "別のニュース", "other", "https://n/2", 1)]
    assert len(memory.dedupe_by_url(recs)) == 2
    # スレッドが「1つの話題が3日続いた」ように見えないこと
    th = dict(memory.all_threads(recs, min_len=2))
    assert not any(len(evs) > 2 for evs in th.values())


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


def test_brier_decomposition_separates_calibration_from_discrimination():
    """合計の Brier だけでは『自信の付け方が下手』と『どれが当たるか区別できていない』を
    区別できない。実測で確度が0.60〜0.85に潰れていた(標準偏差0.06)ので後者が疑わしく、
    どちらを直すべきかを数字で出せるようにする。"""
    # (A) 全部同じ確度 → 判別力ゼロ(何も予測していないのと同じ)
    flat = [_h("a", "x", "hit", 0.7, "resolved"), _h("b", "y", "miss", 0.7, "resolved"),
            _h("c", "z", "hit", 0.7, "resolved"), _h("d", "w", "miss", 0.7, "resolved")]
    pa = memory.brier_parts(flat)
    assert pa["n"] == 4 and pa["resolution"] < 1e-9

    # (B) 当たる方を高く・外す方を低く → 判別力が出る
    sharp = [_h("a", "x", "hit", 0.9, "resolved"), _h("b", "y", "miss", 0.1, "resolved"),
             _h("c", "z", "hit", 0.9, "resolved"), _h("d", "w", "miss", 0.1, "resolved")]
    pb = memory.brier_parts(sharp)
    assert pb["resolution"] > pa["resolution"]
    assert pb["reliability"] < 0.05          # 言った確度どおりに当たっている
    assert pb["brier"] < pa["brier"]

    # 分解は恒等式 Brier = 較正誤差 − 判別力 + 不確実性 を満たす
    for p in (pa, pb):
        assert abs(p["brier"] - (p["reliability"] - p["resolution"] + p["uncertainty"])) < 1e-9
    assert memory.brier_parts([])["n"] == 0


def test_digest_warns_about_flat_confidence_and_one_sided_claims():
    flat = [_h(str(i), "OpenAIが新機能を出す", "hit" if i % 2 else "miss", 0.7, "resolved")
            for i in range(6)]
    d = memory.build_digest([], flat)
    assert "判別力がほぼゼロ" in d          # 全部同じ確度
    assert "起きない方に賭ける勇気" in d    # 「起きる」型しかない
    # 否定型が十分あれば偏りの警告は出ない
    mixed = flat[:3] + [_h("n%d" % i, "OpenAIは期日までに公開しない", "hit", 0.7, "resolved")
                        for i in range(3)]
    assert "起きない方に賭ける勇気" not in memory.build_digest([], mixed)


def test_confidence_moves_on_signal_and_stays_bounded():
    """指標が点灯したら確度を1段動かす(逐次ベイズ更新)。従来は点灯を表示するだけで、
    確度は作成日のまま固まっていた。Brier は確度の正しさを測るので、期日に近いほど
    確度が実態へ寄る方がスコアも良くなる。"""
    up = memory.update_confidence(0.70, "confirm")
    dn = memory.update_confidence(0.70, "kill")
    assert up > 0.70 > dn
    # ログオッズ空間なので上下は対称(0.7から同じ幅だけ動く)
    assert abs((up - 0.70) - (0.70 - dn)) < 0.02
    # 0/1 に振り切らない(振り切ると Brier が壊滅的に効く)
    c = 0.9
    for _ in range(30):
        c = memory.update_confidence(c, "confirm")
    assert c <= memory.CONF_CEIL
    c = 0.5
    for _ in range(30):
        c = memory.update_confidence(c, "kill")
    assert c >= memory.CONF_FLOOR
    # すでに確信している読みは小さな兆候で揺れにくい(中央付近より動きが小さい)
    assert (memory.update_confidence(0.92, "confirm") - 0.92) < \
           (memory.update_confidence(0.50, "confirm") - 0.50)


def test_confidence_update_is_safe_on_garbage():
    assert memory.update_confidence(None, "confirm") is None
    assert memory.update_confidence("x", "kill") == "x"
    assert memory.update_confidence(0.7, "unknown") == 0.7   # 未知の向きは動かさない


def test_next_due_none_when_all_past_or_empty():
    today = datetime.now(JST).date()
    assert memory.next_due([], today) is None
    h = _h("a", "x", status="pending")
    h["deadline"] = (datetime.now(JST) - timedelta(days=3)).strftime("%Y-%m-%d")  # 過去
    assert memory.next_due([h], today) is None
