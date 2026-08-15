#!/usr/bin/env python3
"""selftest.py -- 監査で要求された自動テスト。実験前に必ず通すこと。"""
import contextlib, io, subprocess, sys, json, pathlib, hashlib, tempfile, shutil, os

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
FAIL = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


print("=== 1. py_compile ===")
r = subprocess.run([sys.executable, "-m", "py_compile",
                    str(HERE / "prompts.py"), str(HERE / "run.py"),
                    str(HERE / "score.py"), str(HERE / "cohort.py")],
                   capture_output=True)
check("prompts/run/score/cohort compile", r.returncode == 0, r.stderr.decode()[:200])

import prompts as P
import run as R
from run import (RAW_SCHEMA_VERSION, load_jsonl, retrieve, sha256_text,
                 validate_dataset)
import score as S
import cohort as C

cases = load_jsonl(HERE / "dataset.jsonl")
specs = load_jsonl(HERE / "specs.jsonl")

print("\n=== 2. dataset validator ===")
errs, warns = validate_dataset(cases, specs, require_full=False)
check("pilot dataset valid", not errs, "; ".join(errs[:3]))
errs_full, _ = validate_dataset(cases, specs, require_full=True)
check("6件は full 要件を満たさない（意図通り）", bool(errs_full),
      f"{len(errs_full)} errors")

# 壊れたデータを弾けるか
bad = json.loads(json.dumps(cases[0]))
bad["ground_truth"]["refutation_document_ids"] = ["dZZ"]
e2, _ = validate_dataset([bad], None)
check("存在しない refutation doc を検出", any("実在しない" in x for x in e2))

bad2 = json.loads(json.dumps(cases[4]))   # NR-01
bad2["ground_truth"]["trap_conclusion"] = "何か"
e3, _ = validate_dataset([bad2], None)
check("no_refutation に trap があると検出", any("trap がある" in x for x in e3))

bad_specs = json.loads(json.dumps(specs))
next(s for s in bad_specs if s["id"] == "AB-01")["refutation_match"] = "ANY"
e4, _ = validate_dataset(cases, bad_specs)
check("specs と dataset の refutation_match 不一致を検出",
      any("refutation_match が不一致" in x for x in e4))

print("\n=== 3-4. pilot prompt 生成 ===")
with tempfile.TemporaryDirectory() as td:
    results_root = pathlib.Path(td) / "results"
    missing_meta = subprocess.run(
        [sys.executable, str(HERE / "run.py"), "--backend", "manual",
         "--stage", "1", "--results", str(results_root)],
        capture_output=True, text=True)
    check("manual stage1 は provider/model 欠損を拒否",
          missing_meta.returncode != 0 and "--provider と --model が必須" in missing_meta.stdout)
    r = subprocess.run(
        [sys.executable, str(HERE / "run.py"), "--backend", "manual",
         "--stage", "1", "--provider", "test-provider", "--model", "test-model",
         "--run-id", "selftest-run", "--results", str(results_root)],
        capture_output=True, text=True)
    run_dir = results_root / "runs" / "selftest-run"
    n_files = len(list((run_dir / "prompts").glob("*.txt")))
    check("prompt 生成", r.returncode == 0, r.stdout[-200:])
    check("生成件数 = 6 cases x 3 = 18", n_files == 18, f"actual={n_files}")
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    check("manifest に provider/model/settings/時刻を保存",
          manifest["provider"] == "test-provider" and manifest["model"] == "test-model"
          and "settings" in manifest and bool(manifest.get("started_at_utc")))
    check("18 prompt の SHA-256 を保存",
          len(manifest["prompts"]) == 18
          and all(len(x["sha256"]) == 64 and x.get("utf8_bytes", 0) > 0
                  for x in manifest["prompts"].values()))

print("\n=== 5. pilot モードで KEEP/DROP が絶対に出ない ===")
src = (HERE / "score.py").read_text(encoding="utf-8")
# 表示関数を実際に呼び、正式判定の行が出ないことを behavior で確認する。
pilot_agg = {c: {"accuracy": 0.5, "refutable_accuracy": 0.5,
                 "trap_rate": 0.5,
                 "decisive_refutation_citation": 0.5,
                 "fixed_refutation_retrieval_hit": (0.5 if c == "C" else None),
                 "correct_destruction": 0.0, "anchor_mismatch": 0.0,
                 "malformed": 0.0, "trap_rate_scored_n": 1,
                 "refutable_accuracy_n": 1,
                 "decisive_refutation_citation_n": 1,
                 "fixed_refutation_retrieval_hit_n": (1 if c == "C" else 0)}
             for c in ("A", "B", "C")}
pilot_recs = [dict(condition="A", type="false_coherence")]
pilot_stdout = io.StringIO()
with contextlib.redirect_stdout(pilot_stdout):
    S.print_pilot(pilot_agg, pilot_recs)
pilot_lines = [line.strip() for line in pilot_stdout.getvalue().splitlines()]
check("print_pilot は正式判定を表示しない（behavior検査）",
      not any(line.startswith("判定:") for line in pilot_lines)
      and not any(line in {"KEEP", "SUBSCRIPTION_KEEP", "DROP", "CONDITIONAL KEEP"}
                  for line in pilot_lines))
full_stdout = io.StringIO()
with contextlib.redirect_stdout(full_stdout):
    S.print_full(pilot_agg)
check("full表にAと決定的反証文書の引用率が現れる",
      "A" in full_stdout.getvalue().splitlines()[0]
      and "Decisive Refutation Citation" in full_stdout.getvalue())
# main が pilot 時に verdict_full を呼ばないこと
mainseg = src.split("def main()")[1]
pilot_branch = mainseg.split('if a.mode == "pilot":')[1].split("else:")[0]
check("pilot 分岐で verdict_full を呼ばない", "verdict_full" not in pilot_branch)
check("pilot の verdict は NOT_APPLICABLE", "NOT_APPLICABLE" in pilot_branch)

print("\n=== 6. API token / subscription workload を混同しない ===")
manual_rec = dict(condition="B", type="false_coherence", verdict="correct",
                  unsupported=False, malformed=False,
                  decisive_refutation_citation=True,
                  fixed_refutation_retrieval_hit=None,
                  calls=1, in_tok=None, out_tok=None,
                  input_utf8_bytes=1000, output_utf8_bytes=500,
                  backend="manual", trial=0, case_id="X")
check("total_tokens が None", S.total_tokens(manual_rec) is None)
check("manual workloadはUTF-8 byteで実測", S.total_utf8_bytes(manual_rec) == 1500)
agg = S.aggregate([manual_rec, dict(manual_rec, condition="C", calls=2),
                   dict(manual_rec, condition="A")])
check("avg_tokens が None", agg["B"]["avg_tokens"] is None
      and agg["C"]["avg_tokens"] is None)
check("Aもaggregateされ決定的反証文書の引用率を保持",
      agg["A"]["decisive_refutation_citation"] == 1.0
      and agg["A"]["decisive_refutation_citation_n"] == 1)

