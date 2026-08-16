"""AutoLoop public controllerのfail-closed契約をbehaviorで守る。"""
import copy
import importlib.util
import json
import os
import pathlib
import subprocess
import urllib.request

import pytest
from jsonschema import Draft202012Validator


ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "experiments" / "counterevidence" / "autoloop.py"
SPEC = importlib.util.spec_from_file_location("counterevidence_autoloop", MODULE_PATH)
AUTO = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(AUTO)


def digest(label):
    return AUTO.sha256_text(label)


def generator(**changes):
    value = {"provider": "anthropic", "model": "claude-sonnet-5",
             "route": "subscription_oauth", "session_id": "claude-session-1",
             "verification": "unverifiable_subscription"}
    value.update(changes)
    return value


def judge(**changes):
    value = {"provider": "openai", "model": "gpt-5.6-sol",
             "route": "chatgpt_managed", "session_id": "codex-session-1",
             "verification": "unverifiable_subscription"}
    value.update(changes)
    return value


def pending_call(call_id="A-01", role="generator", stage="A", **changes):
    value = AUTO.new_call(call_id, role, stage, digest("prompt:" + call_id), **changes)
    return value


def complete_call(call_id="A-01", role="generator", stage="A", response="{}", **changes):
    call = pending_call(call_id, role, stage, **changes)
    call.update({"response_sha256": digest(response),
                 "response_utf8_bytes": len(response.encode("utf-8")),
                 "status": "complete", "attempts": 1})
    return call


def completed_pipeline_calls():
    generated = complete_call(response='{"answer":"x"}')
    final_judge = complete_call(
        "J001", role="judge", stage="FINAL_VERDICT", response='{"verdict":"correct"}',
        source_call_id=generated["call_id"],
        source_response_sha256=generated["response_sha256"],
    )
    unsupported_judge = complete_call(
        "J002", role="judge", stage="UNSUPPORTED", response='{"unsupported":false}',
        source_call_id=generated["call_id"],
        source_response_sha256=generated["response_sha256"],
    )
    return [generated, final_judge, unsupported_judge]


def manifest(calls=None, **changes):
    target_state = changes.pop("state", None)
    value = AUTO.new_manifest(
        "run-001", generator(), judge(), calls or [],
        plan_source_sha256=digest("frozen-plan-source"),
    )
    if changes:
        value = copy.deepcopy(value)
        value.update(changes)
        value = AUTO.seal_manifest(value)
    if target_state is not None:
        target_index = AUTO.ACTIVE_STATES.index(target_state)
        for next_state in AUTO.ACTIVE_STATES[1:target_index + 1]:
            value = AUTO.transition(value, next_state)
    return value


def force_late_state_for_negative_test(value, state):
    """chain検証ではなく、late-stateの分母gate単体を試すfixture。"""
    candidate = copy.deepcopy(value)
    candidate["state"] = state
    candidate["sequence"] = AUTO.MIN_SEQUENCE_BY_STATE[state]
    candidate["previous_checkpoint_sha256"] = digest("prior checkpoint")
    return AUTO.seal_manifest(candidate)


def active_chain(calls, target_state):
    current = manifest(calls)
    chain = [current]
    target_index = AUTO.ACTIVE_STATES.index(target_state)
    for next_state in AUTO.ACTIVE_STATES[1:target_index + 1]:
        current = AUTO.transition(current, next_state)
        chain.append(current)
    return chain


@pytest.mark.parametrize("name", [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "XAI_API_KEY",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_CUSTOM_HEADERS",
    "OPENAI_CUSTOM_HEADERS",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "CLOUD_ML_REGION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "GOOGLE_CLOUD_PROJECT",
    "AZURE_CLIENT_SECRET",
    "PROJECT_EXTRA_USAGE_ENABLED",
    "MODEL_PAYG",
    "BILLING_AUTO_TOP_UP",
])
def test_paid_or_override_routes_are_blocked_without_printing_values(name):
    secret = "do-not-print-this-value"
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.require_safe_billing_environment({name: secret})
    assert caught.value.code == "BLOCKED_BILLING_ROUTE"
    assert name in str(caught.value)
    assert secret not in str(caught.value)


@pytest.mark.parametrize("credential", ["CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME", "CODEX_AUTH_JSON"])
def test_subscription_auth_is_refused_on_public_control_plane(credential):
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_execution_context("public_control", "public", {credential: "configured"})
    assert caught.value.code == "PUBLIC_AUTH_REFUSED"


