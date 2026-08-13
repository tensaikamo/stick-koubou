#!/usr/bin/env python3
"""
run.py -- A/B/C 条件を実行し、生の結果だけを保存する。採点はしない。

backend:
  manual : プロンプトをファイルへ書き出し、貼り付けた応答を読む（課金なし）
           token 数は取得できないため usage に token フィールドを作らない。
  api    : Anthropic API を使う（ANTHROPIC_API_KEY が必要）

trial:
  同一プロンプトの確率的反復番号。APIへ実seedは渡していない（渡せない）。
  再現可能なseedではないので seed と呼ばない。

使い方:
  python run.py --validate                     # データセット検証のみ
  python run.py --backend manual --stage 1
  python run.py --backend manual --stage 2
  python run.py --backend manual --stage 3
  python run.py --backend api --trials 3
"""
import argparse, json, sys, pathlib
from collections import Counter

HERE = pathlib.Path(__file__).parent
RESULTS = HERE / "results"
sys.path.insert(0, str(HERE))
import prompts as P

VALID_TYPES = {"false_coherence", "absence", "common_cause", "no_refutation"}
FULL_COMPOSITION = {"false_coherence": 6, "absence": 4,
                    "common_cause": 4, "no_refutation": 6}


# ================================================================ validator
def validate_dataset(cases, specs=None, require_full=False):
    """実験開始前の整合性検証。errors が空でなければ実行してはならない。"""
    errors, warnings = [], []

    ids = [c["id"] for c in cases]
    dup = [i for i, n in Counter(ids).items() if n > 1]
    if dup:
        errors.append(f"id重複: {dup}")

    for c in cases:
        cid = c["id"]
        t = c.get("type")
        if t not in VALID_TYPES:
            errors.append(f"{cid}: 不正なtype '{t}'")
        gt = c.get("ground_truth", {})
        doc_ids = {d["doc_id"] for d in c.get("documents", [])}
        if len(doc_ids) != len(c.get("documents", [])):
            errors.append(f"{cid}: doc_id重複")

        correct = gt.get("correct_conclusion")
        trap = gt.get("trap_conclusion")
        refs = gt.get("refutation_document_ids")
        match = gt.get("refutation_match")

        if not correct:
            errors.append(f"{cid}: correct_conclusion なし")

        if t == "no_refutation":
            if trap:
                errors.append(f"{cid}: no_refutation なのに trap がある")
            if refs:
                errors.append(f"{cid}: no_refutation なのに refutation_document_ids がある")
            if match:
                errors.append(f"{cid}: no_refutation なのに refutation_match がある")
        else:
            if not trap:
                errors.append(f"{cid}: trap_conclusion がない")
            if not refs:
                errors.append(f"{cid}: refutation_document_ids がない")
            if match not in ("ANY", "ALL"):
                errors.append(f"{cid}: refutation_match は ANY|ALL のいずれか（現在 {match!r}）")
            for r in refs or []:
                if r not in doc_ids:
                    errors.append(f"{cid}: refutation_document_id '{r}' が実在しない")
            if correct and trap and correct.strip() == trap.strip():
                errors.append(f"{cid}: correct と trap が同一")

    comp = Counter(c["type"] for c in cases)
    if require_full:
        if len(cases) != 20:
            errors.append(f"full実験には20件必要（現在 {len(cases)}件）")
        for t, n in FULL_COMPOSITION.items():
            if comp.get(t, 0) != n:
                errors.append(f"full構成違反: {t} は {n} 件必要（現在 {comp.get(t,0)}件）")
    else:
        warnings.append(f"件数 {len(cases)} / 構成 {dict(comp)}（pilot想定）")

    if specs:
        smap = {s["id"]: s for s in specs}
        for c in cases:
            s = smap.get(c["id"])
            if s is None:
                errors.append(f"{c['id']}: specs.jsonl に対応する仕様がない")
                continue
            if s.get("type") != c.get("type"):
                errors.append(f"{c['id']}: specs と dataset で type が不一致")
            if (s.get("correct") or "").strip() != \
               (c["ground_truth"].get("correct_conclusion") or "").strip():
                errors.append(f"{c['id']}: specs と dataset で correct_conclusion が不一致")
            st = (s.get("trap") or "").strip()
            dt = (c["ground_truth"].get("trap_conclusion") or "").strip()
            if st != dt:
                errors.append(f"{c['id']}: specs と dataset で trap_conclusion が不一致")
            if s.get("refutation_match") != c["ground_truth"].get("refutation_match"):
                errors.append(f"{c['id']}: specs と dataset で refutation_match が不一致")
    return errors, warnings


# ================================================================ 検索
def bigrams(s):
    s = "".join(s.split())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def retrieve(queries, documents, top_k=2):
    """query語と文書の文字bigram重なりで上位k件。ground truthの位置は使わない。"""
    qg = set()
    for q in queries:
        qg |= bigrams(q)
    scored = []
    for d in documents:
        dg = bigrams(d["title"] + d["text"])
        if not dg:
            continue
        scored.append((len(qg & dg) / (len(qg) ** 0.5 + 1e-9), d))
    scored.sort(key=lambda x: -x[0])
    return [d for s, d in scored[:top_k] if s > 0]


