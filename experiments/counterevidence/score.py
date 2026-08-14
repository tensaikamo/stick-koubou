#!/usr/bin/env python3
"""
score.py -- raw.jsonl を採点する。

  --mode pilot : 記述統計のみ。KEEP/CONDITIONAL KEEP/DROP を絶対に出さない。
  --mode full  : 20件 6/4/4/6 を強制。正式判定はこのモードのみ。

判定基準は PRE_REGISTRATION に固定。結果を見てから編集した場合、その実験は無効。
"""
import argparse, json, hashlib, pathlib, sys

HERE = pathlib.Path(__file__).parent
RESULTS = HERE / "results"
sys.path.insert(0, str(HERE))
import prompts as P
import run as R
from run import (RAW_SCHEMA_VERSION, RESULTS, active_run_id, load_jsonl,
                 run_path, validate_dataset)


JUDGE_API_MODEL = "claude-haiku-4-5-20251001"
MANUAL_JUDGE_SCHEMA_VERSION = 1


# ================================================================
# 事前登録された判定基準。結果を見てから変更禁止。
# 反証可能ケースは 14件（false_coherence 6 + absence 4 + common_cause 4）。
# +0.20 は 14件中およそ 2.8件 ≒ 3件 に相当する。
# no_refutation は 6件。破壊率 0.25 は 6件中 1.5件 に相当する。
# ================================================================
PRE_REGISTRATION = {
    "version": 3,
    "frozen_on": "2026-08-15",
    "primary": "Refutable Accuracy gain (C - B)",
    "primary_formula": "refutable_accuracy_C - refutable_accuracy_B",
    "amendment_reason": (
        "v1のcounterevidence定義とground truthの対象が不一致だった。v2のTrap Rateは、"
        "trapからother/unclear/malformedへの移行を正答化と同額で評価し、さらに工程指標の"
        "分母をモデル出力に依存させていた。formal row 0件のまま第三者静的監査を受け、"
        "結論の正答化を直接測る出力非依存の指標へ修正したため"),
    "known_observations_before_freeze": {
        "formal_rows": 0,
        "AB-01_smoke": (
            "A key_documents=d2,d3,d6; B/Cはinitial=correct側、"
            "counterevidence_documents=d1。B-Cの正答率差は未観測"),
        "AB-01_legacy_C2": "PR #18以前のpromptでcounterevidence_documents=d2,d3",
        "v2_static_probes": (
            "trapからotherへの移行だけで正答増加0でもKEEP、Cの反証可能14行が"
            "malformedでもKEEPになり得ることを第三者監査がオフライン再現"),
    },
    "initial_anchor_rule": (
        "B/Cはinitial_conclusionを記録する。CはC1 provisionalとの不一致を記録し、"
        "1件でもあれば正式判定を無効とする"),
    "anchor_mismatch_max": 0.0,
    "refutable_case_count": 14,
    "no_refutation_case_count": 6,
    "keep": {
        "refutable_accuracy_gain_min": 0.20,      # 14件中 約3件
        "malformed_max": 0.0,
        "correct_destruction_max": 0.25,          # 6件中 1.5件
        "max_token_ratio": 2.0,
        "note": "token情報が欠損している場合、KEEP は出せない"
    },
    "conditional_keep": {
        "note": "Refutable Accuracy (C-B) >= +0.20 だが、破壊率超過 / token比>=2.0 / token情報欠損"
    },
    "drop": {
        "note": "Refutable Accuracy (C-B) < +0.20、Trap Rate悪化、または総合正答率でCがBを下回る"
    },
    "process_metrics": (
        "Decisive Refutation Citationは全条件のkey_documents、Fixed-refutation Retrieval Hitは"
        "Cのretrieved_docsで集計する。どちらも全反証可能行を分母としprimary判定には使わない"),
    "power_caveat": "N=20 は screening。+0.20 未満は検出力不足であり判定不能。"
                    "判定不能は DROP として扱う（不確かな部品を残さない）",
    "pilot_rule": "6件パイロットの結果で KEEP/DROP を判断してはならない",
    "invalidation_conditions": (
        "formal row生成後のPRE_REGISTRATION変更",
        "20件×全trial完了前の中間解析または逐次打ち切り",
        "run途中の生成モデル・判定モデル・設定変更",
        "anchor_mismatch > 0",
        "B/Cいずれかのprimary分母 != 14 × trials",
        "B/Cいずれかにmalformed JSONが1件以上",
        "2026-08-14 smoke応答のformal流用",
        "pilotでのKEEP/DROP判断",
    )
}


def parse_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("no JSON")
    return json.loads(t[a:b + 1])