def test_credential_bearing_plane_must_be_private():
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_execution_context(
            "private_trusted", "public", {"CLAUDE_CODE_OAUTH_TOKEN": "configured"}
        )
    assert caught.value.code == "PRIVATE_RUNNER_REQUIRED"
    AUTO.validate_execution_context(
        "private_trusted", "private",
        {"CLAUDE_CODE_OAUTH_TOKEN": "configured", "CODEX_HOME": "/trusted/codex"},
    )


def test_private_runner_requires_both_subscription_auth_routes():
    for env in ({}, {"CLAUDE_CODE_OAUTH_TOKEN": "configured"}, {"CODEX_HOME": "/trusted/codex"}):
        with pytest.raises(AUTO.AutoLoopError) as caught:
            AUTO.validate_execution_context("private_trusted", "private", env)
        assert caught.value.code == "BLOCKED_AUTH"


def test_only_standard_provider_base_urls_are_allowed():
    AUTO.require_safe_billing_environment({
        "ANTHROPIC_BASE_URL": "api.anthropic.com",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
    })
    for name in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        with pytest.raises(AUTO.AutoLoopError) as caught:
            AUTO.require_safe_billing_environment({name: "https://paid-proxy.example/v1"})
        assert caught.value.code == "BLOCKED_BILLING_ROUTE"
        assert "paid-proxy.example" not in str(caught.value)


def test_secret_material_cannot_enter_manifest_or_receipt():
    for payload in (
        {"api_key": "plain"},
        {"token": "plain"},
        {"credentials": "plain"},
        {"nested": {"oauth_token": "plain"}},
        {"note": "Bearer abcdefghijklmnopqrstuvwxyz"},
        {"note": "sk-ant-abcdefghijklmnop"},
        {"note": "github_pat_abcdefghijklmnop"},
        {"note": "oat01-abcdefghijklmnop"},
        {"note": "eyJabcdefghijk.abcdefghijk.abcdefghijk"},
    ):
        with pytest.raises(AUTO.AutoLoopError) as caught:
            AUTO.assert_secret_free(payload)
        assert caught.value.code == "SECRET_MATERIAL"


def test_quota_pause_and_resume_preserve_route_model_and_calls():
    before = manifest([pending_call()], state="GENERATING")
    paused = AUTO.pause_for_quota(before)
    assert paused["state"] == "PAUSED_QUOTA"
    assert paused["paused_from"] == "GENERATING"
    assert paused["calls"] == before["calls"]
    resumed = AUTO.resume_after_quota(
        paused, execution_plane="public_control", repository_visibility="public", env={}
    )
    assert resumed["state"] == "GENERATING"
    assert resumed["generator"] == before["generator"]
    assert resumed["judge"] == before["judge"]
    assert resumed["calls"] == before["calls"]
    AUTO.validate_resume(before, paused)
    AUTO.validate_resume(paused, resumed)


def test_recorded_response_is_append_only_and_never_reexecuted():
    before = manifest([pending_call()], state="GENERATING")
    after = AUTO.record_response(before, "A-01", '{"answer":"first"}')
    AUTO.validate_resume(before, after)
    assert after["calls"][0]["attempts"] == 1
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.record_response(after, "A-01", '{"answer":"better second try"}')
    assert caught.value.code == "IMMUTABLE_CALL"


def test_checkpoint_chain_requires_genesis_and_every_intermediate_update():
    genesis = manifest([pending_call()])
    generating = AUTO.transition(genesis, "GENERATING")
    recorded = AUTO.record_response(generating, "A-01", '{"answer":"first"}')
    AUTO.validate_chain([genesis, generating, recorded])
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_chain([genesis, recorded])
    assert caught.value.code == "RESUME_DRIFT"
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_chain([generating, recorded])
    assert caught.value.code == "INVALID_PROVENANCE"


def test_malformed_output_is_retained_and_fails_closed_without_retry():
    for bad in ("not-json", ""):
        before = manifest([pending_call()], state="GENERATING")
        after = AUTO.record_response(before, "A-01", bad)
        call = after["calls"][0]
        assert after["state"] == "INVALID_OUTPUT"
        assert after["verdict"] is None
        assert call["status"] == "invalid" and call["attempts"] == 1
        assert call["response_sha256"] == digest(bad)
        with pytest.raises(AUTO.AutoLoopError) as caught:
            AUTO.record_response(after, "A-01", "{}")
        assert caught.value.code == "IMMUTABLE_CALL"