def formal_agg(avg_tokens_c=1500):
    return {"B": dict(refutable_accuracy=0.40, trap_rate=0.50, accuracy=0.50,
                      correct_destruction=0.0, malformed=0.0,
                      avg_tokens=1000, avg_utf8_bytes=1500,
                      backend="api", anchor_mismatch=0.0),
            "C": dict(refutable_accuracy=0.70, trap_rate=0.20, accuracy=0.80,
                      correct_destruction=0.0, malformed=0.0,
                      avg_tokens=avg_tokens_c, avg_utf8_bytes=2200,
                      backend="api", anchor_mismatch=0.0)}

aggx = formal_agg(avg_tokens_c=None)
aggx["B"]["avg_tokens"] = None
v, reason = S.verdict_full(aggx)
check("token欠損時に KEEP が出ない", v != "KEEP", f"verdict={v}")
check("COST_COMPARISON_UNAVAILABLE を明示", "COST_COMPARISON_UNAVAILABLE" in reason)
# token があれば KEEP が出ることも確認（判定ロジックが死んでいないこと）
aggy = formal_agg()
check("token有りかつ条件充足で KEEP", S.verdict_full(aggy)[0] == "KEEP")
check("primary label と計算が Refutable Accuracy C-B で一致",
      S.PRE_REGISTRATION["primary"] ==
      "Refutable Accuracy gain (C - B)"
      and abs(S.primary_effect(aggy) - 0.30) < 1e-12)
check("事前登録v4は性能閾値を維持し無効化条件10件を固定",
      S.PRE_REGISTRATION["version"] == 4
      and S.PRE_REGISTRATION["keep"]["refutable_accuracy_gain_min"] == 0.20
      and S.PRE_REGISTRATION["keep"]["correct_destruction_max"] == 0.25
      and S.PRE_REGISTRATION["keep"]["max_workload_ratio"] == 2.0
      and len(S.PRE_REGISTRATION["invalidation_conditions"]) == 10)

manual_good = formal_agg(avg_tokens_c=None)
for condition in ("B", "C"):
    manual_good[condition]["avg_tokens"] = None
    manual_good[condition]["backend"] = "manual"
manual_good["B"]["avg_utf8_bytes"] = 1500
manual_good["C"]["avg_utf8_bytes"] = 2250
check("APIなしmanualでも監査可能workloadでSUBSCRIPTION_KEEP",
      S.verdict_full(manual_good)[0] == "SUBSCRIPTION_KEEP")
manual_expensive = json.loads(json.dumps(manual_good))
manual_expensive["C"]["avg_utf8_bytes"] = 3000
check("manual workload比2.0以上はCONDITIONAL KEEP",
      S.verdict_full(manual_expensive)[0] == "CONDITIONAL KEEP")

calibration_ok = formal_agg()
calibration_ok["B"]["refutable_accuracy"] = 0.50
calibration_ok["B"]["refutable_accuracy_n"] = 14
check("calibrationはBの中間難易度だけでCOHORT_ACCEPT",
      S.calibration_verdict(calibration_ok, 1)[0] == "COHORT_ACCEPT")
calibration_c_changed = json.loads(json.dumps(calibration_ok))
calibration_c_changed["C"]["refutable_accuracy"] = 0.0
check("calibration cohort選択はCの成績に依存しない",
      S.calibration_verdict(calibration_c_changed, 1)
      == S.calibration_verdict(calibration_ok, 1))
calibration_c_broken = json.loads(json.dumps(calibration_ok))
calibration_c_broken["C"]["malformed"] = 0.01
check("Cの成績は選択に使わないがCの実行破損はINVALID",
      S.calibration_verdict(calibration_c_broken, 1)[0]
      == "CALIBRATION_INVALID")
calibration_ceiling = json.loads(json.dumps(calibration_ok))
calibration_ceiling["B"]["refutable_accuracy"] = 1.0
check("Bが天井ならcohort全体をREJECT",
      S.calibration_verdict(calibration_ceiling, 1)[0] == "COHORT_REJECT")
calibration_ambiguous = json.loads(json.dumps(calibration_ok))
calibration_ambiguous["B"]["correct_destruction"] = 0.50
check("Bがno_refutationを壊す曖昧cohortはREJECT",
      S.calibration_verdict(calibration_ambiguous, 1)[0] == "COHORT_REJECT")
accuracy_bad = json.loads(json.dumps(aggy)); accuracy_bad["C"]["accuracy"] = 0.4
check("Refutable Accuracy改善でも Accuracy(C)<Accuracy(B) は DROP",
      S.verdict_full(accuracy_bad)[0] == "DROP")
destruction_bad = json.loads(json.dumps(aggy))
destruction_bad["C"]["correct_destruction"] = 0.26
check("Correct Destruction閾値超過は CONDITIONAL KEEP",
      S.verdict_full(destruction_bad)[0] == "CONDITIONAL KEEP")
tokens_bad = json.loads(json.dumps(aggy)); tokens_bad["C"]["avg_tokens"] = 2000
check("token比2.0以上は CONDITIONAL KEEP",
      S.verdict_full(tokens_bad)[0] == "CONDITIONAL KEEP")
# primary閾値未満は DROP
aggz = formal_agg(avg_tokens_c=1200)
aggz["C"]["refutable_accuracy"] = 0.50
check("Refutable Accuracy C-B < 0.20 は DROP", S.verdict_full(aggz)[0] == "DROP")
# 監査H-1: trapをother/unclearへ移しただけでは成功しない。
avoidance_only = formal_agg()
avoidance_only["C"]["refutable_accuracy"] = avoidance_only["B"]["refutable_accuracy"]
avoidance_only["C"]["trap_rate"] = 0.0
avoidance_only["C"]["accuracy"] = avoidance_only["B"]["accuracy"]
check("trap→otherだけで正答増加なしは DROP",
      S.verdict_full(avoidance_only)[0] == "DROP")
trap_worse = formal_agg(); trap_worse["C"]["trap_rate"] = 0.60
check("正答率が改善してもTrap Rate悪化は DROP",
      S.verdict_full(trap_worse)[0] == "DROP")
malformed_bad = formal_agg(); malformed_bad["C"]["malformed"] = 0.70
check("Cの反証可能14行が全malformedなら INVALID_OUTPUT",
      S.verdict_full(malformed_bad)[0] == "INVALID_OUTPUT")
anchor_bad = json.loads(json.dumps(aggy)); anchor_bad["C"]["anchor_mismatch"] = 0.01
check("anchor不一致時は DROP でなく INVALID_ANCHOR",
      S.verdict_full(anchor_bad)[0] == "INVALID_ANCHOR")

print("\n=== 7. judge 順序がプロセスを跨いで同一 ===")
code = ("import sys; sys.path.insert(0,%r); import score as S; "
        "print([S.stable_flip(x) for x in "
        "['FC-01','FC-02','AB-01','CC-01','NR-01','NR-02']])" % str(HERE))
outs = set()
for _ in range(3):
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env={"PYTHONHASHSEED": "random", "PATH": "/usr/bin:/bin"})
    outs.add(p.stdout.strip())
check("PYTHONHASHSEED を変えても同一", len(outs) == 1, str(outs))
import ast
def uses_builtin_hash(path):
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "hash" for n in ast.walk(tree))
check("builtin hash() の呼び出しがない（AST検査）",
      not uses_builtin_hash(HERE / "score.py")
      and not uses_builtin_hash(HERE / "run.py"))

