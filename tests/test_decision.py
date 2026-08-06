"""個人参謀の中核制約を純関数で固定する。"""
import decision


def test_action_gate_rejects_pc_work_and_keeps_iphone_work():
    assert decision.action_problem("GPUでvLLMをローカル運用する")
    assert decision.action_problem("Docker環境を構築する")
    assert decision.action_problem("Pythonスクリプトを実行する")
    assert decision.action_problem("AIの最新動向を調べる") == "成果物が残らない"
    assert decision.action_problem("一次情報を読み要点を3行残す") is None
    assert decision.action_problem("GitHubの改善候補を1件Issueに書く") is None


def test_safe_moves_filters_duplicates_and_fills_three():
    got = decision.safe_moves([
        {"t": "GPUでvLLMを試す", "why": "高速だから"},
        {"t": "GitHubの改善候補を1件Issueに書く", "why": "次を固定する"},
        {"t": "GitHubの改善候補を1件Issueに書く", "why": "重複"},
    ])
    assert len(got) == 3
    assert got[0]["t"] == "GitHubの改善候補を1件Issueに書く"
    assert all("GPU" not in x["t"] for x in got)
    assert len({x["t"] for x in got}) == 3


def test_duplicate_claim_detects_near_copy():
    old = [{"id": "h1", "status": "pending",
            "claim": "AnthropicがClaude Codeのトークン消費を削減すると発表する"}]
    hid, score = decision.duplicate_claim(
        "AnthropicがClaude Codeのトークン消費削減を正式発表する", old)
    assert hid == "h1" and score >= 0.58


def test_select_and_apply_canonical_hunch():
    huns = [
        {"id": "weak", "created_at": "2026-08-06T01:00:00+09:00", "status": "pending",
         "claim": "弱い予測", "resolution": {"source": ""}},
        {"id": "strong", "created_at": "2026-08-06T02:00:00+09:00", "status": "pending",
         "claim": "採点可能な予測", "confidence": .42, "base_rate": .2,
         "confidence_why": "公式ページの変更", "counter": "延期告知",
         "resolution": {"source": "https://example.com/news"},
         "indicators": [{"sign": "価格表掲載", "dir": "confirm"}], "based_on": ["r1"]},
    ]
    chosen = decision.select_canonical_hunch(huns, "2026-08-06")
    assert chosen["id"] == "strong"
    final = decision.apply_canonical_hunch({"kan": "別の勘"}, chosen)
    assert final["kan"] == "採点可能な予測"
    assert final["kan_id"] == "strong" and final["kan_base_rate"] == .2


def test_feedback_issue_parser_and_digest():
    issue = {
        "number": 7,
        "title": "[一手フィードバック] 2026-08-06",
        "body": "<!-- stick-koubou-action-feedback -->\n"
                "date: 2026-08-06\naction: Issueを1件書く\nresult: blocked\n"
                "category: build\ngoal_advanced: no\nactual_value_band: unset\n"
                "goal_stage: build\nbottleneck: time\ndesired_outcome: complete\n"
                "risk_mode: balanced\nminutes_band: under-30\nnote: 権限で詰まった",
        "html_url": "https://github.com/example/repo/issues/7",
    }
    event = decision.parse_feedback_issue(issue)
    assert event == {"kind": "action", "id": 7, "date": "2026-08-06", "action": "Issueを1件書く",
                     "result": "blocked", "category": "build", "goal_advanced": "no",
                     "note": "権限で詰まった",
                     "budget_band": "", "planned_cost_band": "", "actual_cost_band": "",
                     "actual_value_band": "unset",
                     "state": {"goal_stage": "build", "bottleneck": "time",
                               "desired_outcome": "complete", "risk_mode": "balanced",
                               "minutes_band": "under-30"},
                     "url": "https://github.com/example/repo/issues/7"}
    digest = decision.feedback_digest([event])
    assert "完了0/1件" in digest and "blocked" in digest and "繰り返すな" in digest


def test_feedback_parser_ignores_unrelated_or_invalid_issue():
    assert decision.parse_feedback_issue({"title": "普通のIssue", "body": ""}) is None
    assert decision.parse_feedback_issue({
        "title": "[一手フィードバック] x", "body": "action: x\nresult: success"}) is None


