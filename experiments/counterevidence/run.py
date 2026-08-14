#!/usr/bin/env python3
"""
run.py -- A/B/C 条件を実行し、生の結果と実行証跡を保存する。採点はしない。

manual は run ごとの prompts/ に書き出し、responses/ に貼り付けた応答を読む。
provider/model は stage 1 で必須。token 数は推測せず記録しない。
api は Anthropic API を使う（ANTHROPIC_API_KEY が必要）。

各 run は results/runs/<run_id>/ に分離する。stage 2/3 は manifest と
入力・コード・prompt/response SHA-256 を照合し、途中のドリフトを拒否する。
"""
import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import uuid
from collections import Counter

HERE = pathlib.Path(__file__).parent
RESULTS = HERE / "results"
DEFAULT_API_MODEL = "claude-haiku-4-5-20251001"
SCHEMA_VERSION = "counterevidence.run.v2"
RAW_SCHEMA_VERSION = "counterevidence.raw.v2"
ATTESTATION_SCHEMA_VERSION = "counterevidence.attestations.v1"
MAX_TOKENS = {"A": 1200, "B": 2000, "C1": 1200, "C2": 1200}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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
        cid, typ = c["id"], c.get("type")
        if typ not in VALID_TYPES:
            errors.append(f"{cid}: 不正なtype '{typ}'")
        gt = c.get("ground_truth", {})
        doc_ids = {d["doc_id"] for d in c.get("documents", [])}
        if len(doc_ids) != len(c.get("documents", [])):
            errors.append(f"{cid}: doc_id重複")
        correct, trap = gt.get("correct_conclusion"), gt.get("trap_conclusion")
        refs, match = gt.get("refutation_document_ids"), gt.get("refutation_match")
        if not correct:
            errors.append(f"{cid}: correct_conclusion なし")
        if typ == "no_refutation":
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
            for ref in refs or []:
                if ref not in doc_ids:
                    errors.append(f"{cid}: refutation_document_id '{ref}' が実在しない")
            if correct and trap and correct.strip() == trap.strip():
                errors.append(f"{cid}: correct と trap が同一")

    comp = Counter(c["type"] for c in cases)
    if require_full:
        if len(cases) != 20:
            errors.append(f"full実験には20件必要（現在 {len(cases)}件）")
        for typ, n in FULL_COMPOSITION.items():
            if comp.get(typ, 0) != n:
                errors.append(f"full構成違反: {typ} は {n} 件必要（現在 {comp.get(typ,0)}件）")
    else:
        warnings.append(f"件数 {len(cases)} / 構成 {dict(comp)}（pilot想定）")

    if specs:
        smap = {s["id"]: s for s in specs}
        for c in cases:
            spec = smap.get(c["id"])
            if spec is None:
                errors.append(f"{c['id']}: specs.jsonl に対応する仕様がない")
                continue
            if spec.get("type") != c.get("type"):
                errors.append(f"{c['id']}: specs と dataset で type が不一致")
            if (spec.get("correct") or "").strip() != \
               (c["ground_truth"].get("correct_conclusion") or "").strip():
                errors.append(f"{c['id']}: specs と dataset で correct_conclusion が不一致")
            if (spec.get("trap") or "").strip() != \
               (c["ground_truth"].get("trap_conclusion") or "").strip():
                errors.append(f"{c['id']}: specs と dataset で trap_conclusion が不一致")
            if spec.get("refutation_match") != c["ground_truth"].get("refutation_match"):
                errors.append(f"{c['id']}: specs と dataset で refutation_match が不一致")
    return errors, warnings


# ================================================================ 検索
def bigrams(s):
    s = "".join(s.split())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def retrieve(queries, documents, top_k=2):
    """query語と文書の文字bigram重なりで上位k件。ground truthの位置は使わない。"""
    qg = set()
    for query in queries:
        qg |= bigrams(query)
    scored = []
    for doc in documents:
        dg = bigrams(doc["title"] + doc["text"])
        if dg:
            scored.append((len(qg & dg) / (len(qg) ** 0.5 + 1e-9), doc))
    scored.sort(key=lambda x: -x[0])
    return [doc for score, doc in scored[:top_k] if score > 0]


