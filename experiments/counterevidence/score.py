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
from run import (RAW_SCHEMA_VERSION, RESULTS, active_run_id, load_jsonl,
                 run_path, validate_dataset)


# ================================================================
# 事前登録された判定基準。結果を見てから変更禁止。
# 反証可能ケースは 14件（false_coherence 6 + absence 4 + common_cause 4）。
# +0.20 は 14件中およそ 2.8件 ≒ 3件 に相当する。
# no_refutation は 6件。破壊率 0.25 は 6件中 1.5件 に相当する。
# ================================================================
PRE_REGISTRATION = {
    "primary": "Refutation Discovery Rate (C - B)",
    "initial_anchor_rule": (
        "B/Cのcounterevidence_documentsは同じ出力のinitial_conclusionを対象とする。"
        "CはC1 provisionalとの不一致を未発見として分母に残す"),
    "refutable_case_count": 14,
    "no_refutation_case_count": 6,
    "keep": {
        "refutation_discovery_delta_min": 0.20,   # 14件中 約3件
        "correct_destruction_max": 0.25,          # 6件中 1.5件
        "max_token_ratio": 2.0,
        "note": "token情報が欠損している場合、KEEP は出せない"
    },
    "conditional_keep": {
        "note": "C-B >= +0.20 だが、破壊率超過 / token比>=2.0 / token情報欠損"
    },
    "drop": {
        "note": "C-B < +0.20、または総合正答率で B を下回る"
    },
    "power_caveat": "N=20 は screening。+0.20 未満は検出力不足であり判定不能。"
                    "判定不能は DROP として扱う（不確かな部品を残さない）",
    "pilot_rule": "6件パイロットの結果で KEEP/DROP を判断してはならない"
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
def validate_results(rows, cases):
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
                             record.get("captured_at_utc"))
                if not timestamp:
                    errors.append(f"{key}: response 時刻証跡欠損")
                    break
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


# ---------------------------------------------------------------- judge
def judge_api(prompt, model="claude-haiku-4-5-20251001"):
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
    found = set(doc_ids or [])
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