def test_fetch_feedback_accepts_only_repository_owner(monkeypatch):
    import json

    def issue(number, author):
        return {"number": number, "title": "[一手フィードバック] 2026-08-06",
                "body": "date: 2026-08-06\naction: Issueを書く\nresult: done",
                "html_url": "https://example.com/%d" % number,
                "user": {"login": author}}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps([issue(1, "tensaikamo"), issue(2, "attacker")]).encode()

    monkeypatch.setattr(decision.urllib.request, "urlopen", lambda *a, **k: Response())
    got = decision.fetch_action_feedback("tensaikamo/stick-koubou")
    assert [x["id"] for x in got] == [1]


def paid_move(**overrides):
    move = {
        "t": "有料AI機能を1か月比較表にする",
        "why": "実装速度への効果を測る",
        "cost_min": 2000,
        "cost_max": 3000,
        "loss_max": 3000,
        "success_p": 0.8,
        "success_p_min": 0.65,
        "success_p_max": 0.9,
        "success_why": "同じ作業で所要時間を比較できる",
        "payback_days": 30,
        "value_score": 5,
        "learning_value": 5,
        "time_minutes": 30,
        "outcome": "無料版との比較表が残る",
        "category": "buy",
        "impact_min": 0.05,
        "impact_max": 0.15,
        "impact_why": "完成速度が上がれば目標達成に寄与する",
        "evidence_ids": ["user-goal", "action-history"],
        "assumptions": ["週2回以上使う"],
        "disconfirm": "完成件数が増えない",
        "continue_if": "月2件以上多く完成する",
        "stop": "30日で差がなければ解約する",
    }
    move.update(overrides)
    return move


def test_budget_gate_enforces_remaining_per_action_risk_and_exit_rules():
    budget = {"total_yen": 10000, "spent_yen": 7000, "per_action_yen": 3000,
              "risk_limit_yen": 3000, "period_months": 3}
    assert decision.budget_problem(paid_move(), budget) is None
    assert decision.budget_problem(paid_move(cost_max=3001), budget) == "残額を超える"
    assert decision.budget_problem(paid_move(cost_max=2600),
                                   dict(budget, spent_yen=6000, per_action_yen=2500)) == "1回の上限を超える"
    assert decision.budget_problem(paid_move(cost_max=2500, loss_max=3001), budget) == "許容損失を超える"
    assert decision.budget_problem(paid_move(cost_max=2500, stop=""), budget) == "有料案に撤退条件がない"
    assert decision.budget_problem(paid_move(cost_max=2500, continue_if=""), budget) == "有料案に続行条件がない"


def test_zero_limits_mean_free_only_not_unlimited():
    budget = {"total_yen": 10000, "spent_yen": 0, "per_action_yen": 0,
              "risk_limit_yen": 0, "period_months": 6}
    assert decision.budget_problem(paid_move(), budget) == "1回の上限を超える"


def test_high_value_paid_move_can_outrank_low_value_free_move():
    free = paid_move(t="短いメモを1行残す", cost_min=0, cost_max=0, loss_max=0,
                     success_p=0.3, value_score=1, learning_value=0, payback_days=0)
    budget = {"total_yen": 10000, "spent_yen": 0, "per_action_yen": 5000,
              "risk_limit_yen": 5000, "period_months": 6}
    ranked = decision.rank_moves([free, paid_move()], budget)
    assert [m["t"] for m in ranked] == [paid_move()["t"], free["t"]]
    assert ranked[0]["decision_score"] > ranked[1]["decision_score"]


def test_expected_value_is_a_range_not_a_fake_single_number():
    move = paid_move(success_p_min=0.5, success_p_max=0.8,
                     impact_min=0.05, impact_max=0.15)
    ev = decision.expected_value_range(move, 100000)
    assert ev == {"min_yen": -500, "max_yen": 10000, "mid_yen": 4750}
    ranked = decision.rank_moves([move],
                                 {"total_yen": 10000, "per_action_yen": 5000,
                                  "risk_limit_yen": 5000, "period_months": 6},
                                 goal_value_yen=100000)
    assert ranked[0]["expected_value"] == ev