print("\n=== 8. C_STEP2 の誤った一文が消えているか ===")
t = P.COND_C_STEP2
check("誤った一文が削除されている",
      "見つからなかった場合、それは暫定結論を支持する情報です" not in t)
check("置換文が入っている",
      "支持する証拠ではありません" in t)

print("\n=== 9. B と C の initial 定義が同一、出力依存fieldなし ===")
b = P.build_b(cases[0]); c = P.build_c2(cases[0], "x", ["y"], [])
initialline = P._INITIAL_CONCLUSION_DEF
check("B/C に同じ initial_conclusion 定義がある",
      initialline in b and initialline in c)
check("B/C prompt は counterevidence_documents を要求しない",
      "counterevidence_documents" not in b and "counterevidence_documents" not in c)
check("prompts.py に出力依存の counterevidence 定義がない",
      not hasattr(P, "_COUNTEREVIDENCE_DEF"))
check("C2 は C1暫定結論の逐語コピーを要求",
      "一字一句変更せず" in P.COND_C_STEP2)
check("C anchor 一致", S.anchor_consistent("最初の説明", "最初の説明", "C", False) is True)
check("C anchor 不一致", S.anchor_consistent("後付け", "最初の説明", "C", False) is False)

print("\n=== 10. retrieval の弁別性 ===")
good = {'FC-01': ['原材料 単価 横ばい', '仕入 単価 前年同月', '材料費 変動なし', '仕入台帳', '原価 構成比'],
        'FC-02': ['同一賃金 他部署 退職率', '賃金テーブル 比較', '製造3課 退職', '隣接部署 退職者', '賃金 条件差'],
        'AB-01': ['定期発注 今期 未受注', '受注 登録なし', '年度初 発注 実績', '受注管理 今期', '定期 発注'],
        'CC-01': ['残業 制限 不良率', '残業 減らした 不良', '介入 試行', '上限 制限 週', '不良率 変化なし']}
bad_q = {'FC-01': ['原材料 高騰', '価格 上昇', '値上げ 理由', '仕入先 連絡', '市況'],
         'FC-02': ['賃金 低い', '待遇 不満', '退職 理由', '給料', '条件'],
         'AB-01': ['取引 良好', '訪問 対応', '関係 継続', '担当者', '入金'],
         'CC-01': ['残業 疲労', '集中力 低下', '不良 増加', '作業者 声', '疲れ']}
def _reached(refs, match, retrieved_ids):
    return (set(refs) <= retrieved_ids) if match == "ALL" else bool(set(refs) & retrieved_ids)

gh = bl = 0
for cs in cases:
    if cs["id"] not in good:
        continue
    g = cs["ground_truth"]
    refs, match = g["refutation_document_ids"], g["refutation_match"]
    top_k = max(2, len(refs))          # ALL の場合は必要件数まで取得
    gh += _reached(refs, match,
                   {d["doc_id"] for d in retrieve(good[cs["id"]], cs["documents"], top_k)})
    bl += _reached(refs, match,
                   {d["doc_id"] for d in retrieve(bad_q[cs["id"]], cs["documents"], top_k)})
check("良質 falsifier で反証到達 4/4", gh == 4, f"{gh}/4")
check("劣質 falsifier では到達しない 0/4", bl == 0, f"{bl}/4")


print("\n=== 11. primaryと記述指標の分母が出力に依存しない ===")
gt_fc02 = [c for c in cases if c["id"] == "FC-02"][0]["ground_truth"]
gt_ab01 = [c for c in cases if c["id"] == "AB-01"][0]["ground_truth"]
gt_nr01 = [c for c in cases if c["id"] == "NR-01"][0]["ground_truth"]

# initialや出力内容に関係なく、反証可能ケースは全行が同じ分母に残る。
mk = lambda cond, verdict, mal, typ, cid: dict(
    case_id=cid, type=typ, condition=cond, trial=0, malformed=mal,
    verdict=verdict, decisive_refutation_citation=(not mal),
    fixed_refutation_retrieval_hit=((not mal) if cond == "C" else None),
    unsupported=False, confidence=None, calls=1, in_tok=None, out_tok=None)
rows_t = [mk("B", "trap", False, "false_coherence", "x1"),
          mk("B", "other", False, "false_coherence", "x2"),
          mk("C", "correct", False, "false_coherence", "x1"),
          mk("C", "unclear", True, "false_coherence", "x2")]
agg_t = S.aggregate(rows_t)
check("B/C primary分母が各2行で一致",
      agg_t["B"]["refutable_accuracy_n"] == 2
      and agg_t["C"]["refutable_accuracy_n"] == 2)
check("malformed/other/unclearもprimary分母に残る",
      agg_t["B"]["refutable_accuracy"] == 0.0
      and agg_t["C"]["refutable_accuracy"] == 0.5)
check("引用率の分母も各2行で一致",
      agg_t["B"]["decisive_refutation_citation_n"] == 2
      and agg_t["C"]["decisive_refutation_citation_n"] == 2)
check("検索ヒットはC全反証可能行だけを分母にする",
      agg_t["B"]["fixed_refutation_retrieval_hit_n"] == 0
      and agg_t["C"]["fixed_refutation_retrieval_hit_n"] == 2)

ab_case = next(c for c in cases if c["id"] == "AB-01")
smoke_answer = json.dumps({
    "initial_conclusion": ab_case["ground_truth"]["correct_conclusion"],
    "conclusion": ab_case["ground_truth"]["correct_conclusion"],
    "key_documents": ["d2", "d3", "d6"],
    "confidence": "暫定"},
    ensure_ascii=False)
judge_calls = []
def smoke_judge(prompt):
    judge_calls.append(prompt)
    if "has_unsupported" in prompt:
        return {"has_unsupported": False}
    # AB-01 は stable_flip=False なので候補1がcorrect。
    return {"match": "1"}
scored_smoke = S.score([
    {"case_id": "AB-01", "condition": "C", "trial": 0,
     "raw_answer": smoke_answer,
     "provisional_answer": ab_case["ground_truth"]["correct_conclusion"],
     "retrieved_docs": ["d2", "d3"], "falsifier": [], "usage": {"calls": 2}}
], [ab_case], smoke_judge)[0]
check("実score経路で決定的反証文書の引用と検索ヒットを別集計",
      scored_smoke["decisive_refutation_citation"] is True
      and scored_smoke["fixed_refutation_retrieval_hit"] is True)
check("initial結論への追加judge呼び出しをしない（1行2call）",
      len(judge_calls) == 2, str(len(judge_calls)))

print("\n=== 12. ground truth の ANY/ALL 単独成立性（修正2）===")
gt_fc01 = [c for c in cases if c["id"] == "FC-01"][0]["ground_truth"]
check("AB-01 は d2+d3 の ALL",
      gt_ab01["refutation_document_ids"] == ["d2", "d3"]
      and gt_ab01["refutation_match"] == "ALL",
      f"{gt_ab01['refutation_document_ids']} / {gt_ab01['refutation_match']}")
check("FC-02 は d4 のみ", gt_fc02["refutation_document_ids"] == ["d4"],
      str(gt_fc02["refutation_document_ids"]))
