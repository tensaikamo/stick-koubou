#!/usr/bin/env python3
"""AutoLoop の公開コントローラ。

このモジュールはモデルや課金APIを呼ばない。公開リポジトリで扱えるのは、
実行計画・状態遷移・SHA-256証跡・fail-closed gateだけである。Claude / Codexの
subscription認証は、別の trusted private runner が保持し、ここへは秘密値でなく
署名対象のreceiptだけを返す。
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
from collections import Counter
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


SCHEMA_VERSION = "counterevidence.autoloop.v1"
RECEIPT_SCHEMA_VERSION = "counterevidence.autoloop.receipt.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

ACTIVE_STATES = (
    "CREATED",
    "GENERATING",
    "VALIDATING_GENERATION",
    "JUDGING",
    "VALIDATING_JUDGMENTS",
    "SCORING",
    "ATTESTING",
    "DRAFT_PR_READY",
)
STOP_STATES = (
    "PAUSED_QUOTA",
    "BLOCKED_AUTH",
    "BLOCKED_BILLING_ROUTE",
    "INVALID_OUTPUT",
    "INVALID_PROVENANCE",
    "INVALID_INDEPENDENCE",
    "FAILED_TESTS",
)
ALL_STATES = frozenset(ACTIVE_STATES + STOP_STATES)
MIN_SEQUENCE_BY_STATE = {state: index for index, state in enumerate(ACTIVE_STATES)}
MIN_SEQUENCE_BY_STATE.update({state: 1 for state in STOP_STATES})
MIN_SEQUENCE_BY_STATE["PAUSED_QUOTA"] = 2
MANIFEST_FIELDS = frozenset({
    "schema_version", "run_id", "execution_plane", "repository_visibility",
    "state", "paused_from", "generator", "judge", "call_plan", "calls",
    "score_sha256", "attestation_sha256", "verdict", "merge_attempted",
    "auto_merge", "sequence", "previous_checkpoint_sha256", "checkpoint_sha256",
})
EXECUTOR_FIELDS = frozenset({"provider", "model", "route", "session_id"})
CALL_FIELDS = frozenset({
    "call_id", "role", "stage", "prompt_sha256", "response_sha256",
    "response_utf8_bytes", "status", "attempts", "derivation",
    "source_response_sha256",
})

ALLOWED_TRANSITIONS = {
    "CREATED": {"GENERATING"},
    "GENERATING": {"VALIDATING_GENERATION", "PAUSED_QUOTA", "INVALID_OUTPUT"},
    "VALIDATING_GENERATION": {
        "JUDGING", "INVALID_OUTPUT", "INVALID_PROVENANCE", "INVALID_INDEPENDENCE"
    },
    "JUDGING": {"VALIDATING_JUDGMENTS", "PAUSED_QUOTA", "INVALID_OUTPUT"},
    "VALIDATING_JUDGMENTS": {
        "SCORING", "INVALID_OUTPUT", "INVALID_PROVENANCE", "INVALID_INDEPENDENCE"
    },
    "SCORING": {"ATTESTING", "INVALID_OUTPUT", "INVALID_PROVENANCE"},
    "ATTESTING": {"DRAFT_PR_READY", "INVALID_PROVENANCE", "FAILED_TESTS"},
    "DRAFT_PR_READY": set(),
}

DENIED_EXACT_ENV = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "XAI_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
})
DENIED_ENV_PREFIXES = (
    "XAI_", "OPENROUTER_", "AWS_BEDROCK_", "BEDROCK_", "VERTEX_",
    "FOUNDRY_", "AZURE_OPENAI_",
)
DENIED_BILLING_MARKERS = ("EXTRA_USAGE", "PAYG", "AUTO_TOP_UP", "AUTO_RELOAD")
ALLOWED_STANDARD_BASE_URL_HOSTS = {
    "ANTHROPIC_BASE_URL": "api.anthropic.com",
    "OPENAI_BASE_URL": "api.openai.com",
}
SUBSCRIPTION_CREDENTIAL_ENV = frozenset({
    "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_HOME", "CODEX_AUTH_JSON"
})

SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|secret|password|access_?tokens?|auth_?tokens?|oauth_?tokens?|tokens?|credentials?)(?:$|_)",
    re.IGNORECASE,
)
SECRET_VALUE_RES = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}"),
)


class AutoLoopError(ValueError):
    """利用者に安全に表示できるコード付きfail-closedエラー。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def assert_secret_free(value: Any, path: str = "$") -> None:
    """manifest/receiptへの資格情報混入を、保存前に拒否する。"""
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                raise AutoLoopError("SECRET_MATERIAL", f"秘密値を格納できないフィールド: {path}.{key_text}")
            assert_secret_free(child, f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            assert_secret_free(child, f"{path}[{index}]")
    elif isinstance(value, str):
        for pattern in SECRET_VALUE_RES:
            if pattern.search(value):
                raise AutoLoopError("SECRET_MATERIAL", f"秘密値らしき文字列を検出: {path}")


def billing_environment_violations(env: Mapping[str, str] | None = None) -> list[str]:
    """追加課金へ接続しうる環境変数名だけを返す。値は返さない。"""
    env = os.environ if env is None else env
    found = []
    for raw_name, raw_value in env.items():
        if not raw_value:
            continue
        name = raw_name.upper()
        standard_host = ALLOWED_STANDARD_BASE_URL_HOSTS.get(name)
        if standard_host is not None:
            try:
                candidate = raw_value if "://" in raw_value else "//" + raw_value
                host = (urlparse(candidate).hostname or "").lower()
            except (TypeError, ValueError):
                host = ""
            if host != standard_host:
                found.append(raw_name)
            continue
        if (name in DENIED_EXACT_ENV
                or name.startswith(DENIED_ENV_PREFIXES)
                or any(marker in name for marker in DENIED_BILLING_MARKERS)):
            found.append(raw_name)
    return sorted(set(found))


def require_safe_billing_environment(env: Mapping[str, str] | None = None) -> None:
    violations = billing_environment_violations(env)
    if violations:
        raise AutoLoopError(
            "BLOCKED_BILLING_ROUTE",
            "追加課金へ接続しうる環境変数を検出: " + ", ".join(violations),
        )


def validate_execution_context(
    execution_plane: str,
    repository_visibility: str,
    env: Mapping[str, str] | None = None,
) -> None:
    """AI資格情報はtrusted private runnerにしか存在できない。"""
    if execution_plane not in {"public_control", "private_trusted"}:
        raise AutoLoopError("INVALID_EXECUTION_PLANE", f"未知のexecution plane: {execution_plane!r}")
    if repository_visibility not in {"public", "private"}:
        raise AutoLoopError("INVALID_VISIBILITY", f"未知のrepository visibility: {repository_visibility!r}")
    require_safe_billing_environment(env)
    env = os.environ if env is None else env
    present = sorted(name for name in SUBSCRIPTION_CREDENTIAL_ENV if env.get(name))
    if execution_plane == "public_control" and present:
        raise AutoLoopError(
            "PUBLIC_AUTH_REFUSED",
            "公開control planeにsubscription資格情報を置けません: " + ", ".join(present),
        )
    if execution_plane == "private_trusted" and repository_visibility != "private":
        raise AutoLoopError(
            "PRIVATE_RUNNER_REQUIRED",
            "資格情報を扱うexecution planeはprivate repositoryでなければなりません",
        )
    if execution_plane == "private_trusted":
        has_claude = bool(env.get("CLAUDE_CODE_OAUTH_TOKEN"))
        has_codex = bool(env.get("CODEX_HOME") or env.get("CODEX_AUTH_JSON"))
        if not has_claude or not has_codex:
            missing = []
            if not has_claude:
                missing.append("Claude subscription OAuth")
            if not has_codex:
                missing.append("ChatGPT-managed Codex auth")
            raise AutoLoopError("BLOCKED_AUTH", "private runnerのsubscription認証が不足: " + ", ".join(missing))


def _checkpoint_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(manifest))
    payload.pop("checkpoint_sha256", None)
    return payload