def test_intelligence_gate_rejects_unsupported_or_invented_evidence():
    assert decision.move_quality_problem(paid_move(evidence_ids=[])) == "有料・高確度案に証拠IDがない"
    assert decision.move_quality_problem(paid_move(evidence_ids=["made-up"]), {"r1"}) == \
        "存在しない証拠IDを参照している"
    assert decision.move_quality_problem(paid_move(evidence_ids=["r1"]), {"r1"}) is None
    assert decision.move_quality_problem(paid_move(evidence_ids=["r1"]), {"r1"}, set()) == \
        "有料・高確度案に確認済み証拠がない"
    assert decision.move_quality_problem(paid_move(evidence_ids=["r1"]), {"r1"}, {"r1"}) is None
    assert decision.move_quality_problem(paid_move(evidence_ids=["action-history"]), set(), set()) == \
        "有料・高確度案に確認済み証拠がない"
    assert decision.move_quality_problem(paid_move(evidence_ids=["action-history"]), set(),
                                         {"action-history"}) is None
    assert decision.move_quality_problem(paid_move(evidence_ids=["user-goal"])) == \
        "有料・高確度案に確認済み証拠がない"
    assert decision.move_quality_problem(paid_move(disconfirm="")) == "価値仮説の反証条件がない"
    assert decision.move_quality_problem(paid_move(impact_max=0.8, terminal=False)) == \
        "30分の一手として目標寄与を過大評価している"
    assert decision.move_quality_problem(paid_move(impact_max=0.8, terminal=True)) is None


def test_action_history_calibrates_slowly_instead_of_overfitting():
    events = [{"kind": "action", "category": "buy", "result": "blocked", "goal_advanced": "no"}
              for _ in range(3)]
    stats = decision.action_category_stats(events)
    assert stats["buy"]["posterior_p"] == 0.2
    calibrated = decision.calibrate_moves([paid_move(success_p=0.8)], events)[0]
    assert 0.5 < calibrated["success_p"] < 0.8
    assert "弱く較正" in calibrated["success_why"]


def test_budget_issue_parser_and_digest_keep_only_bands():
    event = decision.parse_budget_issue({
        "number": 8,
        "title": "[参謀設定] 予算帯 2026-08-06",
        "body": "date: 2026-08-06\nbudget_band: standard\nperiod_months: 6\n"
                "per_action_band: under-5000\nrisk_band: under-5000\n"
                "goal_stage: build\nbottleneck: time\ndesired_outcome: complete\n"
                "risk_mode: balanced\nminutes_band: under-30",
        "html_url": "https://github.com/example/repo/issues/8",
    })
    assert event["kind"] == "budget" and event["budget_band"] == "standard"
    digest = decision.feedback_digest([event])
    assert "正確な金額は非公開" in digest and "standard" in digest
    assert "段階build" in digest and "詰まりtime" in digest and "under-30" in digest
    assert "10000" not in digest


def test_iphone_ui_has_real_budget_cycle_and_recoverable_local_backup():
    from pathlib import Path

    docs = Path(__file__).parents[1] / "docs"
    html = (docs / "ichite.html").read_text(encoding="utf-8")
    engine = (docs / "decision-engine.js").read_text(encoding="utf-8")
    for required in ("budgetTotal", "budgetMonths", "budgetStart", "budgetPer", "budgetRisk",
                     "cycleEnd", "payback_days", "success_why", "continue_if",
                     "stick-koubou-backup-", "restoreFile", "ownCost", "ownLoss",
                     "ownPayback", "ownContinue", "ownStop", "committed(excludeDate)", "予約中",
                     "goalTitle", "goalMetric", "goalValue", "goalDeadline", "expectedValue",
                     "carryValue", "carryAdvance", "actual_value_band", "goal_advanced",
                     "stateStage", "stateBottleneck", "stateOutcome", "stateMinutes", "stateRisk",
                     "recommended", "move_fit", "ownMinutes", "今日は決めない", "decision-engine.js"):
        assert required in html + engine


def test_static_moves_carry_intelligence_fields_and_pass_quality_gate():
    import json
    from pathlib import Path

    moves = json.loads((Path(__file__).parents[1] / "docs" / "ichite.json").read_text(encoding="utf-8"))["moves"]
    required = {"success_p_min", "success_p_max", "category", "terminal", "impact_min",
                "impact_max", "impact_why", "evidence_ids", "assumptions", "disconfirm"}
    assert len(moves) == 5
    for move in moves:
        assert required <= set(move)
        assert decision.move_quality_problem(move) is None