gt_cc01 = [c for c in cases if c["id"] == "CC-01"][0]["ground_truth"]
check("CC-01 は d4 のみ（d5は単独で反証にならない）",
      gt_cc01["refutation_document_ids"] == ["d4"],
      str(gt_cc01["refutation_document_ids"]))
check("FC-01 は d4 のみ ANY（d6は原材料説を直接否定しない）",
      gt_fc01["refutation_document_ids"] == ["d4"]
      and gt_fc01["refutation_match"] == "ANY",
      f"{gt_fc01['refutation_document_ids']} / {gt_fc01['refutation_match']}")

# why_decisive に基づく意味固定。refs はcorrect側の支持文書ではなくtrap否定文書。
semantic_fixtures = {
    "FC-01": (["d4"], "直接否定"),
    "FC-02": (["d4"], "単独"),
    "AB-01": (["d2", "d3"], "2文書が揃って初めて反証"),
    "CC-01": (["d4"], "介入"),
}
for case_id, (expected_refs, why_fragment) in semantic_fixtures.items():
    dataset_case = next(c for c in cases if c["id"] == case_id)
    spec_case = next(s for s in specs if s["id"] == case_id)
    check(f"{case_id} 意味fixture: trap否定refs/why_decisive一致",
          dataset_case["ground_truth"]["refutation_document_ids"] == expected_refs
          and why_fragment in dataset_case["ground_truth"]["why_decisive"]
          and why_fragment in spec_case["why_decisive"])

# --- AB-01: ALL 判定 ---
check("AB-01 d2のみ引用 -> False", S.required_docs_found(["d2"], gt_ab01) is False)
check("AB-01 d3のみ引用 -> False", S.required_docs_found(["d3"], gt_ab01) is False)
check("AB-01 d2+d3 引用 -> True",
      S.required_docs_found(["d2", "d3"], gt_ab01) is True)
check("AB-01 d2+d3+無関係 引用 -> True",
      S.required_docs_found(["d1", "d2", "d3"], gt_ab01) is True)

# --- FC-01: ANY 判定 ---
check("FC-01 d6のみ引用 -> False", S.required_docs_found(["d6"], gt_fc01) is False)
check("FC-01 d4 引用 -> True", S.required_docs_found(["d4"], gt_fc01) is True)
check("FC-01 d4+d6 引用 -> True",
      S.required_docs_found(["d4", "d6"], gt_fc01) is True)

check("FC-02 で d3 のみ引用 -> False",
      S.required_docs_found(["d3"], gt_fc02) is False)
check("CC-01 で d5 のみ引用 -> False",
      S.required_docs_found(["d5"], gt_cc01) is False)

print("\n=== 13. raw result completeness（修正3）===")
def _raw_row(case_id, cond, trial):
    raw_answer = "{}"
    calls = 2 if cond == "C" else 1
    responses = [{"name": f"r{i}", "sha256": sha256_text("step1"),
                  "utf8_bytes": len("step1".encode("utf-8")),
                  "collected_at_utc": "2026-08-14T00:00:00Z"}
                 for i in range(calls)]
    responses[-1]["sha256"] = sha256_text(raw_answer)
    responses[-1]["utf8_bytes"] = len(raw_answer.encode("utf-8"))
    prompts = [{"name": f"p{i}", "sha256": "a" * 64,
                "utf8_bytes": 1,
                "generated_at_utc": "2026-08-14T00:00:00Z"}
               for i in range(calls)]
    provenance = dict(
        schema_version=RAW_SCHEMA_VERSION, run_id="selftest", backend="manual",
        provider="test", model="test",
        settings={"temperature": None, "model_verification": "unverifiable_manual"},
        dataset_sha256="b" * 64, specs_sha256="c" * 64,
        prompts=prompts, responses=responses,
        assembled_at_utc="2026-08-14T00:00:00Z")
    return dict(case_id=case_id, condition=cond, trial=trial,
                raw_answer=raw_answer,
                usage={"calls": calls,
                       "input_utf8_bytes": calls,
                       "output_utf8_bytes": sum(r["utf8_bytes"] for r in responses)},
                provenance=provenance)


full_rows = [_raw_row(c["id"], cond, t)
             for c in cases for cond in ("A", "B", "C") for t in (0, 1)]
e, nt = S.validate_results(full_rows, cases)
check("完全な結果は通る", not e, "; ".join(e[:2]))
check("trial数を検出", nt == 2, str(nt))
e2, _ = S.validate_results(full_rows[:-1], cases)
check("1行削ると reject", bool(e2), f"{len(e2)} errors")
dup = full_rows + [full_rows[0]]
e3, _ = S.validate_results(dup, cases)
check("(case,condition,trial) 重複を reject",
      any("重複行" in x for x in e3))
uneven = [r for r in full_rows if not (r["case_id"] == cases[0]["id"]
                                       and r["condition"] == "B" and r["trial"] == 1)]
e4, _ = S.validate_results(uneven, cases)
check("条件間で trial数が揃わないと reject", bool(e4))
# 分母不足時に verdict が INCOMPLETE_RESULTS
aggn = formal_agg(avg_tokens_c=1200)
aggn["B"]["refutable_accuracy_n"] = 10
aggn["C"]["refutable_accuracy_n"] = 14
v_, r_ = S.verdict_full(aggn, n_trials=1)
check("分母不一致で INCOMPLETE_RESULTS", v_ == "INCOMPLETE_RESULTS", v_)
aggn["B"]["refutable_accuracy_n"] = 14
check("primary分母14×trialsが揃えば完全性gateを通る",
      S.verdict_full(aggn, n_trials=1)[0] == "KEEP")

e0, nt0 = S.validate_results([], cases)
check("結果0件を reject", bool(e0) and nt0 == 0, str(e0))

no_prov = dict(full_rows[0]); no_prov.pop("provenance")
ep, _ = S.validate_results([no_prov] + full_rows[1:], cases)
check("provenance 欠損を reject", any("provenance 欠損" in x for x in ep))
tampered = json.loads(json.dumps(full_rows))
tampered[0]["raw_answer"] = '{"tampered":true}'
et, _ = S.validate_results(tampered, cases)
check("raw本文と response SHA 不一致を reject",
      any("raw_answer と response SHA-256 が不一致" in x for x in et))
mixed = json.loads(json.dumps(full_rows))
mixed[0]["provenance"]["model"] = "other-model"
em, _ = S.validate_results(mixed, cases)
check("異なるmodelの混在を reject", any("異なる実行条件" in x for x in em))
empty_calls = json.loads(json.dumps(full_rows))
empty_calls[0]["usage"]["calls"] = 0
empty_calls[0]["provenance"]["prompts"] = []
empty_calls[0]["provenance"]["responses"] = []
ec, _ = S.validate_results(empty_calls, cases)
check("空のcall証跡を例外にせず reject",
      any("call証跡件数" in x for x in ec)
      and any("usage.calls" in x for x in ec))
bad_trial = json.loads(json.dumps(full_rows))
bad_trial[0]["trial"] = "0"
ebt, _ = S.validate_results(bad_trial, cases)
check("非整数trialを例外にせず reject",
      any("trial は0以上の整数" in x for x in ebt))