def checkpoint_sha256(manifest: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(_checkpoint_payload(manifest)))


def seal_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(manifest))
    sealed.pop("checkpoint_sha256", None)
    assert_secret_free(sealed)
    sealed["checkpoint_sha256"] = checkpoint_sha256(sealed)
    return sealed


def evolve_manifest(previous: Mapping[str, Any], updated: Mapping[str, Any]) -> dict[str, Any]:
    """一つ前のcheckpointへ束縛した次のappend-only checkpointを作る。"""
    validate_manifest(previous)
    evolved = copy.deepcopy(dict(updated))
    evolved["sequence"] = previous["sequence"] + 1
    evolved["previous_checkpoint_sha256"] = previous["checkpoint_sha256"]
    result = seal_manifest(evolved)
    validate_manifest(result)
    return result


def new_call(
    call_id: str,
    role: str,
    stage: str,
    prompt_sha256: str,
    *,
    derivation: Mapping[str, str] | None = None,
    source_response_sha256: str | None = None,
) -> dict[str, Any]:
    call = {
        "call_id": call_id,
        "role": role,
        "stage": stage,
        "prompt_sha256": prompt_sha256,
        "response_sha256": None,
        "response_utf8_bytes": None,
        "status": "pending",
        "attempts": 0,
    }
    if derivation is not None:
        call["derivation"] = dict(derivation)
    if source_response_sha256 is not None:
        call["source_response_sha256"] = source_response_sha256
    return call