def test_checkpoint_detects_response_or_state_tampering():
    sealed = manifest([complete_call()], state="GENERATING")
    tampered = copy.deepcopy(sealed)
    tampered["calls"][0]["response_sha256"] = digest("replacement")
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_manifest(tampered)
    assert caught.value.code == "INVALID_PROVENANCE"


def test_c2_is_bound_to_the_actual_completed_c1_response():
    c1 = complete_call("CASE__C1", stage="C1", response='{"provisional":"x"}')
    c2 = pending_call(
        "CASE__C2", stage="C2",
        derivation={"c1_call_id": "CASE__C1", "c1_response_sha256": c1["response_sha256"]},
    )
    good = manifest([c1, c2], state="GENERATING")
    AUTO.validate_manifest(good)
    substituted = copy.deepcopy(good)
    substituted["calls"][1]["derivation"]["c1_response_sha256"] = digest("other C1")
    substituted = AUTO.seal_manifest(substituted)
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_manifest(substituted)
    assert caught.value.code == "INVALID_PROVENANCE"


def test_judge_call_is_bound_to_an_existing_completed_generator_call_and_sha():
    AUTO.validate_manifest(manifest(completed_pipeline_calls()))
    bad = pending_call(
        "J003", role="judge", stage="FINAL_VERDICT",
        source_call_id="missing", source_response_sha256=digest("invented"),
    )
    source = complete_call()
    other = pending_call(
        "J004", role="judge", stage="UNSUPPORTED",
        source_call_id=source["call_id"], source_response_sha256=source["response_sha256"],
    )
    with pytest.raises(AUTO.AutoLoopError) as caught:
        manifest([source, bad, other])
    assert caught.value.code == "INVALID_PROVENANCE"


def test_judge_cannot_target_invalid_or_another_judge_response():
    invalid_source = complete_call(response="")
    invalid_source["status"] = "invalid"
    bad = pending_call(
        "J001", role="judge", stage="FINAL_VERDICT",
        source_call_id=invalid_source["call_id"],
        source_response_sha256=invalid_source["response_sha256"],
    )
    other = pending_call(
        "J002", role="judge", stage="UNSUPPORTED",
        source_call_id=invalid_source["call_id"],
        source_response_sha256=invalid_source["response_sha256"],
    )
    with pytest.raises(AUTO.AutoLoopError) as caught:
        manifest([invalid_source, bad, other])
    assert caught.value.code == "INVALID_PROVENANCE"

    calls = completed_pipeline_calls()
    calls[2]["source_call_id"] = calls[1]["call_id"]
    calls[2]["source_response_sha256"] = calls[1]["response_sha256"]
    with pytest.raises(AUTO.AutoLoopError) as caught:
        manifest(calls)
    assert caught.value.code == "INVALID_PROVENANCE"


def test_each_judge_stage_covers_every_eligible_generator_exactly_once():
    first = complete_call("A-01", stage="A", response='{"answer":"a"}')
    second = complete_call("B-01", stage="B", response='{"answer":"b"}')
    calls = [first, second]
    for index, source in enumerate((first, first), start=1):
        calls.append(complete_call(
            f"JF{index}", role="judge", stage="FINAL_VERDICT", response='{"verdict":"correct"}',
            source_call_id=source["call_id"], source_response_sha256=source["response_sha256"],
        ))
    for index, source in enumerate((first, second), start=1):
        calls.append(complete_call(
            f"JU{index}", role="judge", stage="UNSUPPORTED", response='{"unsupported":false}',
            source_call_id=source["call_id"], source_response_sha256=source["response_sha256"],
        ))
    with pytest.raises(AUTO.AutoLoopError) as caught:
        manifest(calls)
    assert caught.value.code == "INVALID_PROVENANCE"


def test_receipt_is_bound_to_executor_prompt_and_response_without_secrets():
    response = '{"answer":"x"}'
    current = manifest([pending_call()], state="GENERATING")
    receipt = {
        "schema_version": AUTO.RECEIPT_SCHEMA_VERSION,
        "run_id": current["run_id"],
        "call_id": "A-01",
        **current["generator"],
        "prompt_sha256": current["calls"][0]["prompt_sha256"],
        "response_sha256": digest(response),
        "response_utf8_bytes": len(response.encode("utf-8")),
        "status": "complete",
        "collected_at_utc": "2026-08-16T00:00:00Z",
        "time_verification": "unverifiable_runner_clock",
        "checkpoint_sha256": current["checkpoint_sha256"],
    }
    AUTO.validate_receipt(current, receipt, response)
    for field, bad in (
        ("model", "different"),
        ("prompt_sha256", digest("different")),
        ("response_sha256", digest("different")),
        ("response_utf8_bytes", 999),
        ("status", "invalid"),
        ("checkpoint_sha256", digest("different checkpoint")),
        ("time_verification", "verified"),
    ):
        tampered = {**receipt, field: bad}
        with pytest.raises(AUTO.AutoLoopError) as caught:
            AUTO.validate_receipt(current, tampered, response)
        assert caught.value.code == "INVALID_PROVENANCE"