# ================================================================ 証跡
def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def file_record(path):
    path = pathlib.Path(path)
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
    return {"sha256": sha256_file(path),
            "file_mtime_utc": mtime.isoformat().replace("+00:00", "Z"),
            "collected_at_utc": utc_now()}


def write_json(path, value):
    pathlib.Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def source_fingerprints(dataset_path, specs_path):
    paths = {"dataset": pathlib.Path(dataset_path),
             "specs": pathlib.Path(specs_path),
             "prompts.py": HERE / "prompts.py", "run.py": HERE / "run.py",
             "score.py": HERE / "score.py"}
    return {name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in paths.items()}


def new_run_id():
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def run_path(results_root, run_id):
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"不正な run_id: {run_id!r}")
    return pathlib.Path(results_root) / "runs" / run_id


def active_run_id(results_root):
    path = pathlib.Path(results_root) / "active_run.txt"
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        raise ValueError("active run がありません。先に stage 1 を実行してください")
    return path.read_text(encoding="utf-8").strip()


def resolve_run(results_root, requested=None):
    run_id = requested or active_run_id(results_root)
    run_dir = run_path(results_root, run_id)
    if not run_dir.is_dir():
        raise ValueError(f"run が存在しません: {run_id}")
    return run_id, run_dir


def load_manifest(run_dir):
    path = pathlib.Path(run_dir) / "run_manifest.json"
    if not path.exists():
        raise ValueError(f"manifest がありません: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest_context(manifest, dataset_path, specs_path, run_dir):
    errors = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"未知の manifest schema: {manifest.get('schema_version')}")
    for name, current in source_fingerprints(dataset_path, specs_path).items():
        expected = manifest.get("inputs", {}).get(name, {}).get("sha256")
        if current["sha256"] != expected:
            errors.append(f"{name} SHA-256 drift: {expected} -> {current['sha256']}")
    for name, record in manifest.get("prompts", {}).items():
        path = pathlib.Path(run_dir) / "prompts" / f"{name}.txt"
        if not path.exists():
            errors.append(f"prompt 欠落: {name}")
        elif sha256_file(path) != record.get("sha256"):
            errors.append(f"prompt SHA-256 drift: {name}")
    return errors


def create_run(results_root, backend, provider, model, temperature,
               dataset_path, specs_path, requested_id=None, trials=1):
    run_id = requested_id or new_run_id()
    run_dir = run_path(results_root, run_id)
    if run_dir.exists():
        raise ValueError(f"run_id は既に存在します: {run_id}")
    (run_dir / "prompts").mkdir(parents=True)
    (run_dir / "responses").mkdir()
    root = pathlib.Path(results_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "active_run.txt").write_text(run_id + "\n", encoding="utf-8")
    manifest = {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "status": "created",
        "backend": backend, "provider": provider, "model": model,
        "settings": {
            "temperature": temperature,
            "temperature_status": "declared" if temperature is not None else "unknown",
            "model_verification": ("provider_reported" if backend == "api"
                                   else "unverifiable_manual"),
            "max_output_tokens": MAX_TOKENS if backend == "api" else None,
            "trials": trials},
        "started_at_utc": utc_now(),
        "inputs": source_fingerprints(dataset_path, specs_path),
        "prompts": {}, "c2_derivations": {}, "api_calls": {}}
    write_json(run_dir / "run_manifest.json", manifest)
    return manifest, run_dir


def write_prompt(run_dir, manifest, name, prompt):
    path = pathlib.Path(run_dir) / "prompts" / f"{name}.txt"
    path.write_text(prompt, encoding="utf-8")
    manifest["prompts"][name] = {
        "sha256": sha256_text(prompt), "generated_at_utc": utc_now()}
    return prompt


def read_response(run_dir, name):
    path = pathlib.Path(run_dir) / "responses" / f"{name}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


