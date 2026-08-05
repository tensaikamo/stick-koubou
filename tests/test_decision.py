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
                "date: 2026-08-06\naction: Issueを1件書く\nresult: blocked\nnote: 権限で詰まった",
        "html_url": "https://github.com/example/repo/issues/7",
    }
    event = decision.parse_feedback_issue(issue)
    assert event == {"id": 7, "date": "2026-08-06", "action": "Issueを1件書く",
                     "result": "blocked", "note": "権限で詰まった",
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