# ---------------------------------------------------------------- 結果の完全性
def validate_results(rows, cases, expected_context=None):
    """採点前に raw.jsonl の完全性を強制する。
    欠損した条件だけを比較すると、難しいケースが落ちた側が有利になる。
    戻り値: (errors, n_trials)
    """
    errors = []
    if not rows:
        errors.append("結果が0件")
    seen = set()
    run_contracts = set()
    valid_trials = []

    def valid_sha256(value):
        return (isinstance(value, str) and len(value) == 64
                and all(ch in "0123456789abcdef" for ch in value.lower()))

    for r in rows:
        trial = r.get("trial", 0)
        key = (r.get("case_id"), r.get("condition"), trial)
        if key in seen:
            errors.append(f"重複行: {key}")
        seen.add(key)
        if type(trial) is not int or trial < 0:
            errors.append(f"{key}: trial は0以上の整数が必要")
        else:
            valid_trials.append(trial)
        if not isinstance(r.get("raw_answer"), str):
            errors.append(f"{key}: raw_answer は文字列が必要")
        provenance = r.get("provenance")
        if not isinstance(provenance, dict):
            errors.append(f"{key}: provenance 欠損")
            continue
        if provenance.get("schema_version") != RAW_SCHEMA_VERSION:
            errors.append(f"{key}: provenance schema 不正")
        required = ("run_id", "backend", "provider", "model", "settings",
                    "dataset_sha256", "specs_sha256")
        missing = [name for name in required if provenance.get(name) is None]
        if missing:
            errors.append(f"{key}: provenance 必須項目欠損 {missing}")
        if provenance.get("backend") not in ("manual", "api"):
            errors.append(f"{key}: provenance backend 不正")
        if not isinstance(provenance.get("settings"), dict):
            errors.append(f"{key}: provenance settings はobjectが必要")
        else:
            expected_verification = ("provider_reported" if provenance.get("backend") == "api"
                                     else "unverifiable_manual")
            if provenance["settings"].get("model_verification") != expected_verification:
                errors.append(f"{key}: model_verification が不正")
        if expected_context:
            for name, expected_value in expected_context.items():
                if provenance.get(name) != expected_value:
                    errors.append(f"{key}: provenance {name} がmanifest/実ファイルと不一致")
        contract = tuple(json.dumps(provenance.get(name), sort_keys=True,
                                    ensure_ascii=False) for name in required)
        run_contracts.add(contract)
        usage = r.get("usage")
        calls = usage.get("calls") if isinstance(usage, dict) else None
        expected_calls = 2 if r.get("condition") == "C" else 1
        if type(calls) is not int or calls != expected_calls:
            errors.append(f"{key}: usage.calls は {expected_calls} が必要（現在 {calls!r}）")
        records = (provenance.get("calls") if provenance.get("backend") == "api"
                   else provenance.get("responses"))
        records_ok = (isinstance(records, list) and bool(records)
                      and type(calls) is int and len(records) == calls
                      and all(isinstance(record, dict) for record in records))
        if not records_ok:
            errors.append(f"{key}: call証跡件数 {0 if not isinstance(records,list) else len(records)}"
                          f" != usage.calls {calls}")
        else:
            for record in records:
                digest = record.get("response_sha256") or record.get("sha256")
                if not valid_sha256(digest):
                    errors.append(f"{key}: response SHA-256 不正")
                    break
                timestamp = (record.get("completed_at_utc") or
                             record.get("collected_at_utc"))
                if not timestamp:
                    errors.append(f"{key}: response 時刻証跡欠損")
                    break
                if provenance.get("backend") == "api":
                    if record.get("requested_model") != provenance.get("model"):
                        errors.append(f"{key}: requested_model がrun modelと不一致")
                    if record.get("reported_model") != record.get("requested_model"):
                        errors.append(f"{key}: provider reported_model が要求modelと不一致")
                    if record.get("stop_reason") != "end_turn":
                        errors.append(f"{key}: API応答が正常完了していない "
                                      f"(stop_reason={record.get('stop_reason')!r})")
            final_digest = records[-1].get("response_sha256") or records[-1].get("sha256")
            actual_digest = hashlib.sha256(
                str(r.get("raw_answer", "")).encode("utf-8")).hexdigest()
            if final_digest != actual_digest:
                errors.append(f"{key}: raw_answer と response SHA-256 が不一致")
        prompts = (provenance.get("calls") if provenance.get("backend") == "api"
                   else provenance.get("prompts"))
        prompts_ok = (isinstance(prompts, list) and bool(prompts)
                      and type(calls) is int and len(prompts) == calls
                      and all(isinstance(record, dict) for record in prompts))
        if not prompts_ok:
            errors.append(f"{key}: prompt証跡件数が usage.calls と不一致")
        else:
            for record in prompts:
                digest = record.get("prompt_sha256") or record.get("sha256")
                if not valid_sha256(digest):
                    errors.append(f"{key}: prompt SHA-256 不正")
                    break

    if len(run_contracts) > 1:
        errors.append("複数runまたは異なる実行条件の結果が混在")

    trials = sorted(set(valid_trials))
    n_trials = len(trials)
    if trials != list(range(n_trials)):
        errors.append(f"trial ID が 0..n-1 の連番でない: {trials}")

    case_ids = [c["id"] for c in cases]
    for cid in case_ids:
        for cond in ("A", "B", "C"):
            got = sorted(r.get("trial", 0) for r in rows
                         if r.get("case_id") == cid and r.get("condition") == cond
                         and type(r.get("trial", 0)) is int
                         and r.get("trial", 0) >= 0)
            if got != trials:
                errors.append(f"{cid}/{cond}: trial {trials} が必要（現在 {got}）")

    expected = len(case_ids) * 3 * n_trials
    if len(rows) != expected:
        errors.append(f"行数 {len(rows)} != 期待 {expected} "
                      f"({len(case_ids)} cases x 3 conditions x {n_trials} trials)")
    return errors, n_trials