def test_model_route_or_call_plan_cannot_change_on_resume():
    before = manifest([pending_call()], state="GENERATING")
    for mutate in (
        lambda value: value["generator"].update(model="different"),
        lambda value: value["judge"].update(route="api"),
        lambda value: value.update(plan_source_sha256=digest("different source")),
        lambda value: value["calls"][0].update(prompt_sha256=digest("different prompt")),
    ):
        after = copy.deepcopy(before)
        mutate(after)
        after = AUTO.seal_manifest(after)
        with pytest.raises(AUTO.AutoLoopError):
            AUTO.validate_resume(before, after)


def test_judge_source_call_id_cannot_be_swapped_when_response_sha_is_identical():
    first = complete_call("A-01", stage="A", response='{"answer":"same"}')
    second = complete_call("B-01", stage="B", response='{"answer":"same"}')
    calls = [first, second]
    for prefix, stage in (("JF", "FINAL_VERDICT"), ("JU", "UNSUPPORTED")):
        for index, source in enumerate((first, second), start=1):
            calls.append(complete_call(
                f"{prefix}{index}", role="judge", stage=stage,
                response='{"valid":true}', source_call_id=source["call_id"],
                source_response_sha256=source["response_sha256"],
            ))
    before = manifest(calls, state="ATTESTING")
    swapped = copy.deepcopy(before)
    for call in swapped["calls"]:
        if call["role"] == "judge":
            call["source_call_id"] = "B-01" if call["source_call_id"] == "A-01" else "A-01"
    after = AUTO.evolve_manifest(before, swapped)
    AUTO.validate_manifest(after)
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_resume(before, after)
    assert caught.value.code == "RESUME_DRIFT"


def test_genesis_plan_is_bound_to_an_external_preregistered_artifact(tmp_path):
    source = b'{"cohort":"frozen-before-run","A":20,"B":20,"C1":20,"C2":20}'
    current = AUTO.new_manifest(
        "run-001", generator(), judge(), [], plan_source_sha256=AUTO.sha256_bytes(source),
    )
    AUTO.validate_plan_source(current, source)
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_plan_source(current, b'{"cohort":"smaller-after-results"}')
    assert caught.value.code == "INVALID_PROVENANCE"

    manifest_path = tmp_path / "manifest.json"
    source_path = tmp_path / "plan.json"
    manifest_path.write_text(json.dumps(current), encoding="utf-8")
    source_path.write_bytes(source)
    assert AUTO.main(["validate-plan-source", str(manifest_path), str(source_path)]) == 0


def test_private_execution_manifest_cannot_claim_a_public_repository():
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.new_manifest(
            "run-001", generator(), judge(), [],
            plan_source_sha256=digest("frozen-plan-source"),
            execution_plane="private_trusted", repository_visibility="public",
        )
    assert caught.value.code == "PRIVATE_RUNNER_REQUIRED"


def test_verdict_cannot_appear_before_all_gates_and_draft_state():
    candidate = manifest(completed_pipeline_calls(), state="ATTESTING")
    candidate["verdict"] = "KEEP"
    candidate = AUTO.seal_manifest(candidate)
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_manifest(candidate)
    assert caught.value.code == "FAIL_CLOSED"


def test_transition_itself_rejects_scoring_with_unfinished_calls():
    judging = manifest([pending_call()], state="GENERATING")
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.transition(judging, "VALIDATING_GENERATION")
    assert caught.value.code == "INCOMPLETE_CALLS"


def test_missing_or_invalid_calls_block_scoring_and_draft():
    for call in (pending_call(), {**complete_call(), "status": "invalid"}):
        candidate = force_late_state_for_negative_test(manifest([call]), "SCORING")
        with pytest.raises(AUTO.AutoLoopError) as caught:
            AUTO.validate_manifest(candidate)
        assert caught.value.code == "INCOMPLETE_CALLS"