def response_record(run_dir, name):
    path = pathlib.Path(run_dir) / "responses" / f"{name}.txt"
    record = file_record(path)
    record["name"] = name
    return record


def prompt_record(manifest, name):
    record = dict(manifest["prompts"][name])
    record["name"] = name
    return record


def invalidate_response(run_dir, name):
    """古いpromptに対する応答を削除せず退避し、新promptへの流用を防ぐ。"""
    path = pathlib.Path(run_dir) / "responses" / f"{name}.txt"
    if not path.exists():
        return None
    archive = pathlib.Path(run_dir) / "responses" / "invalidated"
    archive.mkdir(exist_ok=True)
    digest = sha256_file(path)[:12]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = archive / f"{name}__{stamp}__{digest}.txt"
    path.replace(destination)
    return destination


def load_attestations(results_root):
    path = pathlib.Path(results_root) / "run_attestations.json"
    if not path.exists():
        return {"schema_version": ATTESTATION_SCHEMA_VERSION, "entries": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != ATTESTATION_SCHEMA_VERSION \
            or not isinstance(value.get("entries"), list):
        raise ValueError(f"attestation ledger が不正です: {path}")
    return value


def append_attestation(results_root, run_dir, manifest, raw_path):
    """小さいdigest台帳をruns/の外へ残し、Git差分で改変を検知可能にする。"""
    ledger = load_attestations(results_root)
    run_id = manifest["run_id"]
    if any(entry.get("run_id") == run_id for entry in ledger["entries"]):
        raise ValueError(f"attestation は既に存在します: {run_id}")
    manifest_path = pathlib.Path(run_dir) / "run_manifest.json"
    ledger["entries"].append({
        "run_id": run_id,
        "completed_at_utc": manifest["completed_at_utc"],
        "raw_sha256": sha256_file(raw_path),
        "manifest_sha256": sha256_file(manifest_path),
        "dataset_sha256": manifest["inputs"]["dataset"]["sha256"],
        "specs_sha256": manifest["inputs"]["specs"]["sha256"],
        "backend": manifest["backend"], "provider": manifest["provider"],
        "model": manifest["model"]})
    write_json(pathlib.Path(results_root) / "run_attestations.json", ledger)


def provenance_base(manifest):
    return {
        "schema_version": RAW_SCHEMA_VERSION, "run_id": manifest["run_id"],
        "backend": manifest["backend"], "provider": manifest["provider"],
        "model": manifest["model"], "settings": manifest["settings"],
        "dataset_sha256": manifest["inputs"]["dataset"]["sha256"],
        "specs_sha256": manifest["inputs"]["specs"]["sha256"]}


def record_api_artifact(run_dir, manifest, name, text, call):
    """各API応答を直ちに保存する。後続call失敗時も費用と応答の証跡を失わない。"""
    path = pathlib.Path(run_dir) / "responses" / f"{name}.txt"
    path.write_text(text, encoding="utf-8")
    record = dict(call)
    record["name"] = name
    manifest["api_calls"][name] = record
    manifest["status"] = "running"
    write_json(pathlib.Path(run_dir) / "run_manifest.json", manifest)


# ================================================================ backend
def call_api(prompt, model, max_tokens=1200, temperature=None):
    import anthropic
    client = anthropic.Anthropic()
    started = utc_now()
    kwargs = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]}
    if temperature is not None:
        kwargs["temperature"] = temperature
    response = client.messages.create(**kwargs)
    completed = utc_now()
    text = "".join(block.text for block in response.content if block.type == "text")
    usage = {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens, "calls": 1}
    call = {"prompt_sha256": sha256_text(prompt),
            "response_sha256": sha256_text(text),
            "started_at_utc": started, "completed_at_utc": completed,
            "requested_model": model,
            "reported_model": getattr(response, "model", None),
            "max_output_tokens": max_tokens, "temperature": temperature,
            "stop_reason": getattr(response, "stop_reason", None)}
    return text, usage, call


def parse_json(text):
    value = text.strip()
    if value.startswith("```"):
        value = value.split("```")[1]
        if value.startswith("json"):
            value = value[4:]
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON found:\n" + text[:300])
    return json.loads(value[start:end + 1])