def new_manifest(
    run_id: str,
    generator: Mapping[str, str],
    judge: Mapping[str, str],
    calls: Sequence[Mapping[str, Any]],
    *,
    call_plan: Sequence[Mapping[str, Any]] | None = None,
    execution_plane: str = "public_control",
    repository_visibility: str = "public",
) -> dict[str, Any]:
    copied_calls = [copy.deepcopy(dict(call)) for call in calls]
    if call_plan is None:
        counts = Counter((call.get("role"), call.get("stage")) for call in copied_calls)
        call_plan = [
            {"role": role, "stage": stage, "expected": expected}
            for (role, stage), expected in sorted(counts.items())
        ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "execution_plane": execution_plane,
        "repository_visibility": repository_visibility,
        "state": "CREATED",
        "paused_from": None,
        "generator": dict(generator),
        "judge": dict(judge),
        "call_plan": [copy.deepcopy(dict(item)) for item in call_plan],
        "calls": copied_calls,
        "score_sha256": None,
        "attestation_sha256": None,
        "verdict": None,
        "merge_attempted": False,
        "auto_merge": False,
        "sequence": 0,
        "previous_checkpoint_sha256": None,
    }
    sealed = seal_manifest(manifest)
    validate_manifest(sealed)
    return sealed


def _validate_executor(name: str, executor: Any, expected_route: str) -> None:
    if not isinstance(executor, Mapping):
        raise AutoLoopError("INVALID_MANIFEST", f"{name}がobjectではありません")
    if set(executor) != EXECUTOR_FIELDS:
        raise AutoLoopError("INVALID_MANIFEST", f"{name}のフィールドが不正")
    for field in ("provider", "model", "route", "session_id"):
        if not _nonempty_string(executor.get(field)):
            raise AutoLoopError("INVALID_MANIFEST", f"{name}.{field}がありません")
    if executor.get("route") != expected_route:
        raise AutoLoopError(
            "BLOCKED_BILLING_ROUTE",
            f"{name}.routeは{expected_route}だけ許可: {executor.get('route')!r}",
        )