def test_omitted_call_cannot_shrink_the_scoring_denominator():
    call = complete_call()
    candidate = AUTO.new_manifest(
        "run-001", generator(), judge(), [call],
        plan_source_sha256=digest("frozen-plan-source"),
        call_plan=[
            {"role": "generator", "stage": "A", "expected": 2},
        ],
    )
    candidate = force_late_state_for_negative_test(candidate, "SCORING")
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.validate_manifest(candidate)
    assert caught.value.code == "INCOMPLETE_CALLS"


def test_resume_can_only_append_calls_within_the_frozen_plan():
    before = AUTO.new_manifest(
        "run-001", generator(), judge(), [complete_call("CASE__C1", stage="C1", response='{"x":1}')],
        plan_source_sha256=digest("frozen-plan-source"),
        call_plan=[
            {"role": "generator", "stage": "C1", "expected": 1},
            {"role": "generator", "stage": "C2", "expected": 1},
        ],
    )
    c1_sha = before["calls"][0]["response_sha256"]
    appended = AUTO.append_calls(before, [pending_call(
        "CASE__C2", stage="C2",
        derivation={"c1_call_id": "CASE__C1", "c1_response_sha256": c1_sha},
    )])
    AUTO.validate_resume(before, appended)
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.append_calls(appended, [pending_call(
            "OTHER__C2", stage="C2",
            derivation={"c1_call_id": "CASE__C1", "c1_response_sha256": c1_sha},
        )])
    assert caught.value.code == "CALL_PLAN_DRIFT"


@pytest.mark.parametrize("bad_judge", [
    judge(provider="anthropic"),
    judge(session_id="claude-session-1"),
])
def test_generator_and_judge_provider_and_session_are_independent(bad_judge):
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.new_manifest(
            "run-001", generator(), bad_judge, [],
            plan_source_sha256=digest("frozen-plan-source"),
        )
    assert caught.value.code == "INVALID_INDEPENDENCE"


@pytest.mark.parametrize("failed_gate,expected_state", [
    ("generation_valid", "INVALID_OUTPUT"),
    ("judgments_valid", "INVALID_OUTPUT"),
    ("provenance_valid", "INVALID_PROVENANCE"),
    ("independence_valid", "INVALID_INDEPENDENCE"),
    ("tests_passed", "FAILED_TESTS"),
    ("billing_safe", "BLOCKED_BILLING_ROUTE"),
])
def test_any_hard_gate_failure_cannot_produce_keep_or_draft(failed_gate, expected_state):
    chain = active_chain(completed_pipeline_calls(), "ATTESTING")
    ready = chain[-1]
    gates = {name: True for name in AUTO.REQUIRED_GATES}
    gates[failed_gate] = False
    stopped = AUTO.finalize_for_draft(
        ready, gates, score_sha256=digest("score"),
        attestation_sha256=digest("attestation"), verdict="KEEP",
    )
    assert stopped["state"] == expected_state
    assert stopped["verdict"] is None
    assert stopped["merge_attempted"] is False and stopped["auto_merge"] is False
    AUTO.validate_resume(ready, stopped)
    AUTO.validate_chain(chain + [stopped])


def test_success_stops_at_draft_pr_ready_and_never_merges():
    ready = manifest(completed_pipeline_calls(), state="ATTESTING")
    final = AUTO.finalize_for_draft(
        ready, {name: True for name in AUTO.REQUIRED_GATES},
        score_sha256=digest("score"), attestation_sha256=digest("attestation"),
        verdict="SUBSCRIPTION_KEEP",
    )
    assert final["state"] == "DRAFT_PR_READY"
    assert final["verdict"] == "SUBSCRIPTION_KEEP"
    assert final["merge_attempted"] is False and final["auto_merge"] is False
    with pytest.raises(AUTO.AutoLoopError) as caught:
        AUTO.transition(final, "CREATED")
    assert caught.value.code == "INVALID_TRANSITION"
    for field, value in (("auto_merge", True), ("merge_attempted", True), ("merge_commit_sha", digest("merge"))):
        tampered = {**final, field: value}
        tampered["checkpoint_sha256"] = AUTO.checkpoint_sha256(tampered)
        with pytest.raises(AUTO.AutoLoopError):
            AUTO.validate_manifest(tampered)