def load_jsonl(path):
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


# ================================================================ 実行
def run_api(cases, model, trials, temperature, manifest, run_dir):
    rows, base = [], provenance_base(manifest)
    for case in cases:
        for trial in range(trials):
            names = {cond: f"{case['id']}__{cond}__t{trial}"
                     for cond in ("A", "B", "C1", "C2")}
            prompt_a = write_prompt(run_dir, manifest, names["A"], P.build_a(case))
            text, usage, call = call_api(prompt_a, model, MAX_TOKENS["A"], temperature)
            record_api_artifact(run_dir, manifest, names["A"], text, call)
            rows.append(dict(case_id=case["id"], condition="A", trial=trial,
                             raw_answer=text, usage=usage,
                             provenance={**base, "calls": [call]}))

            prompt_b = write_prompt(run_dir, manifest, names["B"], P.build_b(case))
            text, usage, call = call_api(prompt_b, model, MAX_TOKENS["B"], temperature)
            record_api_artifact(run_dir, manifest, names["B"], text, call)
            rows.append(dict(case_id=case["id"], condition="B", trial=trial,
                             raw_answer=text, usage=usage,
                             provenance={**base, "calls": [call]}))

            prompt_c1 = write_prompt(run_dir, manifest, names["C1"], P.build_c1(case))
            text1, usage1, call1 = call_api(
                prompt_c1, model, MAX_TOKENS["C1"], temperature)
            record_api_artifact(run_dir, manifest, names["C1"], text1, call1)
            step1 = parse_json(text1)
            docs = retrieve(step1.get("queries", []), case["documents"])
            prompt_c2 = write_prompt(
                run_dir, manifest, names["C2"],
                P.build_c2(case, step1.get("provisional_conclusion", ""),
                           step1.get("falsifiers", []), docs))
            text2, usage2, call2 = call_api(
                prompt_c2, model, MAX_TOKENS["C2"], temperature)
            record_api_artifact(run_dir, manifest, names["C2"], text2, call2)
            rows.append(dict(
                case_id=case["id"], condition="C", trial=trial,
                provisional_answer=step1.get("provisional_conclusion"),
                falsifier=step1.get("falsifiers"), queries=step1.get("queries"),
                retrieved_docs=[doc["doc_id"] for doc in docs], raw_answer=text2,
                usage={"input_tokens": usage1["input_tokens"] + usage2["input_tokens"],
                       "output_tokens": usage1["output_tokens"] + usage2["output_tokens"],
                       "calls": 2},
                provenance={**base, "calls": [call1, call2]}))
    write_json(run_dir / "run_manifest.json", manifest)
    return rows


def manual_stage1(cases, manifest, run_dir):
    for case in cases:
        write_prompt(run_dir, manifest, f"{case['id']}__A", P.build_a(case))
        write_prompt(run_dir, manifest, f"{case['id']}__B", P.build_b(case))
        write_prompt(run_dir, manifest, f"{case['id']}__C1", P.build_c1(case))
    manifest["status"] = "awaiting_stage1_responses"
    write_json(run_dir / "run_manifest.json", manifest)
    print(f"run_id: {manifest['run_id']}")
    print(f"{len(cases)*3} prompts -> {run_dir/'prompts'}")
    print("各プロンプトは新しい会話で、manifest記載のprovider/model/settingsで実行すること。")
    print(f"応答を {run_dir/'responses'}/<同名>.txt へ保存してから stage 2")