def validate_run_bundle(raw_path, results_root, dataset_path, specs_path):
    """raw・manifest・実入力・追跡対象attestationを一つのrunとして束縛する。"""
    errors = []
    raw_path, results_root = pathlib.Path(raw_path), pathlib.Path(results_root)
    if not raw_path.is_file():
        return [f"raw.jsonl が存在しない: {raw_path}"], None
    if raw_path.name != "raw.jsonl":
        errors.append("採点対象ファイル名は raw.jsonl 固定")
    run_dir = raw_path.parent
    try:
        expected_runs = (results_root / "runs").resolve()
        if run_dir.parent.resolve() != expected_runs:
            errors.append(f"raw は {expected_runs}/<run_id>/raw.jsonl 配下に必要")
    except OSError as error:
        errors.append(f"run path を解決できない: {error}")

    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.is_file():
        return errors + [f"run_manifest.json が存在しない: {manifest_path}"], None
    manifest = R.load_manifest(run_dir)
    run_id = manifest.get("run_id")
    if run_id != run_dir.name:
        errors.append(f"manifest run_id {run_id!r} とdirectory名が不一致")
    if manifest.get("status") != "complete":
        errors.append(f"run が未完了: status={manifest.get('status')!r}")
    raw_meta = manifest.get("raw") if isinstance(manifest.get("raw"), dict) else {}
    actual_raw_sha = R.sha256_file(raw_path)
    if raw_meta.get("sha256") != actual_raw_sha:
        errors.append("raw.jsonl SHA-256 がmanifestと不一致")

    errors.extend(R.validate_manifest_context(
        manifest, dataset_path, specs_path, run_dir))

    ledger = R.load_attestations(results_root)
    entries = [entry for entry in ledger["entries"] if entry.get("run_id") == run_id]
    if len(entries) != 1:
        errors.append(f"attestation はrunごとに1件必要（現在 {len(entries)}）")
    else:
        entry = entries[0]
        expected = {
            "raw_sha256": actual_raw_sha,
            "manifest_sha256": R.sha256_file(manifest_path),
            "dataset_sha256": R.sha256_file(dataset_path),
            "specs_sha256": R.sha256_file(specs_path),
            "backend": manifest.get("backend"), "provider": manifest.get("provider"),
            "model": manifest.get("model")}
        for name, value in expected.items():
            if entry.get(name) != value:
                errors.append(f"attestation {name} が実体と不一致")
    return errors, manifest


# ---------------------------------------------------------------- judge
def judge_api(prompt, model=JUDGE_API_MODEL):
    import anthropic
    c = anthropic.Anthropic()
    r = c.messages.create(model=model, max_tokens=300,
                          messages=[{"role": "user", "content": prompt}])
    return parse_json("".join(b.text for b in r.content if b.type == "text"))


def stable_flip(case_id):
    """builtin hash() を使わない。プロセスを跨いで同一。
    条件ごとではなく case ごとに固定し、position bias が条件差へ混入するのを防ぐ。"""
    h = hashlib.sha256(case_id.encode("utf-8")).digest()
    return bool(h[0] & 1)


def match_conclusion(answer, gt, judge_fn, case_id):
    correct = gt["correct_conclusion"]
    trap = gt.get("trap_conclusion")
    if not trap:
        # no_refutation 専用。二択候補ではなく reference との一致のみを問う。
        res = judge_fn(P.build_judge_nr(correct, answer))
        return ("correct" if res.get("matches_reference") is True else "other"), res
    flip = stable_flip(case_id)
    c1, c2 = (trap, correct) if flip else (correct, trap)
    res = judge_fn(P.build_judge(c1, c2, answer))
    m = res.get("match")
    if m == "1":
        return ("trap" if flip else "correct"), res
    if m == "2":
        return ("correct" if flip else "trap"), res
    return ("other" if m == "neither" else "unclear"), res


# ---------------------------------------------------------------- discovery
def required_docs_found(doc_ids, gt):
    """ground truth の ANY/ALL 条件で必要文書への到達を判定する。"""
    refs = set(gt.get("refutation_document_ids") or [])
    found = (set(doc_ids) if isinstance(doc_ids, list)
             and all(isinstance(doc_id, str) for doc_id in doc_ids) else set())
    if not refs:
        return False
    if gt.get("refutation_match") == "ALL":
        return refs <= found
    return bool(refs & found)


def normalize_anchor(value):
    """意味比較ではなく、C1からC2へのコピー契約だけを空白正規化して確認する。"""
    return " ".join(value.split()) if isinstance(value, str) else ""


def anchor_consistent(initial, provisional, condition, malformed):
    """B/C が同じ意味対象を引用しているか確認する。

    B は出力された初期説明が非空であること、C はそれに加えて C1 の暫定結論を
    C2 が変更せず引き継いだことを要求する。A は比較対象外。
    """
    if condition == "A":
        return None
    if malformed or not normalize_anchor(initial):
        return False
    if condition == "C":
        return normalize_anchor(initial) == normalize_anchor(provisional)
    return True


# ---------------------------------------------------------------- scoring
def score(rows, cases, judge_fn):
    by_id = {c["id"]: c for c in cases}
    recs = []
    for r in rows:
        case = by_id[r["case_id"]]
        gt = case["ground_truth"]
        malformed = False
        try:
            parsed = parse_json(r["raw_answer"])
            initial = parsed.get("initial_conclusion")
            concl = parsed.get("conclusion", "")
            key_documents = parsed.get("key_documents")
            conf = parsed.get("confidence")
        except Exception:
            malformed = True
            initial, concl, key_documents, conf = (
                None, r["raw_answer"], None, None)

        anchor_ok = anchor_consistent(
            initial, r.get("provisional_answer"), r["condition"], malformed)

        verdict, _ = match_conclusion(concl, gt, judge_fn, r["case_id"])
        uns = judge_fn(P.build_unsupported(
            P.format_docs(case["documents"]), concl))

        u = r.get("usage", {}) or {}
        recs.append(dict(
            case_id=r["case_id"], type=case["type"], condition=r["condition"],
            trial=r.get("trial", 0), malformed=malformed,
            verdict=verdict,
            initial_conclusion=initial,
            anchor_consistent=anchor_ok,
            key_documents=key_documents,
            decisive_refutation_citation=(
                required_docs_found(key_documents, gt)
                if case["type"] != "no_refutation" else None),
            fixed_refutation_retrieval_hit=(
                required_docs_found(r.get("retrieved_docs"), gt)
                if r["condition"] == "C" and case["type"] != "no_refutation"
                else None),
            unsupported=bool(uns.get("has_unsupported")),
            confidence=conf,
            calls=u.get("calls"),
            in_tok=u.get("input_tokens"),      # 欠損なら None のまま
            out_tok=u.get("output_tokens"),
            retrieved=r.get("retrieved_docs"),
            falsifier=r.get("falsifier"),
            provisional=r.get("provisional_answer")))
    return recs