def test_dry_run_uses_no_network_process_or_subscription_secret(monkeypatch, capsys):
    for name in AUTO.DENIED_EXACT_ENV | AUTO.SUBSCRIPTION_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("process used")),
    )
    assert AUTO.main(["dry-run"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["execution_plane"] == "public_control"
    assert payload["repository_visibility"] == "public"
    assert "token" not in output.lower()
    assert "secret" not in output.lower()


def test_preflight_checks_real_execution_environment_and_redacts_values(monkeypatch, capsys):
    for name in list(os.environ):
        if (name in AUTO.DENIED_EXACT_ENV or name in AUTO.SUBSCRIPTION_CREDENTIAL_ENV
                or AUTO.billing_environment_violations({name: os.environ[name]})):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-secret-value")
    monkeypatch.setenv("CODEX_HOME", "/trusted/codex")
    assert AUTO.main([
        "preflight", "--execution-plane", "private_trusted",
        "--repository-visibility", "private",
    ]) == 0
    assert "autoloop ok" in capsys.readouterr().out

    header_secret = "x-api-key: sk-ant-api03-do-not-print"
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", header_secret)
    assert AUTO.main([
        "preflight", "--execution-plane", "private_trusted",
        "--repository-visibility", "private",
    ]) == 1
    error = capsys.readouterr().err
    assert "ANTHROPIC_CUSTOM_HEADERS" in error
    assert header_secret not in error


def test_schema_files_are_valid_json_and_match_code_version():
    schema_dir = ROOT / "experiments" / "counterevidence" / "autoloop_schemas"
    manifest_schema = json.loads((schema_dir / "manifest.schema.json").read_text(encoding="utf-8"))
    receipt_schema = json.loads((schema_dir / "receipt.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(manifest_schema)
    Draft202012Validator.check_schema(receipt_schema)
    assert manifest_schema["properties"]["schema_version"]["const"] == AUTO.SCHEMA_VERSION
    assert receipt_schema["properties"]["schema_version"]["const"] == AUTO.RECEIPT_SCHEMA_VERSION
    validator = Draft202012Validator(manifest_schema)
    validator.validate(AUTO.dry_run_manifest())
    validator.validate(manifest(completed_pipeline_calls(), state="ATTESTING"))

    bad_c2 = AUTO.dry_run_manifest()
    bad_c2["call_plan"] = [{"role": "generator", "stage": "C2", "expected": 1}]
    bad_c2["calls"] = [pending_call("CASE__C2", stage="C2")]
    bad_judge = AUTO.dry_run_manifest()
    bad_judge["call_plan"] = [{"role": "judge", "stage": "FINAL_VERDICT", "expected": 1}]
    bad_judge["calls"] = [AUTO.new_call("J001", "judge", "FINAL_VERDICT", digest("judge prompt"))]
    premature = AUTO.dry_run_manifest()
    premature["verdict"] = "KEEP"
    zero_byte_complete = AUTO.dry_run_manifest()
    zero_byte_complete["call_plan"] = [{"role": "generator", "stage": "A", "expected": 1}]
    zero_byte_complete["calls"] = [
        {**pending_call(), "status": "complete", "attempts": 1,
         "response_sha256": digest(""), "response_utf8_bytes": 0}
    ]
    wrong_route = AUTO.dry_run_manifest()
    wrong_route["generator"]["route"] = "chatgpt_managed"
    for invalid in (bad_c2, bad_judge, premature, zero_byte_complete, wrong_route):
        assert list(validator.iter_errors(invalid)), invalid


def test_receipt_schema_enforces_checkpoint_and_unverified_clock_fields():
    schema = json.loads((
        ROOT / "experiments" / "counterevidence" / "autoloop_schemas" / "receipt.schema.json"
    ).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    current = manifest([pending_call()], state="GENERATING")
    response = '{"answer":"x"}'
    receipt = {
        "schema_version": AUTO.RECEIPT_SCHEMA_VERSION,
        "run_id": current["run_id"], "call_id": "A-01", **current["generator"],
        "prompt_sha256": current["calls"][0]["prompt_sha256"],
        "response_sha256": digest(response), "response_utf8_bytes": len(response),
        "status": "complete", "collected_at_utc": "2026-08-16T00:00:00Z",
        "time_verification": "unverifiable_runner_clock",
        "checkpoint_sha256": current["checkpoint_sha256"],
    }
    validator.validate(receipt)
    for field in ("time_verification", "checkpoint_sha256"):
        bad = dict(receipt)
        bad.pop(field)
        assert list(validator.iter_errors(bad))