def manual_stage2(cases, manifest, run_dir):
    made = 0
    for case in cases:
        c1name = f"{case['id']}__C1"
        text = read_response(run_dir, c1name)
        if text is None:
            print(f"  skip {case['id']}: C1応答なし")
            continue
        step1 = parse_json(text)
        docs = retrieve(step1.get("queries", []), case["documents"])
        c2name = f"{case['id']}__C2"
        prompt = P.build_c2(case, step1.get("provisional_conclusion", ""),
                            step1.get("falsifiers", []), docs)
        c1_record = response_record(run_dir, c1name)
        old = manifest["c2_derivations"].get(case["id"])
        if (old and old.get("c1_response", {}).get("sha256") == c1_record["sha256"]
                and old.get("c2_prompt", {}).get("sha256") == sha256_text(prompt)):
            print(f"  unchanged {case['id']}: C2 prompt/response を維持")
            continue
        invalidated = invalidate_response(run_dir, c2name)
        if invalidated:
            print(f"  invalidated {case['id']}: 古いC2応答を {invalidated.name} へ退避")
        write_prompt(run_dir, manifest, c2name, prompt)
        manifest["c2_derivations"][case["id"]] = {
            "c1_response": c1_record,
            "provisional_answer": step1.get("provisional_conclusion"),
            "falsifiers": step1.get("falsifiers"), "queries": step1.get("queries"),
            "retrieved_docs": [doc["doc_id"] for doc in docs],
            "c2_prompt": prompt_record(manifest, c2name),
            "c2_response": None, "generated_at_utc": utc_now()}
        made += 1
    manifest["status"] = "awaiting_stage2_responses"
    write_json(run_dir / "run_manifest.json", manifest)
    print(f"run_id: {manifest['run_id']}")
    print(f"{made} C2 prompts generated")


def manual_missing_responses(cases, run_dir):
    return [f"{case['id']}__{cond}.txt" for case in cases
            for cond in ("A", "B", "C1", "C2")
            if read_response(run_dir, f"{case['id']}__{cond}") is None]