bad_digest = json.loads(json.dumps(full_rows))
bad_digest[0]["provenance"]["responses"][0]["sha256"] = "z" * 64
ebd, _ = S.validate_results(bad_digest, cases)
check("非hex SHA-256を reject", any("response SHA-256 不正" in x for x in ebd))

bad_usage_bytes = json.loads(json.dumps(full_rows))
bad_usage_bytes[0]["usage"]["input_utf8_bytes"] += 1
ebu, _ = S.validate_results(bad_usage_bytes, cases)
check("usageとpromptのUTF-8 byte不一致を reject",
      any("prompt UTF-8 byte合計がusageと不一致" in x for x in ebu))
bad_record_bytes = json.loads(json.dumps(full_rows))
bad_record_bytes[0]["provenance"]["responses"][0]["utf8_bytes"] = 0
ebr, _ = S.validate_results(bad_record_bytes, cases)
check("response UTF-8 byte証跡の欠損・ゼロを reject",
      any("response UTF-8 byte証跡が不正" in x for x in ebr))

# H-3: 値が存在するだけでは足りず、manifest/実ファイル由来の期待値と一致が必要。
forged = json.loads(json.dumps(full_rows))
for row in forged:
    row["provenance"]["dataset_sha256"] = "d" * 64
expected_context = {
    "run_id": "selftest", "backend": "manual", "provider": "test", "model": "test",
    "settings": {"temperature": None, "model_verification": "unverifiable_manual"},
    "dataset_sha256": R.sha256_file(HERE / "dataset.jsonl"),
    "specs_sha256": R.sha256_file(HERE / "specs.jsonl")}
ef, _ = S.validate_results(forged, cases, expected_context)
check("自己申告dataset SHAの捏造を実ファイル照合で reject（H-3）",
      any("dataset_sha256 がmanifest/実ファイルと不一致" in x for x in ef))

# M-3: APIが報告した実モデルと正常終了理由をhard gateにする。
api_rows = json.loads(json.dumps(full_rows))
for row in api_rows:
    provenance = row["provenance"]
    provenance["backend"] = "api"
    provenance["settings"]["model_verification"] = "provider_reported"
    calls = row["usage"]["calls"]
    records = []
    for i in range(calls):
        records.append({
            "prompt_sha256": "a" * 64,
            "response_sha256": (sha256_text(row["raw_answer"])
                                if i == calls - 1 else sha256_text("step1")),
            "prompt_utf8_bytes": 1,
            "response_utf8_bytes": (len(row["raw_answer"].encode("utf-8"))
                                     if i == calls - 1 else len(b"step1")),
            "started_at_utc": "2026-08-14T00:00:00Z",
            "completed_at_utc": "2026-08-14T00:00:01Z",
            "requested_model": "test", "reported_model": "test",
            "stop_reason": "end_turn"})
    provenance["calls"] = records
    provenance.pop("responses"); provenance.pop("prompts")
api_ok, _ = S.validate_results(api_rows, cases)
check("正常なAPI model/stop_reason証跡は通る", not api_ok, "; ".join(api_ok[:2]))
api_wrong_model = json.loads(json.dumps(api_rows))
api_wrong_model[0]["provenance"]["calls"][0]["reported_model"] = "other"
eam, _ = S.validate_results(api_wrong_model, cases)
check("reported_model不一致を reject", any("reported_model" in x for x in eam))
api_truncated = json.loads(json.dumps(api_rows))
api_truncated[0]["provenance"]["calls"][0]["stop_reason"] = "max_tokens"
eat, _ = S.validate_results(api_truncated, cases)
check("max_tokens終了を reject", any("正常完了していない" in x for x in eat))

# pilot CLI も、judge API を呼ぶ前に欠損結果を拒否すること
with tempfile.TemporaryDirectory() as td:
    raw = pathlib.Path(td) / "raw.jsonl"
    raw.write_text(json.dumps(dict(case_id="AB-01", condition="C", trial=0,
                                   raw_answer="{}", usage={"calls": 2}),
                              ensure_ascii=False) + "\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(HERE / "score.py"), "--mode", "pilot",
                        "--raw", str(raw)], capture_output=True, text=True)
    check("pilot CLI も欠損結果を採点前に reject",
          p.returncode != 0 and "[INCOMPLETE_RESULTS]" in p.stdout,
          f"exit={p.returncode}")

# ALL ケースの記述的検索監査は、必要文書がすべて揃うまでヒットにしない。
diag_base = dict(case_id="AB-01", condition="C", verdict="other")
d_one = S.diagnose_c([dict(diag_base, retrieved=["d2"])], cases)[0]
d_all = S.diagnose_c([dict(diag_base, retrieved=["d2", "d3"])], cases)[0]
check("AB-01 は d2 のみでは固定反証文書の検索ヒットにならない",
      d_one["reached"] is False)
check("AB-01 は d2+d3 で検索到達", d_all["reached"] is True)
check("工程の因果推定フィールドを出さない",
      set(d_all) == {"case_id", "reached", "correct"})

print("\n=== 14. no_refutation 専用 judge（修正4）===")
nrp = P.build_judge_nr("工期遅延の主因は長雨である", "長雨が原因です")
check("matches_reference 形式", "matches_reference" in nrp)
check("二択候補方式を使っていない",
      "候補1" not in nrp and "候補2" not in nrp)
check("メタ候補文が消えている",
      "上記とは異なる原因を主因として挙げている" not in (HERE / "score.py").read_text(encoding="utf-8"))
check("NR判定で build_judge_nr を使う",
      "build_judge_nr" in (HERE / "score.py").read_text(encoding="utf-8"))