def mean(xs):
    xs = [float(x) for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def total_tokens(r):
    if r["in_tok"] is None or r["out_tok"] is None:
        return None
    return r["in_tok"] + r["out_tok"]


def aggregate(recs):
    out = {}
    for c in ("A", "B", "C"):
        rs = [r for r in recs if r["condition"] == c]
        if not rs:
            continue
        refutable = [r for r in rs if r["type"] != "no_refutation"]
        nr = [r for r in rs if r["type"] == "no_refutation"]
        toks = [total_tokens(r) for r in rs]
        out[c] = dict(
            n=len(rs),
            accuracy=mean(r["verdict"] == "correct" for r in rs),
            refutable_accuracy=mean(r["verdict"] == "correct" for r in refutable),
            refutable_accuracy_n=len(refutable),
            trap_rate=mean(r["verdict"] == "trap" for r in refutable),
            trap_rate_scored_n=len(refutable),
            decisive_refutation_citation=mean(
                r.get("decisive_refutation_citation") is True for r in refutable),
            decisive_refutation_citation_n=len(refutable),
            fixed_refutation_retrieval_hit=(
                mean(r.get("fixed_refutation_retrieval_hit") is True for r in refutable)
                if c == "C" else None),
            fixed_refutation_retrieval_hit_n=(len(refutable) if c == "C" else 0),
            correct_destruction=mean(r["verdict"] != "correct" for r in nr),
            unsupported=mean(r["unsupported"] for r in rs),
            malformed=mean(r["malformed"] for r in rs),
            anchor_mismatch=mean(r.get("anchor_consistent") is False for r in rs
                                 if r.get("anchor_consistent") is not None),
            avg_calls=mean(r["calls"] for r in rs),
            avg_tokens=(None if any(t is None for t in toks) else mean(toks)),
        )
    return out


# ---------------------------------------------------------------- verdict
def primary_effect(agg):
    """事前登録v3のprimary: 反証可能ケースの正答率差 (C-B)。"""
    if "B" not in agg or "C" not in agg:
        return None
    b = agg["B"].get("refutable_accuracy")
    c = agg["C"].get("refutable_accuracy")
    return None if b is None or c is None else c - b


def verdict_full(agg, n_trials=None):
    if "B" not in agg or "C" not in agg:
        return "INCOMPLETE", "B または C の結果が不足"
    B, C = agg["B"], agg["C"]
    k = PRE_REGISTRATION["keep"]

    for name, values in (("B", B), ("C", C)):
        malformed = values.get("malformed")
        if malformed is None or malformed > k["malformed_max"]:
            return "INVALID_OUTPUT", (
                f"{name} の malformed JSON rate={malformed!r}。"
                "正式判定はB/Cとも0件であることを要求する")

    for name, values in (("B", B), ("C", C)):
        mismatch = values.get("anchor_mismatch")
        if mismatch is None or mismatch > PRE_REGISTRATION["anchor_mismatch_max"]:
            return "INVALID_ANCHOR", (
                f"{name} の initial-anchor mismatch={mismatch!r}。"
                "C1→C2の結論固定を確認できないため正式判定を出さない")

    if n_trials is not None:
        expected = PRE_REGISTRATION["refutable_case_count"] * n_trials
        for name, a in (("B", B), ("C", C)):
            if a.get("refutable_accuracy_n") != expected:
                return "INCOMPLETE_RESULTS", (
                    f"{name} の Refutable Accuracy 採点件数が "
                    f"{a.get('refutable_accuracy_n')} で、期待値 {expected} と一致しない。"
                    f"分母が縮んでいる状態では primary metric を比較できない")

    if B.get("refutable_accuracy") is None or C.get("refutable_accuracy") is None:
        return "INCOMPLETE", "B/C の Refutable Accuracy を計算できない"

    d_accuracy = primary_effect(agg)

    if d_accuracy < k["refutable_accuracy_gain_min"]:
        return "DROP", (
            f"Refutable Accuracy gain の C-B = {d_accuracy:+.2f}。"
            f"事前登録の閾値 +{k['refutable_accuracy_gain_min']:.2f} 未満。"
            f"N={PRE_REGISTRATION['refutable_case_count']} では検出力不足のため判定不能であり、"
            f"事前登録の規定により DROP とする")

    if C.get("trap_rate") is None or B.get("trap_rate") is None:
        return "INCOMPLETE", "B/C の Trap Rate を計算できない"
    if C["trap_rate"] > B["trap_rate"]:
        return "DROP", (f"Refutable Accuracyは改善したがTrap Rateが悪化した "
                        f"(C={C['trap_rate']:.2f} > B={B['trap_rate']:.2f})")

    if C["accuracy"] < B["accuracy"]:
        return "DROP", (f"反証可能ケースは改善したが総合正答率で C が B を下回る "
                        f"(C={C['accuracy']:.2f} < B={B['accuracy']:.2f})")

    destr = C["correct_destruction"]
    if destr is not None and destr > k["correct_destruction_max"]:
        return "CONDITIONAL KEEP", (
            f"Refutable Accuracy C-B = +{d_accuracy:.2f}。ただし Correct Destruction = {destr:.2f} が"
            f"閾値 {k['correct_destruction_max']:.2f} を超過。"
            f"矛盾または期待不成立が検出された場合のみ起動する条件付きで残す")

    if B["avg_tokens"] is None or C["avg_tokens"] is None:
        return "CONDITIONAL KEEP", (
            "COST_COMPARISON_UNAVAILABLE: token情報が欠損しているため、"
            "改善が計算量差で説明されないことを確認できない。"
            f"Refutable Accuracyは C-B = +{d_accuracy:.2f} で改善しているが、"
            "事前登録により KEEP は出せない。"
            "API backend での追試が必要")

    ratio = C["avg_tokens"] / B["avg_tokens"] if B["avg_tokens"] else float("inf")
    if ratio >= k["max_token_ratio"]:
        return "CONDITIONAL KEEP", (
            f"Refutable Accuracy C-B = +{d_accuracy:.2f}、破壊率も許容内。"
            f"ただし token比 {ratio:.2f}倍。"
            f"改善が計算量差で説明される可能性を排除できない。同予算比較の追試が必要")

    return "KEEP", (f"Refutable Accuracy gain C-B = {d_accuracy:+.2f}、"
                    f"Correct Destruction = {destr:.2f}、token比 {ratio:.2f}倍。"
                    f"事前登録の3条件すべて充足")


# ---------------------------------------------------------------- 出力
def fmt(v, digits=2):
    if v is None:
        return "N/A"
    return f"{v:.{digits}f}" if digits else f"{v:.0f}"


def print_pilot(agg, recs):
    print("=" * 70)
    print("PILOT MODE — 記述統計のみ。KEEP/DROP 判定は出しません。")
    print("=" * 70)
    rows = [("Accuracy", "accuracy", 2),
            ("Refutable Accuracy*", "refutable_accuracy", 2),
            ("Trap Rate", "trap_rate", 2),
            ("Decisive Refutation Citation", "decisive_refutation_citation", 2),
            ("Fixed-refutation Retrieval Hit", "fixed_refutation_retrieval_hit", 2),
            ("Correct Destruction", "correct_destruction", 2),
            ("Initial-anchor mismatch", "anchor_mismatch", 2),
            ("malformed JSON rate", "malformed", 2)]
    print(f"{'':31}{'A':>10}{'B':>10}{'C':>10}")
    print("-" * 61)
    for label, key, d in rows:
        line = f"{label:31}"
        for c in ("A", "B", "C"):
            line += f"{fmt(agg.get(c, {}).get(key), d):>10}"
        print(line)
    print()
    n_ref = sum(1 for r in recs if r["condition"] == "A"
                and r["type"] != "no_refutation")
    print(f"反証可能ケースの分母（1条件あたり = {n_ref}）:")
    for c in ("A", "B", "C"):
        values = agg.get(c, {})
        print(f"  {c}: Refutable Accuracy n={values.get('refutable_accuracy_n')}, "
              f"Trap Rate n={values.get('trap_rate_scored_n')}, "
              f"Citation n={values.get('decisive_refutation_citation_n')}, "
              f"Retrieval n={values.get('fixed_refutation_retrieval_hit_n')}")
    print("  * primary は Refutable Accuracy の C-B。出力依存の分母は使わない。")
    print()
    print("パイロットの目的:")
    print("  1) プロンプトが機能するか  2) JSONが安定するか")
    print("  3) retrieval が壊れていないか  4) floor/ceiling effect がないか")
    print()
    print("※ この結果で部品を KEEP / DROP してはならない。")
    print("※ 正式判定は 20件 (6/4/4/6) の --mode full でのみ行う。")


def print_full(agg):
    keys = [("accuracy", "Accuracy", 2),
            ("refutable_accuracy", "Refutable Accuracy", 2),
            ("trap_rate", "Trap Rate", 2),
            ("decisive_refutation_citation", "Decisive Refutation Citation", 2),
            ("fixed_refutation_retrieval_hit", "Fixed-refutation Retrieval Hit", 2),
            ("correct_destruction", "Correct Destruction", 2),
            ("unsupported", "Unsupported Claims", 2),
            ("anchor_mismatch", "Initial-anchor mismatch", 2),
            ("malformed", "malformed JSON", 2),
            ("avg_calls", "Avg Calls", 2), ("avg_tokens", "Avg Tokens", 0)]
    print(f"{'':31}{'A':>10}{'B':>10}{'C':>10}{'Δ':>10}")
    print("-" * 71)
    for k, label, d in keys:
        a = agg.get("A", {}).get(k); b = agg.get("B", {}).get(k)
        c = agg.get("C", {}).get(k)
        # Trap Rateだけは「低いほど良い」ので B-C。他は C-B。
        delta = None if (b is None or c is None) else (b - c if k == "trap_rate" else c - b)
        print(f"{label:31}{fmt(a,d):>10}{fmt(b,d):>10}{fmt(c,d):>10}{fmt(delta,d):>10}")
    print("Δ: primaryのRefutable Accuracyは C-B。Trap Rateのみ B-C、その他は C-B")
    print("\n分母:")
    for condition in ("A", "B", "C"):
        values = agg.get(condition, {})
        print(f"  {condition}: Refutable Accuracy n={values.get('refutable_accuracy_n')}, "
              f"Trap Rate n={values.get('trap_rate_scored_n')}, "
              f"Citation n={values.get('decisive_refutation_citation_n')}, "
              f"Retrieval n={values.get('fixed_refutation_retrieval_hit_n')}")
    print("  * すべての分母はケース種別だけで決まり、モデル出力では変化しない。")


def diagnose_c(recs, cases):
    """Cの検索ヒットと最終正答を記述する。失敗工程の因果推定は行わない。"""
    by_id = {c["id"]: c for c in cases}
    rows = []
    for r in recs:
        if r["condition"] != "C":
            continue
        gt = by_id[r["case_id"]]["ground_truth"]
        refs = gt.get("refutation_document_ids") or []
        if not refs:
            continue
        reached = required_docs_found(r.get("retrieved"), gt)
        rows.append(dict(case_id=r["case_id"], reached=reached,
                         correct=(r["verdict"] == "correct")))
    return rows


# ---------------------------------------------------------------- APIを使わない手動judge
def manual_judge_kind(prompt):
    """judge prompt の出力schemaを、テンプレートに依存する固定3種へ分類する。"""
    if '"has_unsupported"' in prompt:
        return "unsupported"
    if '"matches_reference"' in prompt:
        return "reference"
    if '"match"' in prompt:
        return "match"
    raise ValueError("未知のjudge prompt形式")


def validate_manual_judge_response(value, kind):
    """手動UIから回収したjudge JSONをfail-closedで検証する。"""
    if not isinstance(value, dict):
        raise ValueError("judge応答はJSON objectが必要")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        raise ValueError("judge応答の reason は非空文字列が必要")
    if kind == "match":
        if value.get("match") not in ("1", "2", "neither", "unclear"):
            raise ValueError("match は 1|2|neither|unclear が必要")
    elif kind == "reference":
        if type(value.get("matches_reference")) is not bool:
            raise ValueError("matches_reference はbooleanが必要")
    elif kind == "unsupported":
        if type(value.get("has_unsupported")) is not bool:
            raise ValueError("has_unsupported はbooleanが必要")
        items = value.get("items")
        if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
            raise ValueError("unsupported応答の items は文字列配列が必要")
    else:
        raise ValueError(f"未知のjudge kind: {kind!r}")


class ManualJudgeCollector:
    """score() と同じ経路を通して、必要なjudge promptだけを収集する。"""
    def __init__(self):
        self.prompts = {}
        self.call_count = 0

    def __call__(self, prompt):
        self.call_count += 1
        digest = R.sha256_text(prompt)
        self.prompts.setdefault(digest, {
            "sha256": digest, "kind": manual_judge_kind(prompt), "text": prompt})
        kind = self.prompts[digest]["kind"]
        if kind == "match":
            return {"match": "unclear", "reason": "export placeholder"}
        if kind == "reference":
            return {"matches_reference": False, "reason": "export placeholder"}
        return {"has_unsupported": False, "items": [],
                "reason": "export placeholder"}


class ManualJudgeResponses:
    """prompt SHAに束縛された手動応答だけを score() へ返す。"""
    def __init__(self, responses, expected_calls):
        self.responses = responses
        self.expected_calls = expected_calls
        self.call_count = 0
        self.seen = set()

    def __call__(self, prompt):
        digest = R.sha256_text(prompt)
        if digest not in self.responses:
            raise ValueError(f"未登録のjudge prompt: {digest}")
        self.call_count += 1
        self.seen.add(digest)
        return self.responses[digest]


def _manual_judge_source(raw_path, dataset_path, specs_path):
    return {
        "raw_sha256": R.sha256_file(raw_path),
        "dataset_sha256": R.sha256_file(dataset_path),
        "specs_sha256": R.sha256_file(specs_path),
        "prompts_py_sha256": R.sha256_file(HERE / "prompts.py"),
        "run_py_sha256": R.sha256_file(HERE / "run.py"),
        "score_py_sha256": R.sha256_file(HERE / "score.py"),
    }


def export_manual_judge(rows, cases, raw_path, dataset_path, specs_path,
                        provider, model):
    """APIを呼ばず、サブスクUIへ貼るjudge prompt packetを一度だけ作る。"""
    if not provider or not model:
        raise ValueError("manual judge export は --judge-provider と --judge-model が必須")
    raw_path = pathlib.Path(raw_path)
    judge_dir = raw_path.parent / "manual_judge"
    manifest_path = judge_dir / "manifest.json"
    if judge_dir.exists():
        if not manifest_path.is_file():
            raise ValueError(f"不完全なmanual_judge directoryが存在する: {judge_dir}")
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = _manual_judge_source(raw_path, dataset_path, specs_path)
        if (existing.get("source") != expected
                or existing.get("judge", {}).get("provider") != provider
                or existing.get("judge", {}).get("model") != model):
            raise ValueError("既存manual judge packetのraw/code/provider/modelが現在値と不一致")
        return judge_dir, existing, False

    collector = ManualJudgeCollector()
    score(rows, cases, collector)
    judge_dir.mkdir(parents=False)
    prompt_dir, response_dir = judge_dir / "prompts", judge_dir / "responses"
    prompt_dir.mkdir(); response_dir.mkdir()
    records = []
    for index, item in enumerate(collector.prompts.values(), 1):
        request_id = f"J{index:03d}"
        prompt_rel = f"prompts/{request_id}.txt"
        response_rel = f"responses/{request_id}.txt"
        (judge_dir / prompt_rel).write_text(item["text"], encoding="utf-8")
        records.append({
            "request_id": request_id, "kind": item["kind"],
            "prompt_file": prompt_rel, "prompt_sha256": item["sha256"],
            "response_file": response_rel})
    manifest = {
        "schema_version": MANUAL_JUDGE_SCHEMA_VERSION,
        "status": "awaiting_responses", "created_at_utc": R.utc_now(),
        "source": _manual_judge_source(raw_path, dataset_path, specs_path),
        "judge": {"route": "subscription_ui", "api_used": False,
                  "provider": provider, "model": model,
                  "model_verification": "unverifiable_manual"},
        "call_count": collector.call_count,
        "unique_prompt_count": len(records), "prompts": records}
    R.write_json(manifest_path, manifest)
    instructions = (
        "API課金なし manual judge\n\n"
        "1. prompts/Jxxx.txt を1件ずつ別の新規チャットへ貼る。\n"
        "2. 返ったJSONだけを同番号の responses/Jxxx.txt へ保存する。\n"
        "3. export時と同じ score.py コマンドで --manual-stage score を実行する。\n"
        "注意: 全件で同じprovider/modelを使い、途中変更しない。\n")
    (judge_dir / "HOW_TO.txt").write_text(instructions, encoding="utf-8")
    return judge_dir, manifest, True


def load_manual_judge(raw_path, dataset_path, specs_path):
    """manual judge packetと全応答を検証し、SHA束縛済みcallableを返す。"""
    raw_path = pathlib.Path(raw_path)
    judge_dir = raw_path.parent / "manual_judge"
    manifest_path = judge_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("manual judge manifestがない。先に --manual-stage export が必要")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANUAL_JUDGE_SCHEMA_VERSION:
        raise ValueError("manual judge schema_versionが不正")
    if manifest.get("status") not in ("awaiting_responses", "complete"):
        raise ValueError("manual judge statusが不正")
    if manifest.get("source") != _manual_judge_source(raw_path, dataset_path, specs_path):
        raise ValueError("manual judge packetのraw/code/input SHAが現在値と不一致")
    judge = manifest.get("judge")
    if not (isinstance(judge, dict) and judge.get("route") == "subscription_ui"
            and judge.get("api_used") is False
            and judge.get("model_verification") == "unverifiable_manual"
            and isinstance(judge.get("provider"), str) and judge["provider"].strip()
            and isinstance(judge.get("model"), str) and judge["model"].strip()):
        raise ValueError("manual judge provenanceが不正")
    records = manifest.get("prompts")
    if (not isinstance(records, list) or not records
            or manifest.get("unique_prompt_count") != len(records)):
        raise ValueError("manual judge prompt manifestが不完全")

    responses, response_records = {}, []
    for record in records:
        request_id = record.get("request_id") if isinstance(record, dict) else None
        if not (isinstance(request_id, str) and len(request_id) == 4
                and request_id.startswith("J") and request_id[1:].isdigit()):
            raise ValueError("manual judge request_idが不正")
        prompt_path = judge_dir / "prompts" / f"{request_id}.txt"
        response_path = judge_dir / "responses" / f"{request_id}.txt"
        if record.get("prompt_file") != f"prompts/{request_id}.txt" \
                or record.get("response_file") != f"responses/{request_id}.txt":
            raise ValueError(f"{request_id}: manual judge pathが不正")
        if not prompt_path.is_file():
            raise ValueError(f"{request_id}: judge promptがない")
        prompt = prompt_path.read_text(encoding="utf-8")
        digest = R.sha256_text(prompt)
        if digest != record.get("prompt_sha256"):
            raise ValueError(f"{request_id}: judge prompt SHA-256が不一致")
        if manual_judge_kind(prompt) != record.get("kind"):
            raise ValueError(f"{request_id}: judge prompt kindが不一致")
        if not response_path.is_file():
            raise ValueError(f"{request_id}: judge応答が未回収")
        raw_response = response_path.read_text(encoding="utf-8")
        try:
            value = parse_json(raw_response)
            validate_manual_judge_response(value, record["kind"])
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"{request_id}: judge応答不正: {error}") from error
        responses[digest] = value
        response_records.append({
            "request_id": request_id, "response_sha256": R.sha256_text(raw_response),
            "observed_at_utc": R.utc_now()})
    if manifest["status"] == "complete":
        stored = manifest.get("responses")
        if not isinstance(stored, list) or len(stored) != len(response_records):
            raise ValueError("complete manual judgeのresponse証跡が不完全")
        stored_by_id = {record.get("request_id"): record for record in stored
                        if isinstance(record, dict)}
        if len(stored_by_id) != len(stored):
            raise ValueError("complete manual judgeのresponse証跡IDが不正")
        for current in response_records:
            recorded = stored_by_id.get(current["request_id"])
            if (not recorded
                    or recorded.get("response_sha256") != current["response_sha256"]
                    or not recorded.get("observed_at_utc")):
                raise ValueError(
                    f"{current['request_id']}: 完了後のjudge応答SHAが不一致")
        response_records = stored
    elif manifest.get("responses") is not None:
        raise ValueError("awaiting_responses に完了済みresponse証跡が混入")
    expected_calls = manifest.get("call_count")
    if type(expected_calls) is not int or expected_calls < len(records):
        raise ValueError("manual judge call_countが不正")
    provenance = dict(judge, response_records=response_records)
    return ManualJudgeResponses(responses, expected_calls), provenance