# ================================================================ backend
def call_api(prompt, model, max_tokens=1200):
    import anthropic
    client = anthropic.Anthropic()
    r = client.messages.create(model=model, max_tokens=max_tokens,
                               messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in r.content if b.type == "text")
    return text, {"input_tokens": r.usage.input_tokens,
                  "output_tokens": r.usage.output_tokens, "calls": 1}


def parse_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("no JSON found:\n" + text[:300])
    return json.loads(t[a:b + 1])


def write_prompt(name, text):
    d = RESULTS / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.txt").write_text(text, encoding="utf-8")


def read_response(name):
    p = RESULTS / "responses" / f"{name}.txt"
    return p.read_text(encoding="utf-8") if p.exists() else None


# ================================================================ 実行
def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def run_api(cases, model, trials):
    out = []
    for case in cases:
        for trial in range(trials):
            t, u = call_api(P.build_a(case), model)
            out.append(dict(case_id=case["id"], condition="A", trial=trial,
                            raw_answer=t, usage=u))

            t, u = call_api(P.build_b(case), model, max_tokens=2000)
            out.append(dict(case_id=case["id"], condition="B", trial=trial,
                            raw_answer=t, usage=u))

            t1, u1 = call_api(P.build_c1(case), model)
            s1 = parse_json(t1)
            docs = retrieve(s1.get("queries", []), case["documents"])
            t2, u2 = call_api(P.build_c2(case, s1.get("provisional_conclusion", ""),
                                         s1.get("falsifiers", []), docs), model)
            out.append(dict(
                case_id=case["id"], condition="C", trial=trial,
                provisional_answer=s1.get("provisional_conclusion"),
                falsifier=s1.get("falsifiers"), queries=s1.get("queries"),
                retrieved_docs=[d["doc_id"] for d in docs],
                raw_answer=t2,
                usage={"input_tokens": u1["input_tokens"] + u2["input_tokens"],
                       "output_tokens": u1["output_tokens"] + u2["output_tokens"],
                       "calls": 2}))
    return out


def manual_stage1(cases):
    for case in cases:
        write_prompt(f"{case['id']}__A", P.build_a(case))
        write_prompt(f"{case['id']}__B", P.build_b(case))
        write_prompt(f"{case['id']}__C1", P.build_c1(case))
    print(f"{len(cases)*3} prompts -> {RESULTS/'prompts'}")
    print("各プロンプトは新しい会話で実行すること。")
    print(f"応答を {RESULTS/'responses'}/<同名>.txt へ保存してから stage 2")


def manual_stage2(cases):
    (RESULTS / "responses").mkdir(parents=True, exist_ok=True)
    made = 0
    for case in cases:
        r = read_response(f"{case['id']}__C1")
        if r is None:
            print(f"  skip {case['id']}: C1応答なし")
            continue
        s1 = parse_json(r)
        docs = retrieve(s1.get("queries", []), case["documents"])
        write_prompt(f"{case['id']}__C2",
                     P.build_c2(case, s1.get("provisional_conclusion", ""),
                                s1.get("falsifiers", []), docs))
        made += 1
    print(f"{made} C2 prompts generated")


def manual_stage3(cases):
    """manual では token 数が存在しない。0 で埋めず、フィールド自体を作らない。"""
    out = []
    for case in cases:
        for cond in ("A", "B"):
            r = read_response(f"{case['id']}__{cond}")
            if r:
                out.append(dict(case_id=case["id"], condition=cond, trial=0,
                                raw_answer=r, usage={"calls": 1}))
        r1, r2 = read_response(f"{case['id']}__C1"), read_response(f"{case['id']}__C2")
        if r1 and r2:
            s1 = parse_json(r1)
            docs = retrieve(s1.get("queries", []), case["documents"])
            out.append(dict(
                case_id=case["id"], condition="C", trial=0,
                provisional_answer=s1.get("provisional_conclusion"),
                falsifier=s1.get("falsifiers"), queries=s1.get("queries"),
                retrieved_docs=[d["doc_id"] for d in docs],
                raw_answer=r2, usage={"calls": 2}))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(HERE / "dataset.jsonl"))
    ap.add_argument("--specs", default=str(HERE / "specs.jsonl"))
    ap.add_argument("--backend", choices=["manual", "api"], default="manual")
    ap.add_argument("--stage", type=int, default=1)
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--require-full", action="store_true")
    ap.add_argument("--validate", action="store_true")
    a = ap.parse_args()

    cases = load_jsonl(a.dataset)
    specs = load_jsonl(a.specs) if pathlib.Path(a.specs).exists() else None
    errors, warnings = validate_dataset(cases, specs, require_full=a.require_full)
    for w in warnings:
        print(f"[warn] {w}")
    if errors:
        print("[VALIDATION FAILED]")
        for e in errors:
            print("  - " + e)
        sys.exit(1)
    print(f"[validation ok] {len(cases)} cases")
    if a.validate:
        return

    RESULTS.mkdir(exist_ok=True)
    if a.backend == "api":
        rows = run_api(cases, a.model, a.trials)
    else:
        if a.stage == 1:
            manual_stage1(cases); return
        if a.stage == 2:
            manual_stage2(cases); return
        rows = manual_stage3(cases)

    with open(RESULTS / "raw.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(rows)} rows -> {RESULTS/'raw.jsonl'}")


if __name__ == "__main__":
    main()
