#!/usr/bin/env python3
"""selftest.py -- 監査で要求された自動テスト。実験前に必ず通すこと。"""
import subprocess, sys, json, pathlib, hashlib

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
                    str(HERE / "score.py")], capture_output=True)
check("prompts/run/score compile", r.returncode == 0, r.stderr.decode()[:200])

import prompts as P
from run import load_jsonl, validate_dataset, retrieve
import score as S

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

print("\n=== 3-4. pilot prompt 生成 ===")
r = subprocess.run([sys.executable, str(HERE / "run.py"),
                    "--backend", "manual", "--stage", "1"],
                   capture_output=True, text=True)
n_files = len(list((HERE / "results" / "prompts").glob("*.txt")))
check("prompt 生成", r.returncode == 0)
check(f"生成件数 = 6 cases x 3 = 18", n_files == 18, f"actual={n_files}")

print("\n=== 5. pilot モードで KEEP/DROP が絶対に出ない ===")
src = (HERE / "score.py").read_text(encoding="utf-8")
# print_pilot 関数の本文に KEEP/DROP 文字列が現れないこと
seg = src.split("def print_pilot")[1].split("\ndef ")[0]
check("print_pilot に KEEP/DROP 出力なし",
      "KEEP" not in seg.replace("KEEP/DROP 判定は出しません", "")
      .replace("KEEP / DROP してはならない", ""))
# main が pilot 時に verdict_full を呼ばないこと
mainseg = src.split("def main()")[1]
pilot_branch = mainseg.split('if a.mode == "pilot":')[1].split("else:")[0]
check("pilot 分岐で verdict_full を呼ばない", "verdict_full" not in pilot_branch)
check("pilot の verdict は NOT_APPLICABLE", "NOT_APPLICABLE" in pilot_branch)

print("\n=== 6. token 欠損が 0 扱いされない ===")
manual_rec = dict(condition="B", type="false_coherence", verdict="correct",
                  refutation_found=True, unsupported=False, malformed=False,
                  calls=1, in_tok=None, out_tok=None, trial=0, case_id="X")
check("total_tokens が None", S.total_tokens(manual_rec) is None)
agg = S.aggregate([manual_rec, dict(manual_rec, condition="C", calls=2)])
check("avg_tokens が None", agg["B"]["avg_tokens"] is None
      and agg["C"]["avg_tokens"] is None)
# discovery 差を十分大きくして、token欠損だけで KEEP が出ないことを見る
aggx = {"B": dict(refutation_discovery=0.20, accuracy=0.5,
                  correct_destruction=0.0, avg_tokens=None),
        "C": dict(refutation_discovery=0.80, accuracy=0.9,
                  correct_destruction=0.0, avg_tokens=None)}
v, reason = S.verdict_full(aggx)
check("token欠損時に KEEP が出ない", v != "KEEP", f"verdict={v}")
check("COST_COMPARISON_UNAVAILABLE を明示", "COST_COMPARISON_UNAVAILABLE" in reason)
# token があれば KEEP が出ることも確認（判定ロジックが死んでいないこと）
aggy = {"B": dict(refutation_discovery=0.20, accuracy=0.5,
                  correct_destruction=0.0, avg_tokens=1000),
        "C": dict(refutation_discovery=0.80, accuracy=0.9,
                  correct_destruction=0.0, avg_tokens=1500)}
check("token有りかつ条件充足で KEEP", S.verdict_full(aggy)[0] == "KEEP")
# 閾値未満は DROP
aggz = {"B": dict(refutation_discovery=0.50, accuracy=0.5,
                  correct_destruction=0.0, avg_tokens=1000),
        "C": dict(refutation_discovery=0.60, accuracy=0.6,
                  correct_destruction=0.0, avg_tokens=1200)}
check("C-B < 0.20 は DROP", S.verdict_full(aggz)[0] == "DROP")

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

print("\n=== 9. B と C の counterevidence 定義が同一 ===")
b = P.build_b(cases[0]); c = P.build_c2(cases[0], "x", ["y"], [])
defline = P._COUNTEREVIDENCE_DEF
check("B に定義文がある", defline in b)
check("C に同一定義文がある", defline in c)
check("A に定義文がない", defline not in P.build_a(cases[0]))

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


print("\n=== 11. Refutation Discovery の分母が縮まないこと（修正1）===")
gt_fc02 = [c for c in cases if c["id"] == "FC-02"][0]["ground_truth"]
gt_ab01 = [c for c in cases if c["id"] == "AB-01"][0]["ground_truth"]
gt_nr01 = [c for c in cases if c["id"] == "NR-01"][0]["ground_truth"]

