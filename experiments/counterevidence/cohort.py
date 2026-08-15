#!/usr/bin/env python3
"""未観測60ケースを、回答生成前に20件ずつ決定的に分割する。

このツールは意味の正しさを自動判定しない。構成・参照・重複・pilot流用を
fail-closedで検査し、意味監査は独立レビューで行う。
"""
import argparse
import hashlib
import json
import pathlib
import sys
from collections import Counter

import run as R


SCHEMA_VERSION = "counterevidence.cohort.v1"
SPLITS = ("calibration", "formal", "reserve")
SPLIT_COMPOSITION = {"false_coherence": 6, "absence": 4,
                     "common_cause": 4, "no_refutation": 6}
POOL_COMPOSITION = {name: count * len(SPLITS)
                    for name, count in SPLIT_COMPOSITION.items()}
PILOT_IDS = {"FC-01", "FC-02", "AB-01", "CC-01", "NR-01", "NR-02"}


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def validate_pool(cases, specs):
    errors, _ = R.validate_dataset(cases, specs, require_full=False)
    ids = [case.get("id") for case in cases]
    overlap = sorted(PILOT_IDS & set(ids))
    if overlap:
        errors.append(f"観測済みpilot IDを再利用している: {overlap}")

    composition = Counter(case.get("type") for case in cases)
    if len(cases) != 60:
        errors.append(f"cohort poolは60件必要（現在 {len(cases)}件）")
    for case_type, expected in POOL_COMPOSITION.items():
        if composition.get(case_type, 0) != expected:
            errors.append(
                f"pool構成違反: {case_type} は {expected}件必要"
                f"（現在 {composition.get(case_type, 0)}件）")

    spec_ids = [spec.get("id") for spec in specs]
    if Counter(spec_ids) != Counter(ids):
        errors.append("pool datasetとspecsのID集合・重複数が一致しない")

    for case in cases:
        case_id = case.get("id", "<missing>")
        documents = case.get("documents") or []
        doc_ids = {doc.get("doc_id") for doc in documents}
        if not 6 <= len(documents) <= 10:
            errors.append(f"{case_id}: 文書数は6〜10件が必要（現在 {len(documents)}件）")
        design = case.get("design")
        if not isinstance(design, dict):
            errors.append(f"{case_id}: design metadataが必要")
            continue
        gt = case.get("ground_truth") or {}
        if not isinstance(gt.get("why_decisive"), str) or not gt["why_decisive"].strip():
            errors.append(f"{case_id}: why_decisiveが必要")

        if case.get("type") == "no_refutation":
            alternatives = design.get("plausible_alternative_document_ids")
            if not isinstance(alternatives, list) or len(alternatives) < 2:
                errors.append(f"{case_id}: plausible alternative文書を2件以上指定")
            elif not set(alternatives) <= doc_ids:
                errors.append(f"{case_id}: plausible alternative文書IDが実在しない")
            why_not = design.get("why_alternatives_fail")
            if not isinstance(why_not, str) or not why_not.strip():
                errors.append(f"{case_id}: why_alternatives_failが必要")
        else:
            trap_support = design.get("trap_support_document_ids")
            correct_support = design.get("correct_support_document_ids")
            refs = set(gt.get("refutation_document_ids") or [])
            if not isinstance(trap_support, list) or len(trap_support) < 2:
                errors.append(f"{case_id}: trap support文書を2件以上指定")
            elif not set(trap_support) <= doc_ids:
                errors.append(f"{case_id}: trap support文書IDが実在しない")
            elif refs & set(trap_support):
                errors.append(f"{case_id}: trap supportと決定的反証文書が重複")
            if not isinstance(correct_support, list) or not correct_support:
                errors.append(f"{case_id}: correct support文書を1件以上指定")
            elif not set(correct_support) <= doc_ids:
                errors.append(f"{case_id}: correct support文書IDが実在しない")
    return errors


def split_pool(cases, specs, cohort_id):
    if not isinstance(cohort_id, str) or not cohort_id.strip():
        raise ValueError("cohort_idは空にできない")
    errors = validate_pool(cases, specs)
    if errors:
        raise ValueError("\n".join(errors))

    case_splits = {name: [] for name in SPLITS}
    assignments = []
    for case_type, per_split in SPLIT_COMPOSITION.items():
        typed = [case for case in cases if case["type"] == case_type]
        ranked = sorted(typed, key=lambda case: sha256_text(
            cohort_id + "\0" + canonical_json(case)))
        for index, split_name in enumerate(SPLITS):
            start, end = index * per_split, (index + 1) * per_split
            for case in ranked[start:end]:
                case_splits[split_name].append(case)
                assignments.append({
                    "case_id": case["id"], "type": case_type,
                    "split": split_name,
                    "content_sha256": sha256_text(canonical_json(case))})

    spec_map = {spec["id"]: spec for spec in specs}
    spec_splits = {name: [spec_map[case["id"]] for case in case_splits[name]]
                   for name in SPLITS}
    for split_name in SPLITS:
        case_splits[split_name].sort(key=lambda case: case["id"])
        spec_splits[split_name].sort(key=lambda spec: spec["id"])
    assignments.sort(key=lambda row: row["case_id"])
    return case_splits, spec_splits, assignments


def write_jsonl(path, values):
    pathlib.Path(path).write_text(
        "".join(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
                for value in values), encoding="utf-8")


def freeze_pool(pool_path, specs_path, output_dir, cohort_id):
    output_dir = pathlib.Path(output_dir)
    if output_dir.exists():
        raise ValueError(f"出力先は既に存在する（上書き禁止）: {output_dir}")
    cases, specs = R.load_jsonl(pool_path), R.load_jsonl(specs_path)
    case_splits, spec_splits, assignments = split_pool(cases, specs, cohort_id)
    output_dir.mkdir(parents=True)
    files = {}
    for split_name in SPLITS:
        dataset_name = f"{split_name}.dataset.jsonl"
        specs_name = f"{split_name}.specs.jsonl"
        write_jsonl(output_dir / dataset_name, case_splits[split_name])
        write_jsonl(output_dir / specs_name, spec_splits[split_name])
        files[dataset_name] = sha256_file(output_dir / dataset_name)
        files[specs_name] = sha256_file(output_dir / specs_name)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "cohort_id": cohort_id,
        "source": {"pool_sha256": sha256_file(pool_path),
                   "specs_sha256": sha256_file(specs_path),
                   "cohort_py_sha256": sha256_file(__file__)},
        "split_composition": SPLIT_COMPOSITION,
        "assignments": assignments,
        "files": files,
        "rules": {"pilot_ids_excluded": sorted(PILOT_IDS),
                  "answers_seen_before_split": False,
                  "individual_case_replacement": False}}
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", required=True)
    parser.add_argument("--specs", required=True)
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        manifest = freeze_pool(
            args.pool, args.specs, args.out, args.cohort_id)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print("[COHORT INVALID]")
        print(error)
        sys.exit(1)
    print(f"[COHORT FROZEN] {manifest['cohort_id']}")
    print(f"  60 cases -> {', '.join(SPLITS)} (20 each)")
    print(f"  manifest: {pathlib.Path(args.out) / 'manifest.json'}")


if __name__ == "__main__":
    main()