def finalize_manual_judge(raw_path, provenance):
    """採点経路が全promptを消費した後だけresponse SHAをmanifestへ固定する。"""
    judge_dir = pathlib.Path(raw_path).parent / "manual_judge"
    manifest_path = judge_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") == "awaiting_responses":
        manifest["status"] = "complete"
        manifest["completed_at_utc"] = R.utc_now()
        manifest["responses"] = provenance["response_records"]
        R.write_json(manifest_path, manifest)
        for record in manifest["prompts"]:
            (judge_dir / record["prompt_file"]).chmod(0o444)
            (judge_dir / record["response_file"]).chmod(0o444)
        manifest_path.chmod(0o444)
    elif manifest.get("status") != "complete":
        raise ValueError("manual judgeをcompleteにできないstatus")
    finalized = dict(provenance)
    finalized["status"] = "complete"
    finalized["manifest_sha256"] = R.sha256_file(manifest_path)
    return finalized


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw")
    ap.add_argument("--results", default=str(RESULTS))
    ap.add_argument("--run-id")
    ap.add_argument("--dataset", default=str(HERE / "dataset.jsonl"))
    ap.add_argument("--specs", default=str(HERE / "specs.jsonl"))
    ap.add_argument("--mode", choices=["pilot", "full"], required=True)
    ap.add_argument("--judge", choices=["api", "manual"], default="api")
    ap.add_argument("--manual-stage", choices=["export", "score"])
    ap.add_argument("--judge-provider")
    ap.add_argument("--judge-model")
    a = ap.parse_args()

    try:
        dataset_path, specs_path = pathlib.Path(a.dataset), pathlib.Path(a.specs)
        cases = load_jsonl(dataset_path)
        specs = load_jsonl(specs_path)
        errors, _ = validate_dataset(cases, specs, require_full=(a.mode == "full"))
        if errors:
            print("[VALIDATION FAILED] 採点を実行しません。")
            for error in errors:
                print("  - " + error)
            sys.exit(1)

        if a.raw:
            raw_path = pathlib.Path(a.raw)
        else:
            run_id = a.run_id or active_run_id(a.results)
            raw_path = run_path(a.results, run_id) / "raw.jsonl"
        bundle_errors, manifest = validate_run_bundle(
            raw_path, a.results, dataset_path, specs_path)
        if bundle_errors:
            raise ValueError("\n  - ".join(bundle_errors))
        rows = load_jsonl(raw_path)
        expected_context = {
            "run_id": manifest["run_id"], "backend": manifest["backend"],
            "provider": manifest["provider"], "model": manifest["model"],
            "settings": manifest["settings"],
            "dataset_sha256": R.sha256_file(dataset_path),
            "specs_sha256": R.sha256_file(specs_path)}
        rerr, n_trials = validate_results(rows, cases, expected_context)
        if manifest.get("raw", {}).get("rows") != len(rows):
            rerr.append("raw行数がmanifestと不一致")
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print("[INCOMPLETE_RESULTS] 採点を行いません。")
        print(f"  - {error}")
        sys.exit(1)

    if rerr:
        print("[INCOMPLETE_RESULTS] 採点を行いません。")
        for e in rerr[:20]:
            print("  - " + e)
        if len(rerr) > 20:
            print(f"  ... 他 {len(rerr)-20} 件")
        sys.exit(1)
    print(f"[results ok] {len(rows)} rows / {n_trials} trials per condition")
    if manifest["settings"].get("model_verification") == "unverifiable_manual":
        print("[provenance warning] manual model identity is operator-declared and unverifiable")

    judge_provenance = None
    try:
        if a.judge == "manual":
            if not a.manual_stage:
                raise ValueError("--judge manual は --manual-stage export|score が必須")
            if a.manual_stage == "export":
                judge_dir, judge_manifest, created = export_manual_judge(
                    rows, cases, raw_path, dataset_path, specs_path,
                    a.judge_provider, a.judge_model)
                action = "created" if created else "already exists"
                print(f"[manual judge packet {action}] {judge_dir}")
                print(f"  {judge_manifest['unique_prompt_count']} unique prompts / "
                      f"{judge_manifest['call_count']} scoring calls")
                print(f"  1) {judge_dir/'HOW_TO.txt'} を開く")
                print("  2) 全response保存後、同じコマンドの --manual-stage を score にする")
                return
            judge_fn, judge_provenance = load_manual_judge(
                raw_path, dataset_path, specs_path)
            print("[judge provenance warning] subscription UI model identity is "
                  "operator-declared and unverifiable")
        else:
            if a.manual_stage or a.judge_provider or a.judge_model:
                raise ValueError("manual judge用引数は --judge manual の場合だけ指定できる")
            judge_fn = judge_api
            judge_provenance = {
                "route": "anthropic_api", "api_used": True,
                "provider": "anthropic", "model": JUDGE_API_MODEL,
                "model_verification": "provider_response_not_recorded"}
        recs = score(rows, cases, judge_fn)
        if isinstance(judge_fn, ManualJudgeResponses):
            if judge_fn.call_count != judge_fn.expected_calls:
                raise ValueError(
                    f"manual judge call数 {judge_fn.call_count} != packet "
                    f"{judge_fn.expected_calls}")
            if judge_fn.seen != set(judge_fn.responses):
                raise ValueError("manual judge packetに未使用または不足promptがある")
            judge_provenance = finalize_manual_judge(raw_path, judge_provenance)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print("[INCOMPLETE_JUDGMENTS] 採点を行いません。")
        print(f"  - {error}")
        sys.exit(1)
    agg = aggregate(recs)

    if a.mode == "pilot":
        print_pilot(agg, recs)
        v, reason = "NOT_APPLICABLE", "pilot モードでは正式判定を行わない"
    else:
        print_full(agg)
        v, reason = verdict_full(agg, n_trials)
        print("\n" + "=" * 66)
        print(f"判定: {v}")
        print(f"理由: {reason}")
        print("=" * 66)

    print("\n[C の記述的検索監査]")
    for d in diagnose_c(recs, cases):
        print(f"  {d['case_id']:8} 固定反証文書の検索ヒット={d['reached']!s:5} "
              f"最終正答={d['correct']!s:5}")

    output_dir = raw_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"scored_{a.mode}.json").write_text(
        json.dumps(dict(mode=a.mode, records=recs, aggregate=agg,
                        verdict=v, reason=reason,
                        judge_provenance=judge_provenance,
                        source_raw={"path": str(raw_path),
                                    "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                                    "run_id": rows[0]["provenance"]["run_id"],
                                    "model_verification": manifest["settings"].get(
                                        "model_verification")},
                        pre_registration=PRE_REGISTRATION),
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {output_dir/f'scored_{a.mode}.json'}")


if __name__ == "__main__":
    main()
