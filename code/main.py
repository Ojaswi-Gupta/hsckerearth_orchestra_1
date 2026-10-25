"""Ghost Data Still Exists — Production CLI and Pipeline Runner.

Executes the complete deletion audit pipeline against official deletion requests,
performs identity resolution, intent classification, evidence ingestion, policy
audit, and remediation planning, validates schema compliance and lifecycle invariants,
and generates the authoritative output.csv.

Usage:
    python3 code/main.py
    python3 code/main.py --requests dataset/deletion_requests.csv --output output.csv
    python3 code/main.py --sample
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Ensure module import path
REPO_ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = REPO_ROOT / "dataset"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from identity import IdentityGraph
from intent import RequestIntentClassifier
from evidence import EvidenceStore
from audit import AuditEngine, DeletionPolicy, DeletionStatus
from planner import RemediationPlanner


def build_audit_pipeline(dataset_dir: Path) -> tuple[AuditEngine, RemediationPlanner]:
    """Construct and initialize all components of the deletion audit pipeline."""
    graph = IdentityGraph(
        identities_path=dataset_dir / "user_identities.csv",
        audit_log_path=dataset_dir / "user_audit_log.csv",
    )
    store = EvidenceStore(dataset_dir)
    policy = DeletionPolicy(dataset_dir / "deletion_rules.csv")
    engine = AuditEngine(
        policy=policy,
        identity_graph=graph,
        evidence_store=store,
        system_activity_path=dataset_dir / "system_activity.csv",
    )

    known_eids = set(store.manifest.keys())
    for r in store.records:
        known_eids.add(r.source_evidence_id)
    planner = RemediationPlanner(known_evidence_ids=known_eids)

    return engine, planner


def process_case(
    engine: AuditEngine,
    planner: RemediationPlanner,
    case_row: Dict[str, str],
) -> Dict[str, Any]:
    """Run audit and remediation planning for a single deletion request."""
    cid = case_row["case_id"]
    email = case_row.get("email") or case_row.get("user_email") or ""
    req_text = case_row.get("request_text", "")
    audit_at = case_row["audit_at"]
    req_at = case_row.get("requested_at")
    rep_at = case_row.get("reported_at")
    rep_status = case_row.get("reported_status")

    res = engine.audit_case(
        case_id=cid,
        email=email,
        request_text=req_text,
        audit_at=audit_at,
        requested_at=req_at,
        reported_at=rep_at,
        reported_status=rep_status,
    )

    steps = planner.plan_steps(res, validate=True)
    steps_json = planner.format_steps_json(steps)

    return {
        "case_id": cid,
        "deletion_status": res.status.value,
        "steps": steps,
        "steps_json": steps_json,
    }


def run_pipeline(
    requests_path: Path,
    output_path: Path,
    dataset_dir: Path = DATASET_DIR,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Execute full audit pipeline over request dataset and write schema-compliant CSV."""
    if not requests_path.exists():
        raise FileNotFoundError(f"Requests file not found: {requests_path}")

    with open(requests_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        request_rows = list(reader)

    total_cases = len(request_rows)
    if not quiet:
        print(f"Loaded {total_cases} deletion requests from {requests_path}")
        print("Initializing pipeline engines (Identity, Evidence, Audit, Planner)...")

    t_start = time.time()
    engine, planner = build_audit_pipeline(dataset_dir)
    init_time = time.time() - t_start

    if not quiet:
        print(f"Pipeline initialized in {init_time:.2f}s. Beginning case processing...")

    predictions: List[Dict[str, str]] = []
    status_counts: Dict[str, int] = {"COMPLETE": 0, "INCOMPLETE": 0, "NOT_APPLICABLE": 0}
    total_steps = 0

    t_proc_start = time.time()
    for idx, row in enumerate(request_rows, start=1):
        pred = process_case(engine, planner, row)
        st = pred["deletion_status"]
        status_counts[st] = status_counts.get(st, 0) + 1
        total_steps += len(pred["steps"])

        predictions.append({
            "case_id": pred["case_id"],
            "deletion_status": pred["deletion_status"],
            "steps_json": pred["steps_json"],
        })

        if not quiet and (idx % 100 == 0 or idx == total_cases):
            elapsed = time.time() - t_proc_start
            rate = idx / elapsed if elapsed > 0 else 0
            print(f"  Processed {idx}/{total_cases} cases ({idx/total_cases*100:.1f}%) — {rate:.1f} cases/sec")

    proc_time = time.time() - t_proc_start

    # Write output CSV atomically
    tmp_output = output_path.with_suffix(".csv.tmp")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_output, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "deletion_status", "steps_json"])
        writer.writeheader()
        writer.writerows(predictions)

    os.replace(tmp_output, output_path)

    summary = {
        "total_cases": total_cases,
        "status_distribution": status_counts,
        "total_steps": total_steps,
        "avg_steps_per_case": round(total_steps / total_cases, 2) if total_cases > 0 else 0,
        "elapsed_seconds": round(proc_time, 3),
        "mean_latency_ms": round(proc_time / total_cases * 1000, 2) if total_cases > 0 else 0,
        "output_file": str(output_path),
    }

    if not quiet:
        print("\n" + "=" * 60)
        print(" FULL PIPELINE EXECUTION SUMMARY")
        print("=" * 60)
        print(f"Output File:           {output_path}")
        print(f"Total Requests:        {total_cases}")
        print(f"Processing Time:       {proc_time:.2f}s ({summary['mean_latency_ms']} ms/case)")
        print("-" * 60)
        print("Status Distribution:")
        for st, count in status_counts.items():
            pct = count / total_cases * 100 if total_cases > 0 else 0
            print(f"  {st:<16} {count:>5} ({pct:>5.1f}%)")
        print("-" * 60)
        print(f"Total Planned Steps:   {total_steps} (avg {summary['avg_steps_per_case']} steps/case)")
        print("=" * 60)

    return summary


def main() -> None:
    """CLI entry point for running the deletion audit pipeline."""
    parser = argparse.ArgumentParser(description="Ghost Data Still Exists Deletion Audit Pipeline")
    parser.add_argument("--requests", type=Path, default=DATASET_DIR / "deletion_requests.csv", help="Requests CSV path")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "output.csv", help="Target output CSV path")
    parser.add_argument("--dataset-dir", type=Path, default=DATASET_DIR, help="Dataset directory path")
    parser.add_argument("--sample", action="store_true", help="Process sample dataset instead of full requests")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")

    args = parser.parse_args()

    if args.sample:
        req_path = args.dataset_dir / "sample_deletion_requests.csv"
        out_path = args.output if args.output != (REPO_ROOT / "output.csv") else REPO_ROOT / "output_sample.csv"
    else:
        req_path = args.requests
        out_path = args.output

    run_pipeline(
        requests_path=req_path,
        output_path=out_path,
        dataset_dir=args.dataset_dir,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