def refutation_found(cited, gt, condition, malformed, anchor_ok=True):
    """B/C 共通の counterevidence_documents で判定する。

    None を返す（採点対象外）のは次の2つだけ:
      - no_refutation ケース（反証が存在しない）
      - condition A（当該フィールドを要求していない）

    B/C の反証可能ケースでは、malformed・フィールド省略・欠損はすべて
    False として分母に残す。除外すると分母が縮み、primary metric が水増しされる。
    """
    refs = gt.get("refutation_document_ids") or []
    if not refs:
        return None
    if condition == "A":
        return None
    if malformed or anchor_ok is False or cited is None or not isinstance(cited, list):
        return False
    return required_docs_found(cited, gt)


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
            cited = parsed.get("counterevidence_documents")
            conf = parsed.get("confidence")
        except Exception:
            malformed = True
            initial, concl, cited, conf = None, r["raw_answer"], None, None

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
            initial_conclusion=initial, anchor_consistent=anchor_ok,
            refutation_found=refutation_found(
                cited, gt, r["condition"], malformed, anchor_ok),
            cited_counterevidence=cited,
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
            trap_rate=mean(r["verdict"] == "trap" for r in refutable),
            refutation_discovery=mean(
                r["refutation_found"] for r in refutable
                if r["refutation_found"] is not None),
            refutation_scored_n=sum(
                1 for r in refutable if r["refutation_found"] is not None),
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
def verdict_full(agg, n_trials=None):
    if "B" not in agg or "C" not in agg:
        return "INCOMPLETE", "B または C の結果が不足"
    B, C = agg["B"], agg["C"]
    k = PRE_REGISTRATION["keep"]

    if n_trials is not None:
        expected = PRE_REGISTRATION["refutable_case_count"] * n_trials
        for name, a in (("B", B), ("C", C)):
            if a.get("refutation_scored_n") != expected:
                return "INCOMPLETE_RESULTS", (
                    f"{name} の Refutation Discovery 採点件数が "
                    f"{a.get('refutation_scored_n')} で、期待値 {expected} と一致しない。"
                    f"分母が縮んでいる状態では primary metric を比較できない")

    if B["refutation_discovery"] is None or C["refutation_discovery"] is None:
        return "INCOMPLETE", ("counterevidence_documents が取得できていない。"
                             "B/C の Refutation Discovery を計算できない")

    d_ref = C["refutation_discovery"] - B["refutation_discovery"]

    if d_ref < k["refutation_discovery_delta_min"]:
        return "DROP", (
            f"Refutation Discovery の C-B = {d_ref:+.2f}。"
            f"事前登録の閾値 +{k['refutation_discovery_delta_min']:.2f} 未満。"
            f"N={PRE_REGISTRATION['refutable_case_count']} では検出力不足のため判定不能であり、"
            f"事前登録の規定により DROP とする")

    if C["accuracy"] < B["accuracy"]:
        return "DROP", (f"反証発見は改善したが総合正答率で B を下回る "
                        f"(C={C['accuracy']:.2f} < B={B['accuracy']:.2f})")

    destr = C["correct_destruction"]
    if destr is not None and destr > k["correct_destruction_max"]:
        return "CONDITIONAL KEEP", (
            f"反証発見 +{d_ref:.2f} で改善。ただし Correct Destruction = {destr:.2f} が"
            f"閾値 {k['correct_destruction_max']:.2f} を超過。"
            f"矛盾または期待不成立が検出された場合のみ起動する条件付きで残す")

    if B["avg_tokens"] is None or C["avg_tokens"] is None:
        return "CONDITIONAL KEEP", (
            "COST_COMPARISON_UNAVAILABLE: token情報が欠損しているため、"
            "改善が計算量差で説明されないことを確認できない。"
            f"反証発見は +{d_ref:.2f} で改善しているが、事前登録により KEEP は出せない。"
            "API backend での追試が必要")

    ratio = C["avg_tokens"] / B["avg_tokens"] if B["avg_tokens"] else float("inf")
    if ratio >= k["max_token_ratio"]:
        return "CONDITIONAL KEEP", (
            f"反証発見 +{d_ref:.2f}、破壊率も許容内。ただし token比 {ratio:.2f}倍。"
            f"改善が計算量差で説明される可能性を排除できない。同予算比較の追試が必要")

    return "KEEP", (f"Refutation Discovery C-B = {d_ref:+.2f}、"
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
            ("Refutation Discovery", "refutation_discovery", 2),
            ("Correct Destruction", "correct_destruction", 2),
            ("Initial-anchor mismatch", "anchor_mismatch", 2),
            ("malformed JSON rate", "malformed", 2)]
    print(f"{'':26}{'A':>10}{'B':>10}{'C':>10}")
    print("-" * 56)
    for label, key, d in rows:
        line = f"{label:26}"
        for c in ("A", "B", "C"):
            line += f"{fmt(agg.get(c, {}).get(key), d):>10}"
        print(line)
    print()
    n_ref = sum(1 for r in recs if r["condition"] == "A"
                and r["type"] != "no_refutation")
    print(f"Refutation Discovery の採点対象件数（反証可能ケース = {n_ref}）:")
    for c in ("A", "B", "C"):
        n = agg.get(c, {}).get("refutation_scored_n")
        if c == "A":
            print(f"  A: 採点対象外（counterevidence_documents を要求していない）")
        else:
            ok = "OK" if n == n_ref else "分母が縮んでいる"
            print(f"  {c}: {n} / {n_ref}  [{ok}]")
    print()
    print("パイロットの目的:")
    print("  1) プロンプトが機能するか  2) JSONが安定するか")
    print("  3) retrieval が壊れていないか  4) floor/ceiling effect がないか")
    print()
    print("※ この結果で部品を KEEP / DROP してはならない。")
    print("※ 正式判定は 20件 (6/4/4/6) の --mode full でのみ行う。")