def _validate_call(call: Any, call_ids: set[str]) -> None:
    if not isinstance(call, Mapping):
        raise AutoLoopError("INVALID_MANIFEST", "callがobjectではありません")
    if not set(call) <= CALL_FIELDS:
        raise AutoLoopError("INVALID_MANIFEST", "callに未知のフィールドがあります")
    call_id = call.get("call_id")
    if not _nonempty_string(call_id) or call_id in call_ids:
        raise AutoLoopError("INVALID_MANIFEST", f"call_idが空または重複: {call_id!r}")
    call_ids.add(call_id)
    if call.get("role") not in {"generator", "judge"}:
        raise AutoLoopError("INVALID_MANIFEST", f"{call_id}: roleが不正")
    if not _nonempty_string(call.get("stage")) or not _is_sha256(call.get("prompt_sha256")):
        raise AutoLoopError("INVALID_MANIFEST", f"{call_id}: stageまたはprompt SHAが不正")
    if call.get("status") not in {"pending", "complete", "invalid"}:
        raise AutoLoopError("INVALID_MANIFEST", f"{call_id}: statusが不正")
    attempts = call.get("attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts not in {0, 1}:
        raise AutoLoopError("INVALID_MANIFEST", f"{call_id}: attemptsは0または1のみ")
    if call.get("status") == "pending":
        if attempts != 0 or call.get("response_sha256") is not None or call.get("response_utf8_bytes") is not None:
            raise AutoLoopError("INVALID_MANIFEST", f"{call_id}: pending callに応答証跡があります")
    else:
        if attempts != 1 or not _is_sha256(call.get("response_sha256")):
            raise AutoLoopError("INVALID_MANIFEST", f"{call_id}: 完了/不正callの応答SHAが不正")
        size = call.get("response_utf8_bytes")
        minimum = 0 if call.get("status") == "invalid" else 1
        if not isinstance(size, int) or isinstance(size, bool) or size < minimum:
            raise AutoLoopError("INVALID_MANIFEST", f"{call_id}: response_utf8_bytesが不正")
    if call.get("stage") == "C2":
        derivation = call.get("derivation")
        if not isinstance(derivation, Mapping):
            raise AutoLoopError("INVALID_PROVENANCE", f"{call_id}: C2 derivationがありません")
        if not _nonempty_string(derivation.get("c1_call_id")) or not _is_sha256(derivation.get("c1_response_sha256")):
            raise AutoLoopError("INVALID_PROVENANCE", f"{call_id}: C1束縛が不正")
    if call.get("role") == "judge" and not _is_sha256(call.get("source_response_sha256")):
        raise AutoLoopError("INVALID_PROVENANCE", f"{call_id}: judge対象応答SHAがありません")


def _validate_call_plan(plan: Any, calls: Sequence[Mapping[str, Any]]) -> Counter:
    if not isinstance(plan, list):
        raise AutoLoopError("INVALID_MANIFEST", "call_planが配列ではありません")
    expected = Counter()
    for item in plan:
        if not isinstance(item, Mapping):
            raise AutoLoopError("INVALID_MANIFEST", "call_plan itemがobjectではありません")
        if set(item) != {"role", "stage", "expected"}:
            raise AutoLoopError("INVALID_MANIFEST", "call_plan itemのフィールドが不正")
        role, stage, count = item.get("role"), item.get("stage"), item.get("expected")
        if role not in {"generator", "judge"} or not _nonempty_string(stage):
            raise AutoLoopError("INVALID_MANIFEST", "call_planのrole/stageが不正")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise AutoLoopError("INVALID_MANIFEST", "call_plan.expectedは正整数が必要")
        key = (role, stage)
        if key in expected:
            raise AutoLoopError("INVALID_MANIFEST", f"call_planが重複: {role}/{stage}")
        expected[key] = count
    actual = Counter((call["role"], call["stage"]) for call in calls)
    unexpected = [key for key in actual if key not in expected]
    excessive = [key for key, count in actual.items() if count > expected.get(key, 0)]
    if unexpected or excessive:
        raise AutoLoopError("CALL_PLAN_DRIFT", f"call_plan外または過剰なcall: {unexpected + excessive}")
    return expected


def _require_role_complete(
    role: str,
    expected: Counter,
    calls: Sequence[Mapping[str, Any]],
) -> None:
    planned = sum(count for (planned_role, _), count in expected.items() if planned_role == role)
    role_calls = [call for call in calls if call["role"] == role]
    if planned <= 0 or len(role_calls) != planned or any(call["status"] != "complete" for call in role_calls):
        raise AutoLoopError("INCOMPLETE_CALLS", f"{role} callが計画数どおり完了していません")


def validate_manifest(manifest: Mapping[str, Any], *, verify_checkpoint: bool = True) -> None:
    if not isinstance(manifest, Mapping):
        raise AutoLoopError("INVALID_MANIFEST", "manifestがobjectではありません")
    assert_secret_free(manifest)
    if set(manifest) != MANIFEST_FIELDS:
        raise AutoLoopError("INVALID_MANIFEST", "manifestのフィールドが不正")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise AutoLoopError("INVALID_MANIFEST", "未知のschema_version")
    if not isinstance(manifest.get("run_id"), str) or not RUN_ID_RE.fullmatch(manifest["run_id"]):
        raise AutoLoopError("INVALID_MANIFEST", "run_idが不正")
    if manifest.get("state") not in ALL_STATES:
        raise AutoLoopError("INVALID_MANIFEST", "stateが不正")
    execution_plane = manifest.get("execution_plane")
    visibility = manifest.get("repository_visibility")
    if execution_plane not in {"public_control", "private_trusted"}:
        raise AutoLoopError("INVALID_EXECUTION_PLANE", "execution_planeが不正")
    if visibility not in {"public", "private"}:
        raise AutoLoopError("INVALID_VISIBILITY", "repository_visibilityが不正")
    if execution_plane == "private_trusted" and visibility != "private":
        raise AutoLoopError("PRIVATE_RUNNER_REQUIRED", "private_trusted planeを公開repositoryで実行できません")
    if manifest.get("merge_attempted") is not False or manifest.get("auto_merge") is not False:
        raise AutoLoopError("AUTO_MERGE_REFUSED", "AutoLoopはmergeを実行できません")
    sequence = manifest.get("sequence")
    previous_checkpoint = manifest.get("previous_checkpoint_sha256")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise AutoLoopError("INVALID_PROVENANCE", "checkpoint sequenceが不正")
    if sequence < MIN_SEQUENCE_BY_STATE[manifest["state"]]:
        raise AutoLoopError("INVALID_PROVENANCE", "stateに必要なcheckpoint履歴がありません")
    if ((sequence == 0 and previous_checkpoint is not None)
            or (sequence > 0 and not _is_sha256(previous_checkpoint))):
        raise AutoLoopError("INVALID_PROVENANCE", "previous checkpoint束縛が不正")
    _validate_executor("generator", manifest.get("generator"), "subscription_oauth")
    _validate_executor("judge", manifest.get("judge"), "chatgpt_managed")
    generator, judge = manifest["generator"], manifest["judge"]
    if generator["provider"] == judge["provider"] or generator["session_id"] == judge["session_id"]:
        raise AutoLoopError("INVALID_INDEPENDENCE", "generatorとjudgeはprovider/sessionを分離してください")
    calls = manifest.get("calls")
    if not isinstance(calls, list):
        raise AutoLoopError("INVALID_MANIFEST", "callsが配列ではありません")
    call_ids: set[str] = set()
    for call in calls:
        _validate_call(call, call_ids)
    expected = _validate_call_plan(manifest.get("call_plan"), calls)
    by_id = {call["call_id"]: call for call in calls}
    for call in calls:
        if call.get("stage") == "C2":
            derivation = call["derivation"]
            c1 = by_id.get(derivation["c1_call_id"])
            if (not c1 or c1.get("stage") != "C1" or c1.get("status") != "complete"
                    or c1.get("response_sha256") != derivation["c1_response_sha256"]):
                raise AutoLoopError("INVALID_PROVENANCE", f"{call['call_id']}: C1/C2束縛が一致しません")
    if manifest.get("state") in {
        "VALIDATING_GENERATION", "JUDGING", "VALIDATING_JUDGMENTS",
        "SCORING", "ATTESTING", "DRAFT_PR_READY",
    }:
        _require_role_complete("generator", expected, calls)
    if manifest.get("state") in {"VALIDATING_JUDGMENTS", "SCORING", "ATTESTING", "DRAFT_PR_READY"}:
        _require_role_complete("judge", expected, calls)
    if manifest.get("state") == "DRAFT_PR_READY" and not _is_sha256(manifest.get("attestation_sha256")):
        raise AutoLoopError("INVALID_PROVENANCE", "Draft PRにはattestation SHAが必要です")
    for field in ("score_sha256", "attestation_sha256"):
        if manifest.get(field) is not None and not _is_sha256(manifest.get(field)):
            raise AutoLoopError("INVALID_PROVENANCE", f"{field}が不正")
    if manifest.get("state") == "DRAFT_PR_READY" and not _is_sha256(manifest.get("score_sha256")):
        raise AutoLoopError("INVALID_PROVENANCE", "Draft PRにはscore SHAが必要です")
    if manifest.get("state") != "DRAFT_PR_READY" and manifest.get("verdict") is not None:
        raise AutoLoopError("FAIL_CLOSED", "Draft PR準備前にverdictを確定できません")
    if manifest.get("state") in STOP_STATES and manifest.get("verdict") is not None:
        raise AutoLoopError("FAIL_CLOSED", "停止状態ではverdictを確定できません")
    if manifest.get("state") == "PAUSED_QUOTA":
        if manifest.get("paused_from") not in {"GENERATING", "JUDGING"}:
            raise AutoLoopError("INVALID_PROVENANCE", "quota停止元が不正")
    elif manifest.get("paused_from") is not None:
        raise AutoLoopError("INVALID_PROVENANCE", "quota停止中以外にpaused_fromを保持できません")
    if verify_checkpoint:
        if not _is_sha256(manifest.get("checkpoint_sha256")):
            raise AutoLoopError("INVALID_PROVENANCE", "checkpoint SHAがありません")
        if manifest["checkpoint_sha256"] != checkpoint_sha256(manifest):
            raise AutoLoopError("INVALID_PROVENANCE", "checkpoint SHAが一致しません")


RECEIPT_FIELDS = frozenset({
    "schema_version", "run_id", "call_id", "provider", "model", "route",
    "session_id", "prompt_sha256", "response_sha256", "response_utf8_bytes",
    "status", "collected_at_utc",
})


def validate_receipt(
    manifest: Mapping[str, Any],
    receipt: Mapping[str, Any],
    response_text: str,
    *,
    require_json: bool = True,
    required_fields: Sequence[str] = (),
) -> None:
    """private runnerのreceiptを応答実体・固定executor・callへ束縛する。"""
    validate_manifest(manifest)
    if not isinstance(receipt, Mapping) or set(receipt) != RECEIPT_FIELDS:
        raise AutoLoopError("INVALID_PROVENANCE", "receiptのフィールドが不正")
    assert_secret_free(receipt)
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION or receipt.get("run_id") != manifest["run_id"]:
        raise AutoLoopError("INVALID_PROVENANCE", "receiptのschema/run_idが一致しません")
    call = next((item for item in manifest["calls"] if item["call_id"] == receipt.get("call_id")), None)
    if call is None or call["status"] != "pending":
        raise AutoLoopError("INVALID_PROVENANCE", "receipt対象callが不在または記録済みです")
    executor = manifest["generator" if call["role"] == "generator" else "judge"]
    for field in ("provider", "model", "route", "session_id"):
        if receipt.get(field) != executor[field]:
            raise AutoLoopError("INVALID_PROVENANCE", f"receiptの{field}が固定executorと不一致")
    if receipt.get("prompt_sha256") != call["prompt_sha256"]:
        raise AutoLoopError("INVALID_PROVENANCE", "receiptのprompt SHAが不一致")
    if not isinstance(response_text, str):
        raise AutoLoopError("INVALID_OUTPUT", "response実体が文字列ではありません")
    if (receipt.get("response_sha256") != sha256_text(response_text)
            or receipt.get("response_utf8_bytes") != len(response_text.encode("utf-8"))):
        raise AutoLoopError("INVALID_PROVENANCE", "receiptとresponse実体のSHA/byteが不一致")
    valid = bool(response_text)
    if require_json:
        try:
            parsed = json.loads(response_text)
            valid = valid and isinstance(parsed, Mapping) and all(field in parsed for field in required_fields)
        except json.JSONDecodeError:
            valid = False
    expected_status = "complete" if valid else "invalid"
    collected_at = receipt.get("collected_at_utc")
    try:
        parsed_time = dt.datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        time_valid = collected_at.endswith("Z") and parsed_time.tzinfo is not None
    except (AttributeError, TypeError, ValueError):
        time_valid = False
    if receipt.get("status") != expected_status or not time_valid:
        raise AutoLoopError("INVALID_PROVENANCE", "receiptのstatus/collected_at_utcが実体と不一致")


def transition(manifest: Mapping[str, Any], next_state: str) -> dict[str, Any]:
    validate_manifest(manifest)
    current = manifest["state"]
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if next_state not in allowed:
        raise AutoLoopError("INVALID_TRANSITION", f"{current} -> {next_state} は許可されません")
    updated = copy.deepcopy(dict(manifest))
    updated["state"] = next_state
    return evolve_manifest(manifest, updated)


def pause_for_quota(manifest: Mapping[str, Any]) -> dict[str, Any]:
    validate_manifest(manifest)
    if manifest["state"] not in {"GENERATING", "JUDGING"}:
        raise AutoLoopError("INVALID_TRANSITION", "quota停止できるのは生成/判定中だけです")
    updated = copy.deepcopy(dict(manifest))
    updated["paused_from"] = manifest["state"]
    updated["state"] = "PAUSED_QUOTA"
    updated["verdict"] = None
    return evolve_manifest(manifest, updated)


def resume_after_quota(
    manifest: Mapping[str, Any],
    *,
    execution_plane: str,
    repository_visibility: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    validate_manifest(manifest)
    if manifest["state"] != "PAUSED_QUOTA" or manifest.get("paused_from") not in {"GENERATING", "JUDGING"}:
        raise AutoLoopError("INVALID_TRANSITION", "再開可能なquota checkpointではありません")
    validate_execution_context(execution_plane, repository_visibility, env)
    if execution_plane != manifest["execution_plane"] or repository_visibility != manifest["repository_visibility"]:
        raise AutoLoopError("INVALID_PROVENANCE", "再開時にexecution contextを変更できません")
    updated = copy.deepcopy(dict(manifest))
    updated["state"] = manifest["paused_from"]
    updated["paused_from"] = None
    return evolve_manifest(manifest, updated)


def append_calls(manifest: Mapping[str, Any], calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """凍結planの範囲内で、未materialize callだけを追加する。"""
    validate_manifest(manifest)
    if manifest["state"] not in {"CREATED", "GENERATING", "JUDGING"}:
        raise AutoLoopError("INVALID_TRANSITION", "このstateではcallを追加できません")
    updated = copy.deepcopy(dict(manifest))
    existing = {call["call_id"] for call in updated["calls"]}
    for call in calls:
        call_id = call.get("call_id") if isinstance(call, Mapping) else None
        if call_id in existing:
            raise AutoLoopError("IMMUTABLE_CALL", f"callを上書きできません: {call_id}")
        updated["calls"].append(copy.deepcopy(dict(call)))
        existing.add(call_id)
    return evolve_manifest(manifest, updated)


def record_response(
    manifest: Mapping[str, Any],
    call_id: str,
    response_text: str,
    *,
    require_json: bool = True,
    required_fields: Sequence[str] = (),
) -> dict[str, Any]:
    """応答を1回だけ記録する。不正JSONもSHAを残して再試行しない。"""
    validate_manifest(manifest)
    if not isinstance(response_text, str):
        raise AutoLoopError("INVALID_OUTPUT", "応答本文は文字列でなければなりません")
    updated = copy.deepcopy(dict(manifest))
    call = next((item for item in updated["calls"] if item["call_id"] == call_id), None)
    if call is None:
        raise AutoLoopError("UNKNOWN_CALL", f"未知のcall: {call_id}")
    if call["status"] != "pending" or call["attempts"] != 0:
        raise AutoLoopError("IMMUTABLE_CALL", f"callは再実行できません: {call_id}")
    valid = bool(response_text)
    if require_json:
        try:
            parsed = json.loads(response_text)
            valid = valid and isinstance(parsed, Mapping) and all(field in parsed for field in required_fields)
        except json.JSONDecodeError:
            valid = False
    call["response_sha256"] = sha256_text(response_text)
    call["response_utf8_bytes"] = len(response_text.encode("utf-8"))
    call["attempts"] = 1
    call["status"] = "complete" if valid else "invalid"
    if not valid:
        updated["state"] = "INVALID_OUTPUT"
        updated["verdict"] = None
    return evolve_manifest(manifest, updated)


def validate_resume(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    """checkpoint再開でモデル・route・prompt・既存応答を書き換えさせない。"""
    validate_manifest(before)
    validate_manifest(after)
    if (after["sequence"] != before["sequence"] + 1
            or after["previous_checkpoint_sha256"] != before["checkpoint_sha256"]):
        raise AutoLoopError("RESUME_DRIFT", "checkpoint chainが連続していません")
    state_ok = (
        after["state"] == before["state"]
        or after["state"] in ALLOWED_TRANSITIONS.get(before["state"], set())
        or (before["state"] == "PAUSED_QUOTA" and after["state"] == before.get("paused_from"))
    )
    if not state_ok:
        raise AutoLoopError("RESUME_DRIFT", f"stateを飛び越えています: {before['state']} -> {after['state']}")
    for field in ("run_id", "execution_plane", "repository_visibility", "generator", "judge", "call_plan"):
        if before.get(field) != after.get(field):
            raise AutoLoopError("RESUME_DRIFT", f"再開時に{field}を変更できません")
    before_calls = {call["call_id"]: call for call in before["calls"]}
    after_calls = {call["call_id"]: call for call in after["calls"]}
    if not before_calls.keys() <= after_calls.keys():
        raise AutoLoopError("RESUME_DRIFT", "再開時に既存callを削除できません")
    immutable = ("call_id", "role", "stage", "prompt_sha256", "derivation", "source_response_sha256")
    for call_id, old in before_calls.items():
        new = after_calls[call_id]
        if any(old.get(field) != new.get(field) for field in immutable):
            raise AutoLoopError("RESUME_DRIFT", f"{call_id}: call定義が変わりました")
        if old["status"] in {"complete", "invalid"} and old != new:
            raise AutoLoopError("IMMUTABLE_CALL", f"{call_id}: 記録済み応答が変わりました")
        if old["status"] == "pending" and new["attempts"] < old["attempts"]:
            raise AutoLoopError("RESUME_DRIFT", f"{call_id}: attemptsが巻き戻りました")


def validate_chain(checkpoints: Sequence[Mapping[str, Any]]) -> None:
    """genesisから終端まで、飛び越しのないcheckpoint chainを検証する。"""
    if not checkpoints:
        raise AutoLoopError("INVALID_PROVENANCE", "checkpoint chainが空です")
    first = checkpoints[0]
    validate_manifest(first)
    if first["sequence"] != 0 or first["state"] != "CREATED" or first["previous_checkpoint_sha256"] is not None:
        raise AutoLoopError("INVALID_PROVENANCE", "checkpoint chainのgenesisが不正")
    for before, after in zip(checkpoints, checkpoints[1:]):
        validate_resume(before, after)


REQUIRED_GATES = (
    "generation_valid",
    "judgments_valid",
    "provenance_valid",
    "independence_valid",
    "tests_passed",
    "billing_safe",
)


def finalize_for_draft(
    manifest: Mapping[str, Any],
    gates: Mapping[str, bool],
    *,
    score_sha256: str,
    attestation_sha256: str,
    verdict: str | None,
) -> dict[str, Any]:
    """既存scoreの結果を封印し、Draft PR準備で止める。mergeはしない。"""
    validate_manifest(manifest)
    if manifest["state"] != "ATTESTING":
        raise AutoLoopError("INVALID_TRANSITION", "Draft PR準備はATTESTINGからだけ可能です")
    missing = [name for name in REQUIRED_GATES if gates.get(name) is not True]
    if missing:
        state = "FAILED_TESTS" if "tests_passed" in missing else (
            "INVALID_INDEPENDENCE" if "independence_valid" in missing else (
                "INVALID_PROVENANCE" if "provenance_valid" in missing else "INVALID_OUTPUT"
            )
        )
        if "billing_safe" in missing:
            state = "BLOCKED_BILLING_ROUTE"
        updated = copy.deepcopy(dict(manifest))
        updated["state"] = state
        updated["verdict"] = None
        return evolve_manifest(manifest, updated)
    if not _is_sha256(score_sha256) or not _is_sha256(attestation_sha256):
        raise AutoLoopError("INVALID_PROVENANCE", "score/attestation SHAが不正")
    if not manifest["calls"] or any(call["status"] != "complete" for call in manifest["calls"]):
        raise AutoLoopError("INCOMPLETE_CALLS", "全call完了前にDraft PRを作れません")
    updated = copy.deepcopy(dict(manifest))
    updated.update({
        "state": "DRAFT_PR_READY",
        "score_sha256": score_sha256,
        "attestation_sha256": attestation_sha256,
        "verdict": verdict,
        "merge_attempted": False,
        "auto_merge": False,
    })
    return evolve_manifest(manifest, updated)


def dry_run_manifest() -> dict[str, Any]:
    """ネットワークも資格情報も使わない公開control planeの例。"""
    return new_manifest(
        "dry-run",
        {"provider": "anthropic", "model": "configured-on-private-runner",
         "route": "subscription_oauth", "session_id": "generator-session"},
        {"provider": "openai", "model": "configured-on-private-runner",
         "route": "chatgpt_managed", "session_id": "judge-session"},
        [],
    )


def load_json(path: str | pathlib.Path) -> Any:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Counterevidence AutoLoop public controller")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("dry-run", help="資格情報・ネットワークなしでcontract例を出力")
    validate = sub.add_parser("validate", help="manifestとcheckpoint SHAを検証")
    validate.add_argument("manifest")
    resume = sub.add_parser("validate-resume", help="再開前後の不変性を検証")
    resume.add_argument("before")
    resume.add_argument("after")
    receipt = sub.add_parser("validate-receipt", help="receiptをmanifestとresponse実体へ束縛")
    receipt.add_argument("manifest")
    receipt.add_argument("receipt")
    receipt.add_argument("response")
    chain = sub.add_parser("validate-chain", help="genesisからのcheckpoint chainを検証")
    chain.add_argument("checkpoints", nargs="+")
    args = parser.parse_args(argv)
    command = args.command or "dry-run"
    try:
        if command == "dry-run":
            # dry-runは意図的に現在のhost資格情報を参照しない。ここで出すのは
            # 公開control plane用の無通信templateだけで、実行資格の確認ではない。
            validate_execution_context("public_control", "public", {})
            print(json.dumps(dry_run_manifest(), ensure_ascii=False, indent=2))
        elif command == "validate":
            validate_manifest(load_json(args.manifest))
            print("[autoloop ok] manifest/checkpoint")
        elif command == "validate-resume":
            validate_resume(load_json(args.before), load_json(args.after))
            print("[autoloop ok] resume is append-only")
        elif command == "validate-receipt":
            validate_receipt(
                load_json(args.manifest), load_json(args.receipt),
                pathlib.Path(args.response).read_text(encoding="utf-8"),
            )
            print("[autoloop ok] receipt is bound to manifest/response")
        else:
            validate_chain([load_json(path) for path in args.checkpoints])
            print("[autoloop ok] checkpoint chain is continuous")
    except (AutoLoopError, OSError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, AutoLoopError) else "INVALID_INPUT"
        print(f"[{code}] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