check("B: field欠損 -> False（Noneでない）",
      S.refutation_found(None, gt_fc02, "B", False) is False)
check("C: field欠損 -> False",
      S.refutation_found(None, gt_fc02, "C", False) is False)
check("B: malformed -> False",
      S.refutation_found(["d4"], gt_fc02, "B", True) is False)
check("C: malformed -> False",
      S.refutation_found(["d4"], gt_fc02, "C", True) is False)
check("B: 非list -> False",
      S.refutation_found("d4", gt_fc02, "B", False) is False)
check("A: 常に None（採点対象外）",
      S.refutation_found(None, gt_fc02, "A", False) is None)
check("no_refutation: 常に None",
      S.refutation_found(["d1"], gt_nr01, "C", False) is None)

# 分母が縮まないことを集計レベルで確認
mk = lambda cond, cited, mal, typ, cid: dict(
    case_id=cid, type=typ, condition=cond, trial=0, malformed=mal,
    verdict="correct", cited_counterevidence=cited,
    refutation_found=S.refutation_found(cited, gt_fc02 if typ != "no_refutation" else gt_nr01,
                                        cond, mal),
    unsupported=False, confidence=None, calls=1, in_tok=None, out_tok=None)
rows_t = [mk("B", ["d4"], False, "false_coherence", "x1"),
          mk("B", None,   False, "false_coherence", "x2"),
          mk("C", ["d4"], False, "false_coherence", "x1"),
          mk("C", None,   True,  "false_coherence", "x2")]
agg_t = S.aggregate(rows_t)
check("B の分母が 2 のまま", agg_t["B"]["refutation_scored_n"] == 2,
      str(agg_t["B"]["refutation_scored_n"]))
check("C の分母が 2 のまま（欠損で縮まない）",
      agg_t["C"]["refutation_scored_n"] == 2, str(agg_t["C"]["refutation_scored_n"]))
check("B/C の discovery が同値 0.5", agg_t["B"]["refutation_discovery"] == 0.5
      and agg_t["C"]["refutation_discovery"] == 0.5)

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

# --- AB-01: ALL 判定 ---
check("AB-01 d2のみ引用 -> False",
      S.refutation_found(["d2"], gt_ab01, "C", False) is False)
check("AB-01 d3のみ引用 -> False",
      S.refutation_found(["d3"], gt_ab01, "C", False) is False)
check("AB-01 d2+d3 引用 -> True",
      S.refutation_found(["d2", "d3"], gt_ab01, "C", False) is True)
check("AB-01 d2+d3+無関係 引用 -> True",
      S.refutation_found(["d1", "d2", "d3"], gt_ab01, "C", False) is True)

# --- FC-01: ANY 判定 ---
check("FC-01 d6のみ引用 -> False",
      S.refutation_found(["d6"], gt_fc01, "C", False) is False)
check("FC-01 d4 引用 -> True",
      S.refutation_found(["d4"], gt_fc01, "C", False) is True)
check("FC-01 d4+d6 引用 -> True",
      S.refutation_found(["d4", "d6"], gt_fc01, "C", False) is True)

check("FC-02 で d3 のみ引用 -> False",
      S.refutation_found(["d3"], gt_fc02, "C", False) is False)
check("CC-01 で d5 のみ引用 -> False",
      S.refutation_found(["d5"], gt_cc01, "C", False) is False)

print("\n=== 13. raw result completeness（修正3）===")
full_rows = [dict(case_id=c["id"], condition=cond, trial=t)
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
aggn = {"B": dict(refutation_discovery=0.2, accuracy=0.5, correct_destruction=0.0,
                  avg_tokens=1000, refutation_scored_n=10),
        "C": dict(refutation_discovery=0.9, accuracy=0.9, correct_destruction=0.0,
                  avg_tokens=1200, refutation_scored_n=14)}
v_, r_ = S.verdict_full(aggn, n_trials=1)
check("分母不一致で INCOMPLETE_RESULTS", v_ == "INCOMPLETE_RESULTS", v_)

print("\n=== 14. no_refutation 専用 judge（修正4）===")
nrp = P.build_judge_nr("工期遅延の主因は長雨である", "長雨が原因です")
check("matches_reference 形式", "matches_reference" in nrp)
check("二択候補方式を使っていない",
      "候補1" not in nrp and "候補2" not in nrp)
check("メタ候補文が消えている",
      "上記とは異なる原因を主因として挙げている" not in (HERE / "score.py").read_text(encoding="utf-8"))
check("NR判定で build_judge_nr を使う",
      "build_judge_nr" in (HERE / "score.py").read_text(encoding="utf-8"))

print("\n" + "=" * 60)
if FAIL:
    print(f"FAILED: {len(FAIL)}")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS")