def print_full(agg):
    keys = [("accuracy", "Accuracy", 2), ("trap_rate", "Trap Rate", 2),
            ("refutation_discovery", "Refutation Discovery", 2),
            ("correct_destruction", "Correct Destruction", 2),
            ("unsupported", "Unsupported Claims", 2),
            ("malformed", "malformed JSON", 2),
            ("avg_calls", "Avg Calls", 2), ("avg_tokens", "Avg Tokens", 0)]
    print(f"{'':26}{'A':>10}{'B':>10}{'C':>10}{'C-B':>10}")
    print("-" * 66)
    for k, label, d in keys:
        a = agg.get("A", {}).get(k); b = agg.get("B", {}).get(k)
        c = agg.get("C", {}).get(k)
        delta = None if (b is None or c is None) else c - b
        print(f"{label:26}{fmt(a,d):>10}{fmt(b,d):>10}{fmt(c,d):>10}{fmt(delta,d):>10}")


def diagnose_c(recs, cases):
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
        cited = r["refutation_found"]
        if not reached:
            stage = "検索（falsifier または query が的外れ）"
        elif not cited:
            stage = "認識（反証を提示されたが counterevidence に挙げていない）"
        elif r["verdict"] != "correct":
            stage = "更新（反証を認識したが結論を変えていない）"
        else:
            stage = "-"
        rows.append(dict(case_id=r["case_id"], reached=reached,
                         cited=cited, correct=(r["verdict"] == "correct"),
                         stage=stage))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw")
    ap.add_argument("--results", default=str(RESULTS))
    ap.add_argument("--run-id")
    ap.add_argument("--dataset", default=str(HERE / "dataset.jsonl"))
    ap.add_argument("--specs", default=str(HERE / "specs.jsonl"))
    ap.add_argument("--mode", choices=["pilot", "full"], required=True)
    ap.add_argument("--judge", choices=["api"], default="api")
    a = ap.parse_args()

    cases = load_jsonl(a.dataset)
    specs = load_jsonl(a.specs) if pathlib.Path(a.specs).exists() else None
    errors, _ = validate_dataset(cases, specs, require_full=(a.mode == "full"))
    if errors:
        print("[VALIDATION FAILED] 採点を実行しません。")
        for e in errors:
            print("  - " + e)
        sys.exit(1)

    if a.raw:
        raw_path = pathlib.Path(a.raw)
    else:
        run_id = a.run_id or active_run_id(a.results)
        raw_path = run_path(a.results, run_id) / "raw.jsonl"
    rows = load_jsonl(raw_path)

    rerr, n_trials = validate_results(rows, cases)
    if rerr:
        print("[INCOMPLETE_RESULTS] 採点を行いません。")
        for e in rerr[:20]:
            print("  - " + e)
        if len(rerr) > 20:
            print(f"  ... 他 {len(rerr)-20} 件")
        sys.exit(1)
    print(f"[results ok] {len(rows)} rows / {n_trials} trials per condition")

    judge_fn = judge_api
    recs = score(rows, cases, judge_fn)
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

    print("\n[C の工程別診断]")
    for d in diagnose_c(recs, cases):
        print(f"  {d['case_id']:8} 到達={d['reached']!s:5} 引用={d['cited']!s:5} "
              f"正答={d['correct']!s:5} 失敗工程={d['stage']}")

    output_dir = raw_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"scored_{a.mode}.json").write_text(
        json.dumps(dict(mode=a.mode, records=recs, aggregate=agg,
                        verdict=v, reason=reason,
                        source_raw={"path": str(raw_path),
                                    "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                                    "run_id": rows[0]["provenance"]["run_id"]},
                        pre_registration=PRE_REGISTRATION),
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {output_dir/f'scored_{a.mode}.json'}")


if __name__ == "__main__":
    main()