def manual_stage3(cases, manifest, run_dir):
    """全応答と stage 2 証跡が揃い、SHAが一致する場合だけ raw を生成する。"""
    missing = manual_missing_responses(cases, run_dir)
    if missing:
        raise ValueError("応答不足のため raw.jsonl を生成しません: " + ", ".join(missing))
    rows, base = [], provenance_base(manifest)
    for case in cases:
        for cond in ("A", "B"):
            name = f"{case['id']}__{cond}"
            rows.append(dict(
                case_id=case["id"], condition=cond, trial=0,
                raw_answer=read_response(run_dir, name), usage={"calls": 1},
                provenance={**base, "prompts": [prompt_record(manifest, name)],
                            "responses": [response_record(run_dir, name)],
                            "assembled_at_utc": utc_now()}))

        derivation = manifest.get("c2_derivations", {}).get(case["id"])
        if not derivation:
            raise ValueError(f"{case['id']}: C2 derivation 証跡がありません。stage 2 を再実行してください")
        c1name, c2name = f"{case['id']}__C1", f"{case['id']}__C2"
        c1record = response_record(run_dir, c1name)
        if c1record["sha256"] != derivation.get("c1_response", {}).get("sha256"):
            raise ValueError(f"{case['id']}: C1 response SHA-256 drift")
        if prompt_record(manifest, c2name)["sha256"] != \
                derivation.get("c2_prompt", {}).get("sha256"):
            raise ValueError(f"{case['id']}: C2 prompt SHA-256 drift")
        c2record = response_record(run_dir, c2name)
        generated = derivation.get("c2_prompt", {}).get("generated_at_utc")
        if not generated or c2record["file_mtime_utc"] <= generated:
            raise ValueError(f"{case['id']}: C2応答がC2 prompt生成後に収集されたと確認できません")
        bound = derivation.get("c2_response")
        if bound and bound.get("sha256") != c2record["sha256"]:
            raise ValueError(f"{case['id']}: C2 response SHA-256 drift")
        derivation["c2_response"] = c2record
        rows.append(dict(
            case_id=case["id"], condition="C", trial=0,
            provisional_answer=derivation.get("provisional_answer"),
            falsifier=derivation.get("falsifiers"), queries=derivation.get("queries"),
            retrieved_docs=derivation.get("retrieved_docs"),
            raw_answer=read_response(run_dir, c2name), usage={"calls": 2},
            provenance={**base,
                        "prompts": [prompt_record(manifest, c1name),
                                    prompt_record(manifest, c2name)],
                        "responses": [c1record, c2record],
                        "assembled_at_utc": utc_now()}))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(HERE / "dataset.jsonl"))
    parser.add_argument("--specs", default=str(HERE / "specs.jsonl"))
    parser.add_argument("--results", default=str(RESULTS))
    parser.add_argument("--backend", choices=["manual", "api"], default="manual")
    parser.add_argument("--stage", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--run-id")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--require-full", action="store_true")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()

    dataset_path, specs_path = pathlib.Path(args.dataset), pathlib.Path(args.specs)
    cases = load_jsonl(dataset_path)
    specs = load_jsonl(specs_path) if specs_path.exists() else None
    errors, warnings = validate_dataset(cases, specs, require_full=args.require_full)
    if not specs_path.exists():
        errors.append(f"specs.jsonl が存在しない: {specs_path}")
    if args.trials < 1:
        errors.append(f"trials は1以上が必要（現在 {args.trials}）")
    for warning in warnings:
        print(f"[warn] {warning}")
    if errors:
        print("[VALIDATION FAILED]")
        for error in errors:
            print("  - " + error)
        sys.exit(1)
    print(f"[validation ok] {len(cases)} cases")
    if args.validate:
        return

    try:
        if args.backend == "api":
            if args.provider not in (None, "anthropic"):
                raise ValueError("api backend の provider は anthropic のみです")
            if args.temperature is not None and not 0 <= args.temperature <= 1:
                raise ValueError("Anthropic API の temperature は0〜1です")
            model = args.model or DEFAULT_API_MODEL
            manifest, run_dir = create_run(
                args.results, "api", "anthropic", model, args.temperature,
                dataset_path, specs_path, args.run_id, args.trials)
            rows = run_api(cases, model, args.trials, args.temperature, manifest, run_dir)
        elif args.stage == 1:
            if not args.provider or not args.model:
                raise ValueError("manual stage 1 は --provider と --model が必須です")
            if args.trials != 1:
                raise ValueError("manual backend の trials は1固定です")
            manifest, run_dir = create_run(
                args.results, "manual", args.provider, args.model, args.temperature,
                dataset_path, specs_path, args.run_id, 1)
            manual_stage1(cases, manifest, run_dir)
            return
        else:
            run_id, run_dir = resolve_run(args.results, args.run_id)
            manifest = load_manifest(run_dir)
            if manifest.get("backend") != "manual":
                raise ValueError(f"run {run_id} は manual backend ではありません")
            raw_path = run_dir / "raw.jsonl"
            if raw_path.exists():
                raise ValueError(f"run {run_id} は raw.jsonl が存在するため不変です。"
                                 "新しいrunを開始してください")
            if manifest.get("status") == "complete":
                raise ValueError(f"run {run_id} は完了済みで不変です。新しいrunを開始してください")
            drift = validate_manifest_context(
                manifest, dataset_path, specs_path, run_dir)
            if drift:
                raise ValueError("実行証跡がドリフトしています:\n  - " + "\n  - ".join(drift))
            if args.stage == 2:
                manual_stage2(cases, manifest, run_dir)
                return
            rows = manual_stage3(cases, manifest, run_dir)

        raw_path = run_dir / "raw.jsonl"
        if raw_path.exists():
            raise ValueError(f"raw.jsonl は既に存在します: {raw_path}")
        with open(raw_path, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        manifest["status"] = "complete"
        manifest["completed_at_utc"] = utc_now()
        manifest["raw"] = {"sha256": sha256_file(raw_path), "rows": len(rows)}
        write_json(run_dir / "run_manifest.json", manifest)
        append_attestation(args.results, run_dir, manifest, raw_path)
        raw_path.chmod(0o444)
        (run_dir / "run_manifest.json").chmod(0o444)
        print(f"run_id: {manifest['run_id']}")
        print(f"{len(rows)} rows -> {raw_path}")
    except (ValueError, json.JSONDecodeError) as error:
        print(f"[RUN REFUSED] {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