print("\n=== 15. manual run 証跡の end-to-end ===")
with tempfile.TemporaryDirectory() as td:
    results_root = pathlib.Path(td) / "results"
    common = [sys.executable, str(HERE / "run.py"), "--backend", "manual",
              "--results", str(results_root), "--run-id", "protocol-run"]
    p1 = subprocess.run(
        common + ["--stage", "1", "--provider", "test-provider",
                  "--model", "test-model"], capture_output=True, text=True)
    run_dir = results_root / "runs" / "protocol-run"
    response_dir = run_dir / "responses"
    check("stage1 成功", p1.returncode == 0, p1.stdout[-200:])

    initials = {case["id"]: f"初期説明 {case['id']}" for case in cases}
    for case in cases:
        c1 = {"provisional_conclusion": initials[case["id"]],
              "falsifiers": ["反対事実1", "反対事実2", "反対事実3"],
              "queries": ["該当なし1", "該当なし2", "該当なし3", "該当なし4", "該当なし5"]}
        (response_dir / f"{case['id']}__C1.txt").write_text(
            json.dumps(c1, ensure_ascii=False), encoding="utf-8")
    p2 = subprocess.run(common + ["--stage", "2"], capture_output=True, text=True)
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    check("stage2 が6件のC2を生成", p2.returncode == 0
          and len(manifest["c2_derivations"]) == 6, p2.stdout[-200:])
    check("C2 derivation にC1 response SHAと検索結果を保存",
          all(len(x["c1_response"]["sha256"]) == 64
              and isinstance(x["retrieved_docs"], list)
              for x in manifest["c2_derivations"].values()))

    # H-2: C1変更後のstage2再実行は、古いC2応答を新promptへ流用させない。
    attacked = cases[0]["id"]
    old_c2 = {"initial_conclusion": initials[attacked], "conclusion": "古い応答",
              "key_documents": [], "confidence": "保留"}
    (response_dir / f"{attacked}__C2.txt").write_text(
        json.dumps(old_c2, ensure_ascii=False), encoding="utf-8")
    initials[attacked] = f"変更後の初期説明 {attacked}"
    changed_c1 = {"provisional_conclusion": initials[attacked],
                  "falsifiers": ["変更1", "変更2", "変更3"],
                  "queries": ["変更1", "変更2", "変更3", "変更4", "変更5"]}
    (response_dir / f"{attacked}__C1.txt").write_text(
        json.dumps(changed_c1, ensure_ascii=False), encoding="utf-8")
    p2_changed = subprocess.run(common + ["--stage", "2"], capture_output=True, text=True)
    invalidated = list((response_dir / "invalidated").glob(f"{attacked}__C2__*.txt"))
    changed_manifest = json.loads(
        (run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    check("C1変更時は古いC2応答を退避し新promptへ束縛しない",
          p2_changed.returncode == 0 and len(invalidated) == 1
          and not (response_dir / f"{attacked}__C2.txt").exists()
          and changed_manifest["c2_derivations"][attacked]["provisional_answer"]
          == initials[attacked])

    incomplete = subprocess.run(common + ["--stage", "3"],
                                capture_output=True, text=True)
    check("stage3 は一部応答だけでは raw を作らない",
          incomplete.returncode != 0 and not (run_dir / "raw.jsonl").exists()
          and "応答不足" in incomplete.stdout)

    for case in cases:
        a = {"conclusion": "テスト結論", "key_documents": [], "confidence": "保留"}
        b = {"initial_conclusion": f"初期説明 {case['id']}",
             "conclusion": "テスト結論", "key_documents": [],
             "confidence": "保留"}
        c2 = dict(b, initial_conclusion=initials[case["id"]])
        (response_dir / f"{case['id']}__A.txt").write_text(
            json.dumps(a, ensure_ascii=False), encoding="utf-8")
        (response_dir / f"{case['id']}__B.txt").write_text(
            json.dumps(b, ensure_ascii=False), encoding="utf-8")
        (response_dir / f"{case['id']}__C2.txt").write_text(
            json.dumps(c2, ensure_ascii=False), encoding="utf-8")

    p3 = subprocess.run(common + ["--stage", "3"], capture_output=True, text=True)
    raw_rows = load_jsonl(run_dir / "raw.jsonl") if (run_dir / "raw.jsonl").exists() else []
    raw_errors, raw_trials = S.validate_results(raw_rows, cases)
    check("stage3 が完全な18行だけを生成", p3.returncode == 0
          and len(raw_rows) == 18 and raw_trials == 1, p3.stdout[-200:])
    check("生成rawのprovenanceが採点hard gateを通る",
          not raw_errors, "; ".join(raw_errors[:3]))
    check("manual rawは全18行に実測UTF-8 workloadを保存",
          len(raw_rows) == 18
          and all(row["usage"].get("input_utf8_bytes", 0) > 0
                  and row["usage"].get("output_utf8_bytes", 0) > 0
                  for row in raw_rows)
          and all(len(row["provenance"]["prompts"])
                  == row["usage"]["calls"] for row in raw_rows))
    final_manifest = json.loads(
        (run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    check("manifest は complete / raw SHA・行数を保存",
          final_manifest["status"] == "complete"
          and final_manifest["raw"]["rows"] == 18
          and len(final_manifest["raw"]["sha256"]) == 64)
    bundle_errors, _ = S.validate_run_bundle(
        run_dir / "raw.jsonl", results_root, HERE / "dataset.jsonl", HERE / "specs.jsonl")
    ledger = json.loads((results_root / "run_attestations.json").read_text(encoding="utf-8"))
    check("raw/manifest/実入力/attestation bundle が一致",
          not bundle_errors and len(ledger["entries"]) == 1,
          "; ".join(bundle_errors[:2]))

    print("\n=== 16. API課金なし manual judge の end-to-end ===")
    # anthropic がimportされた瞬間に失敗する偽SDKを置き、manual経路がAPI実装を
    # 読み込まないことをbehaviorで確認する。
    fake_sdk = pathlib.Path(td) / "fake-sdk"
    fake_sdk.mkdir()
    (fake_sdk / "anthropic.py").write_text(
        'raise RuntimeError("anthropic must not be imported in manual judge")\n',
        encoding="utf-8")
    manual_export_cmd = [
        sys.executable, str(HERE / "score.py"), "--mode", "pilot",
        "--results", str(results_root), "--run-id", "protocol-run",
        "--judge", "manual", "--manual-stage", "export",
        "--judge-provider", "test-subscription-ui",
        "--judge-model", "operator-declared-test-model"]
    manual_score_cmd = [
        sys.executable, str(HERE / "score.py"), "--mode", "pilot",
        "--results", str(results_root), "--run-id", "protocol-run",
        "--judge", "manual", "--manual-stage", "score"]
    manual_env = dict(os.environ)
    manual_env["PYTHONPATH"] = str(fake_sdk)
    default_judge = subprocess.run(
        [sys.executable, str(HERE / "score.py"), "--mode", "pilot",
         "--results", str(results_root), "--run-id", "protocol-run"],
        capture_output=True, text=True, env=manual_env)
    check("judge既定値はmanualで、明示なしにAPIへ到達しない",
          default_judge.returncode != 0
          and "--judge manual は --manual-stage" in default_judge.stdout
          and "anthropic must not be imported" not in
          default_judge.stdout + default_judge.stderr)
    exported = subprocess.run(
        manual_export_cmd, capture_output=True, text=True, env=manual_env)
    judge_dir = run_dir / "manual_judge"
    judge_manifest_path = judge_dir / "manifest.json"
    judge_manifest = (json.loads(judge_manifest_path.read_text(encoding="utf-8"))
                      if judge_manifest_path.exists() else {})
    check("manual export はanthropicをimportせず成功",
          exported.returncode == 0 and "manual judge packet" in exported.stdout
          and "anthropic must not be imported" not in exported.stdout + exported.stderr,
          exported.stdout[-240:] + exported.stderr[-120:])
    check("manual packet は18行×2=36 callをSHA付きpromptへ束縛",
          judge_manifest.get("call_count") == 36
          and judge_manifest.get("unique_prompt_count") == len(
              judge_manifest.get("prompts", []))
          and all(len(x.get("prompt_sha256", "")) == 64
                  for x in judge_manifest.get("prompts", [])))
    check("manual judge provenance はsubscription_ui/API未使用/検証不能を明示",
          judge_manifest.get("judge") == {
              "route": "subscription_ui", "api_used": False,
              "provider": "test-subscription-ui",
              "model": "operator-declared-test-model",
              "model_verification": "unverifiable_manual"})

    missing_judge = subprocess.run(
        manual_score_cmd, capture_output=True, text=True, env=manual_env)
    check("judge応答欠損はAPIへfallbackせずINCOMPLETE_JUDGMENTS",
          missing_judge.returncode != 0
          and "[INCOMPLETE_JUDGMENTS]" in missing_judge.stdout
          and "judge応答が未回収" in missing_judge.stdout
          and "anthropic must not be imported" not in missing_judge.stdout + missing_judge.stderr)

    response_dir_manual = judge_dir / "responses"
    first = judge_manifest["prompts"][0]
    (response_dir_manual / f"{first['request_id']}.txt").write_text(
        '{"broken":true}', encoding="utf-8")
    malformed_judge = subprocess.run(
        manual_score_cmd, capture_output=True, text=True, env=manual_env)
    check("形式不正judge応答をfail-closedで拒否",
          malformed_judge.returncode != 0
          and "judge応答不正" in malformed_judge.stdout
          and "Traceback" not in malformed_judge.stdout + malformed_judge.stderr)

    for record in judge_manifest["prompts"]:
        if record["kind"] == "match":
            value = {"match": "unclear", "reason": "selftest"}
        elif record["kind"] == "reference":
            value = {"matches_reference": False, "reason": "selftest"}
        else:
            value = {"has_unsupported": False, "items": [], "reason": "selftest"}
        (response_dir_manual / f"{record['request_id']}.txt").write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8")
    scored_manual = subprocess.run(
        manual_score_cmd, capture_output=True, text=True, env=manual_env)
    scored_path = run_dir / "scored_pilot.json"
    scored_value = (json.loads(scored_path.read_text(encoding="utf-8"))
                    if scored_path.exists() else {})
    check("全judge応答回収後はAPIなしでpilot採点完了",
          scored_manual.returncode == 0
          and scored_value.get("verdict") == "NOT_APPLICABLE"
          and scored_value.get("judge_provenance", {}).get("api_used") is False,
          scored_manual.stdout[-240:] + scored_manual.stderr[-120:])
    completed_judge_manifest = json.loads(
        judge_manifest_path.read_text(encoding="utf-8"))
    completed_ledger = json.loads(
        (results_root / "run_attestations.json").read_text(encoding="utf-8"))
    manual_attestation = completed_ledger["entries"][0].get("manual_judge", {})
    check("採点成功後だけmanifestをcompleteにして全response SHAを固定",
          completed_judge_manifest.get("status") == "complete"
          and bool(completed_judge_manifest.get("completed_at_utc"))
          and len(completed_judge_manifest.get("responses", []))
          == completed_judge_manifest.get("unique_prompt_count")
          and scored_value.get("judge_provenance", {}).get("status") == "complete"
          and len(scored_value.get("judge_provenance", {}).get(
              "manifest_sha256", "")) == 64)
    check("manual judge manifest SHAをGit追跡対象run台帳へ外部アンカー化",
          manual_attestation.get("manifest_sha256")
          == R.sha256_file(judge_manifest_path)
          == scored_value.get("judge_provenance", {}).get("manifest_sha256")
          and manual_attestation.get("response_count")
          == completed_judge_manifest.get("unique_prompt_count")
          and scored_value.get("judge_provenance", {}).get("ledger_attested") is True)

    first_response = response_dir_manual / f"{first['request_id']}.txt"
    original_response = first_response.read_text(encoding="utf-8")
    first_response.chmod(0o644)
    first_response.write_text(original_response + " ", encoding="utf-8")
    tampered_response = subprocess.run(
        manual_score_cmd, capture_output=True, text=True, env=manual_env)
    check("採点完了後のjudge応答改ざんを保存済みSHAで拒否",
          tampered_response.returncode != 0
          and "完了後のjudge応答SHAが不一致" in tampered_response.stdout)
    first_response.write_text(original_response, encoding="utf-8")
    first_response.chmod(0o444)

    original_manifest_text = judge_manifest_path.read_text(encoding="utf-8")
    changed_manifest = json.loads(original_manifest_text)
    changed_manifest["completed_at_utc"] = "2099-01-01T00:00:00Z"
    judge_manifest_path.chmod(0o644)
    judge_manifest_path.write_text(
        json.dumps(changed_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    tampered_manifest = subprocess.run(
        manual_score_cmd, capture_output=True, text=True, env=manual_env)
    check("manifestと応答を自己整合させても追跡対象台帳SHA不一致で拒否",
          tampered_manifest.returncode != 0
          and "台帳のmanual judge attestationが実体と不一致"
          in tampered_manifest.stdout)
    judge_manifest_path.write_text(original_manifest_text, encoding="utf-8")
    judge_manifest_path.chmod(0o444)

    first_prompt = judge_dir / first["prompt_file"]
    original_prompt = first_prompt.read_text(encoding="utf-8")
    first_prompt.chmod(0o644)
    first_prompt.write_text(original_prompt + "\ntampered", encoding="utf-8")
    tampered_judge = subprocess.run(
        manual_score_cmd, capture_output=True, text=True, env=manual_env)
    check("judge prompt改ざんはSHA不一致で拒否",
          tampered_judge.returncode != 0
          and "prompt SHA-256が不一致" in tampered_judge.stdout)
    first_prompt.write_text(original_prompt, encoding="utf-8")
    first_prompt.chmod(0o444)

    def refresh_test_bundle(root):
        """改変検知より先のエラー経路を試すため、テスト用bundleだけ再署名する。"""
        copied_run = root / "runs" / "protocol-run"
        copied_manifest_path = copied_run / "run_manifest.json"
        copied_manifest_path.chmod(0o644)
        copied_manifest = json.loads(
            copied_manifest_path.read_text(encoding="utf-8"))
        copied_manifest["raw"]["sha256"] = R.sha256_file(copied_run / "raw.jsonl")
        copied_manifest_path.write_text(
            json.dumps(copied_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        copied_ledger_path = root / "run_attestations.json"
        copied_ledger = json.loads(copied_ledger_path.read_text(encoding="utf-8"))
        copied_entry = copied_ledger["entries"][0]
        copied_entry["raw_sha256"] = copied_manifest["raw"]["sha256"]
        copied_entry["manifest_sha256"] = R.sha256_file(copied_manifest_path)
        copied_ledger_path.write_text(
            json.dumps(copied_ledger, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    def score_cli(root, run_id="protocol-run"):
        return subprocess.run(
            [sys.executable, str(HERE / "score.py"), "--mode", "pilot",
             "--results", str(root), "--run-id", run_id],
            capture_output=True, text=True)

    # M-1: ファイル単位の破損・不在・不正IDも traceback ではなくfail-closedにする。
    broken_root = pathlib.Path(td) / "broken-json-results"
    shutil.copytree(results_root, broken_root)
    broken_raw = broken_root / "runs" / "protocol-run" / "raw.jsonl"
    broken_raw.chmod(0o644)
    broken_raw.write_text('{"broken":\n', encoding="utf-8")
    refresh_test_bundle(broken_root)
    broken_cli = score_cli(broken_root)
    check("壊れたrawはtracebackなしでINCOMPLETE_RESULTS（M-1）",
          broken_cli.returncode != 0
          and "[INCOMPLETE_RESULTS]" in broken_cli.stdout
          and "Traceback" not in broken_cli.stdout + broken_cli.stderr)

    missing_cli = score_cli(results_root, "missing-run")
    check("不在runはtracebackなしでINCOMPLETE_RESULTS（M-1）",
          missing_cli.returncode != 0
          and "[INCOMPLETE_RESULTS]" in missing_cli.stdout
          and "Traceback" not in missing_cli.stdout + missing_cli.stderr)

    traversal_cli = score_cli(results_root, "../../etc")
    check("不正run_idはtracebackなしでINCOMPLETE_RESULTS（M-1）",
          traversal_cli.returncode != 0
          and "[INCOMPLETE_RESULTS]" in traversal_cli.stdout
          and "Traceback" not in traversal_cli.stdout + traversal_cli.stderr)

    # H-3: raw/manifest/台帳を自己整合させても、行内の捏造SHAは実入力照合で拒否する。
    forged_root = pathlib.Path(td) / "forged-results"
    shutil.copytree(results_root, forged_root)
    forged_raw = forged_root / "runs" / "protocol-run" / "raw.jsonl"
    forged_raw.chmod(0o644)
    forged_rows = load_jsonl(forged_raw)
    for row in forged_rows:
        row["provenance"]["dataset_sha256"] = "d" * 64
    forged_raw.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in forged_rows),
        encoding="utf-8")
    refresh_test_bundle(forged_root)
    forged_cli = score_cli(forged_root)
    check("自己整合した捏造rawも実dataset SHA照合で拒否（H-3 CLI）",
          forged_cli.returncode != 0
          and "dataset_sha256 がmanifest/実ファイルと不一致" in forged_cli.stdout
          and "Traceback" not in forged_cli.stdout + forged_cli.stderr,
          forged_cli.stdout[-240:])

    raw_sha_before = final_manifest["raw"]["sha256"]
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.chmod(0o644)
    rolled_back = json.loads(manifest_path.read_text(encoding="utf-8"))
    rolled_back["status"] = "awaiting_stage2_responses"
    manifest_path.write_text(json.dumps(rolled_back), encoding="utf-8")
    (response_dir / f"{cases[0]['id']}__B.txt").write_text('{"tampered":true}', encoding="utf-8")
    rerun = subprocess.run(common + ["--stage", "3"], capture_output=True, text=True)
    check("manifest status巻き戻し後もraw存在で上書きを拒否（H-1）",
          rerun.returncode != 0 and "raw.jsonl が存在するため不変" in rerun.stdout
          and sha256_text((run_dir / "raw.jsonl").read_text(encoding="utf-8"))
          == raw_sha_before)

print("\n=== 17. 未観測60ケースの事前分割 ===")
templates = {
    "false_coherence": next(c for c in cases if c["type"] == "false_coherence"),
    "absence": next(c for c in cases if c["type"] == "absence"),
    "common_cause": next(c for c in cases if c["type"] == "common_cause"),
    "no_refutation": next(c for c in cases if c["type"] == "no_refutation"),
}
spec_by_id = {spec["id"]: spec for spec in specs}
pool_cases, pool_specs = [], []
prefixes = {"false_coherence": "V4-FC", "absence": "V4-AB",
            "common_cause": "V4-CC", "no_refutation": "V4-NR"}
for case_type, count in C.POOL_COMPOSITION.items():
    template = templates[case_type]
    for index in range(count):
        case = json.loads(json.dumps(template))
        case["id"] = f"{prefixes[case_type]}-{index + 1:02d}"
        doc_ids = [doc["doc_id"] for doc in case["documents"]]
        refs = set(case["ground_truth"].get("refutation_document_ids") or [])
        if case_type == "no_refutation":
            case["design"] = {
                "plausible_alternative_document_ids": doc_ids[-2:],
                "why_alternatives_fail": "各代替説明は同じ文書内の記録で否定される"}
        else:
            non_refs = [doc_id for doc_id in doc_ids if doc_id not in refs]
            case["design"] = {
                "trap_support_document_ids": non_refs[:2],
                "correct_support_document_ids": list(refs)}
        pool_cases.append(case)
        spec = json.loads(json.dumps(spec_by_id[template["id"]]))
        spec["id"] = case["id"]
        pool_specs.append(spec)

pool_errors = C.validate_pool(pool_cases, pool_specs)
check("60件 18/12/12/18 の構造poolを受理", not pool_errors,
      "; ".join(pool_errors[:2]))
split_cases, split_specs, assignments = C.split_pool(
    pool_cases, pool_specs, "counterevidence-v4-cohort-01")
check("calibration/formal/reserveが各20件・6/4/4/6",
      all(len(split_cases[name]) == 20
          and {typ: sum(c["type"] == typ for c in split_cases[name])
               for typ in C.SPLIT_COMPOSITION} == C.SPLIT_COMPOSITION
          for name in C.SPLITS))
split_ids = [{case["id"] for case in split_cases[name]} for name in C.SPLITS]
check("3 splitは重複なしで全60件を一度ずつ使う",
      not (split_ids[0] & split_ids[1] or split_ids[0] & split_ids[2]
           or split_ids[1] & split_ids[2])
      and len(set().union(*split_ids)) == 60)
_, _, reversed_assignments = C.split_pool(
    list(reversed(pool_cases)), list(reversed(pool_specs)),
    "counterevidence-v4-cohort-01")
check("入力順を変えてもsplit assignmentは不変",
      assignments == reversed_assignments)

reused = json.loads(json.dumps(pool_cases))
reused_specs = json.loads(json.dumps(pool_specs))
reused_specs[0]["id"] = reused[0]["id"] = "FC-01"
check("2026-08-15までのpilot ID再利用をreject",
      any("pilot ID" in error for error in C.validate_pool(reused, reused_specs)))
overlap_support = json.loads(json.dumps(pool_cases))
first_refutable = next(c for c in overlap_support if c["type"] != "no_refutation")
first_refutable["design"]["trap_support_document_ids"][0] = \
    first_refutable["ground_truth"]["refutation_document_ids"][0]
check("trap支持文書と決定的反証文書の重複をreject",
      any("重複" in error for error in C.validate_pool(overlap_support, pool_specs)))

with tempfile.TemporaryDirectory() as td:
    td_path = pathlib.Path(td)
    pool_path, pool_specs_path = td_path / "pool.jsonl", td_path / "specs.jsonl"
    C.write_jsonl(pool_path, pool_cases)
    C.write_jsonl(pool_specs_path, pool_specs)
    frozen_dir = td_path / "frozen"
    cohort_manifest = C.freeze_pool(
        pool_path, pool_specs_path, frozen_dir, "counterevidence-v4-cohort-01")
    check("freezeは6 splitファイルとSHA付きmanifestを生成",
          len(cohort_manifest["files"]) == 6
          and all((frozen_dir / name).is_file()
                  and C.sha256_file(frozen_dir / name) == digest
                  for name, digest in cohort_manifest["files"].items()))
    try:
        C.freeze_pool(pool_path, pool_specs_path, frozen_dir,
                      "counterevidence-v4-cohort-01")
        overwrite_rejected = False
    except ValueError:
        overwrite_rejected = True
    check("凍結済みcohortの上書きをreject", overwrite_rejected)

print("\n" + "=" * 60)
if FAIL:
    print(f"FAILED: {len(FAIL)}")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS")
