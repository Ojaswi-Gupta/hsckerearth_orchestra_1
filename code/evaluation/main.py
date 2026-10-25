"""Sample Benchmark and Evaluation Validator for Ghost Data Still Exists.

Executes the complete deletion audit pipeline against the official sample
dataset (or arbitrary request/reference datasets), validates prediction
schemas and lifecycle invariants, computes accuracy metrics (status, plan,
and step level), produces actionable mismatch diagnostics, and formats
comprehensive machine-readable (JSON) and human-readable (Markdown) reports.

Usage:
    python3 code/evaluation/main.py --sample
    python3 code/evaluation/main.py --sample --fail-on-mismatch
    python3 code/evaluation/main.py --help
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# Set up module paths
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = REPO_ROOT / "dataset"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

try:
    from identity import IdentityGraph, UserIdentityProfile
    from intent import RequestIntentClassifier
    from evidence import EvidenceRecord, EvidenceStore
    from audit import AuditEngine, AuditResult, DeletionPolicy, DeletionStatus
    from planner import (
        CANONICAL_SYSTEMS,
        OPERATION_ORDER,
        OPERATION_TO_STATE,
        PHASE_ORDER,
        RemediationPlanner,
        canonical_sort_key,
        validate_steps,
    )
except ImportError:
    from code.identity import IdentityGraph, UserIdentityProfile
    from code.intent import RequestIntentClassifier
    from code.evidence import EvidenceRecord, EvidenceStore
    from code.audit import AuditEngine, AuditResult, DeletionPolicy, DeletionStatus
    from code.planner import (
        CANONICAL_SYSTEMS,
        OPERATION_ORDER,
        OPERATION_TO_STATE,
        PHASE_ORDER,
        RemediationPlanner,
        canonical_sort_key,
        validate_steps,
    )


# ---------------------------------------------------------------------------
# Mismatch Categories
# ---------------------------------------------------------------------------

class MismatchCategory(Enum):
    """Actionable root-cause categorization for evaluation mismatches."""
    INPUT_OR_REFERENCE_DATA_ERROR = "INPUT_OR_REFERENCE_DATA_ERROR"
    IDENTITY_RESOLUTION_ERROR = "IDENTITY_RESOLUTION_ERROR"
    INTENT_CLASSIFICATION_ERROR = "INTENT_CLASSIFICATION_ERROR"
    EVIDENCE_COVERAGE_OR_PARSING_ERROR = "EVIDENCE_COVERAGE_OR_PARSING_ERROR"
    TEMPORAL_CUTOFF_ERROR = "TEMPORAL_CUTOFF_ERROR"
    POLICY_AUDIT_ERROR = "POLICY_AUDIT_ERROR"
    REMEDIATION_PLANNING_ERROR = "REMEDIATION_PLANNING_ERROR"
    CANONICAL_SORTING_OR_NUMBERING_ERROR = "CANONICAL_SORTING_OR_NUMBERING_ERROR"
    SCHEMA_OR_SERIALIZATION_ERROR = "SCHEMA_OR_SERIALIZATION_ERROR"
    UNCLASSIFIED_MISMATCH = "UNCLASSIFIED_MISMATCH"


# ---------------------------------------------------------------------------
# Data Classes for Evaluation
# ---------------------------------------------------------------------------

@dataclass
class DatasetIntegrityReport:
    """Integrity checks on input requests and reference ground truth."""
    requests_path: str
    references_path: str
    request_count: int
    reference_count: int
    matched_case_count: int
    missing_in_reference: List[str] = field(default_factory=list)
    unexpected_in_reference: List[str] = field(default_factory=list)
    request_duplicate_ids: List[str] = field(default_factory=list)
    reference_duplicate_ids: List[str] = field(default_factory=list)
    missing_request_columns: List[str] = field(default_factory=list)
    missing_reference_columns: List[str] = field(default_factory=list)
    invalid_reference_json_cases: List[str] = field(default_factory=list)
    is_valid: bool = True
    errors: List[str] = field(default_factory=list)


@dataclass
class CaseEvaluationResult:
    """Detailed evaluation result for a single case."""
    case_id: str
    email: str
    audit_at: str
    ref_status: str
    pred_status: str
    status_match: bool
    plan_match: bool
    full_match: bool
    ref_step_count: int
    pred_step_count: int
    first_diff_step_no: Optional[int] = None
    first_diff_ref_step: Optional[Dict[str, Any]] = None
    first_diff_pred_step: Optional[Dict[str, Any]] = None
    schema_errors: List[str] = field(default_factory=list)
    mismatch_category: Optional[str] = None
    suggested_module: str = ""
    pipeline_exception: Optional[str] = None
    ref_steps: List[Dict[str, Any]] = field(default_factory=list)
    pred_steps: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class EvaluationSummary:
    """Aggregated benchmark metrics across all evaluated cases."""
    total_cases: int
    successful_runs: int
    pipeline_exceptions: int
    status_matches: int
    status_accuracy_pct: float
    exact_plan_matches: int
    exact_plan_accuracy_pct: float
    full_prediction_matches: int
    full_prediction_accuracy_pct: float
    status_match_plan_mismatch: int
    schema_invalid_count: int
    confusion_matrix: Dict[str, Dict[str, int]]
    per_status_metrics: Dict[str, Dict[str, float]]
    total_latency_seconds: float
    mean_latency_ms: float
    mismatch_counts_by_category: Dict[str, int]
    case_results: List[CaseEvaluationResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Data Loading and Dataset Integrity Checks
# ---------------------------------------------------------------------------

REQUIRED_REQUEST_COLUMNS = [
    "case_id",
    "email",
    "requested_at",
    "reported_status",
    "reported_at",
    "audit_at",
    "request_text",
]

REQUIRED_REFERENCE_COLUMNS = [
    "case_id",
    "deletion_status",
    "steps_json",
]


def load_requests(csv_path: Union[str, Path]) -> Tuple[List[Dict[str, str]], List[str]]:
    """Load and validate deletion requests from CSV."""
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(f"Requests file not found: {p}")

    rows: List[Dict[str, str]] = []
    errors: List[str] = []

    with open(p, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return [], [f"Empty CSV or missing header: {p}"]

        missing_cols = [c for c in REQUIRED_REQUEST_COLUMNS if c not in reader.fieldnames]
        if missing_cols:
            errors.append(f"Missing required request columns: {missing_cols}")

        seen_ids: Set[str] = set()
        for i, row in enumerate(reader, start=1):
            cid = row.get("case_id", "").strip()
            if not cid:
                errors.append(f"Row {i} has empty case_id")
                continue
            if cid in seen_ids:
                errors.append(f"Duplicate case_id '{cid}' at row {i}")
            seen_ids.add(cid)
            rows.append(row)

    return rows, errors


def load_reference_outputs(csv_path: Union[str, Path]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Load and parse reference outputs from sample_output.csv."""
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(f"Reference file not found: {p}")

    records: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    with open(p, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return {}, [f"Empty reference CSV: {p}"]

        missing_cols = [c for c in REQUIRED_REFERENCE_COLUMNS if c not in reader.fieldnames]
        if missing_cols:
            errors.append(f"Missing required reference columns: {missing_cols}")

        for i, row in enumerate(reader, start=1):
            cid = row.get("case_id", "").strip()
            if not cid:
                errors.append(f"Reference row {i} has empty case_id")
                continue
            if cid in records:
                errors.append(f"Duplicate reference case_id '{cid}' at row {i}")

            status = row.get("deletion_status", "").strip().upper()
            raw_json = row.get("steps_json", "").strip()

            try:
                steps = json.loads(raw_json)
                if not isinstance(steps, list):
                    errors.append(f"Reference case '{cid}' steps_json is not a JSON list")
                    steps = []
            except json.JSONDecodeError as ex:
                errors.append(f"Reference case '{cid}' has invalid JSON in steps_json: {ex}")
                steps = []

            records[cid] = {
                "case_id": cid,
                "deletion_status": status,
                "steps_json": raw_json,
                "steps": steps,
            }

    return records, errors


def verify_dataset_integrity(
    requests_path: Union[str, Path],
    references_path: Union[str, Path],
) -> DatasetIntegrityReport:
    """Perform comprehensive cross-file dataset integrity validation."""
    req_rows, req_errors = load_requests(requests_path)
    ref_dict, ref_errors = load_reference_outputs(references_path)

    req_ids = [r["case_id"] for r in req_rows if "case_id" in r]
    ref_ids = list(ref_dict.keys())

    req_set = set(req_ids)
    ref_set = set(ref_ids)

    missing_in_ref = sorted(list(req_set - ref_set))
    unexpected_in_ref = sorted(list(ref_set - req_set))

    # Detect duplicates
    req_dup = [cid for cid in req_set if req_ids.count(cid) > 1]
    ref_dup = [cid for cid in ref_set if ref_ids.count(cid) > 1]

    errors = list(req_errors) + list(ref_errors)
    if missing_in_ref:
        errors.append(f"{len(missing_in_ref)} request cases missing in reference outputs")
    if unexpected_in_ref:
        errors.append(f"{len(unexpected_in_ref)} reference cases not found in requests")

    report = DatasetIntegrityReport(
        requests_path=str(requests_path),
        references_path=str(references_path),
        request_count=len(req_rows),
        reference_count=len(ref_dict),
        matched_case_count=len(req_set & ref_set),
        missing_in_reference=missing_in_ref,
        unexpected_in_reference=unexpected_in_ref,
        request_duplicate_ids=req_dup,
        reference_duplicate_ids=ref_dup,
        is_valid=len(errors) == 0,
        errors=errors,
    )
    return report


# ---------------------------------------------------------------------------
# Pipeline Builder and In-Memory Prediction
# ---------------------------------------------------------------------------

def build_pipeline(
    dataset_dir: Union[str, Path] = DATASET_DIR,
    identities_csv: Optional[Union[str, Path]] = None,
    audit_log_csv: Optional[Union[str, Path]] = None,
    deletion_rules_csv: Optional[Union[str, Path]] = None,
    system_activity_csv: Optional[Union[str, Path]] = None,
) -> Tuple[AuditEngine, RemediationPlanner]:
    """Initialize end-to-end audit engine and remediation planner."""
    d_dir = Path(dataset_dir)
    id_csv = Path(identities_csv) if identities_csv else d_dir / "user_identities.csv"
    audit_csv = Path(audit_log_csv) if audit_log_csv else d_dir / "user_audit_log.csv"
    rules_csv = Path(deletion_rules_csv) if deletion_rules_csv else d_dir / "deletion_rules.csv"
    act_csv = Path(system_activity_csv) if system_activity_csv else d_dir / "system_activity.csv"

    graph = IdentityGraph(id_csv, audit_csv)
    store = EvidenceStore(d_dir)
    policy = DeletionPolicy(rules_csv)
    engine = AuditEngine(
        policy=policy,
        identity_graph=graph,
        evidence_store=store,
        system_activity_path=act_csv,
    )

    known_eids = set(store.manifest.keys())
    for r in store.records:
        known_eids.add(r.source_evidence_id)
    planner = RemediationPlanner(known_evidence_ids=known_eids)

    return engine, planner


def predict_case(
    engine: AuditEngine,
    planner: RemediationPlanner,
    case_row: Dict[str, str],
) -> Dict[str, Any]:
    """Execute end-to-end pipeline in-memory for a single deletion request."""
    cid = case_row["case_id"]
    email = case_row["email"]
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
        "audit_result": res,
    }


# ---------------------------------------------------------------------------
# Prediction Validation
# ---------------------------------------------------------------------------

def validate_prediction(
    prediction: Dict[str, Any],
    known_evidence_ids: Optional[Set[str]] = None,
) -> List[str]:
    """Validate prediction top-level schema and internal steps_json invariants."""
    errors: List[str] = []
    cid = prediction.get("case_id", "UNKNOWN")

    for req_field in ["case_id", "deletion_status", "steps"]:
        if req_field not in prediction:
            errors.append(f"Case {cid} missing top-level field '{req_field}'")

    status = str(prediction.get("deletion_status", "")).strip().upper()
    if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
        errors.append(f"Case {cid} has invalid deletion_status '{status}'")

    steps = prediction.get("steps")
    if not isinstance(steps, list):
        errors.append(f"Case {cid} steps is not a list")
        return errors

    step_errors = validate_steps(steps, status=status, known_evidence_ids=known_evidence_ids)
    for err in step_errors:
        errors.append(f"Case {cid}: {err}")

    return errors


# ---------------------------------------------------------------------------
# Diagnostics & Root Cause Classification
# ---------------------------------------------------------------------------

def classify_mismatch(
    case_row: Dict[str, str],
    ref: Dict[str, Any],
    pred: Dict[str, Any],
    schema_errors: List[str],
) -> Tuple[MismatchCategory, str]:
    """Diagnose the root cause category and recommend the module to inspect."""
    if schema_errors:
        return (
            MismatchCategory.SCHEMA_OR_SERIALIZATION_ERROR,
            "code/planner.py (validate_steps / schema formatting)",
        )

    ref_status = ref.get("deletion_status", "")
    pred_status = pred.get("deletion_status", "")

    # Status Mismatch
    if ref_status != pred_status:
        if ref_status == "NOT_APPLICABLE" or pred_status == "NOT_APPLICABLE":
            return (
                MismatchCategory.INTENT_CLASSIFICATION_ERROR,
                "code/intent.py (RequestIntentClassifier)",
            )
        return (
            MismatchCategory.POLICY_AUDIT_ERROR,
            "code/audit.py (AuditEngine.audit_case)",
        )

    # Step Mismatch
    ref_steps = ref.get("steps", [])
    pred_steps = pred.get("steps", [])

    if len(ref_steps) != len(pred_steps):
        # Look for missing resource in evidence
        ref_systems = {s.get("system_id") for s in ref_steps}
        pred_systems = {s.get("system_id") for s in pred_steps}
        if ref_systems != pred_systems:
            return (
                MismatchCategory.EVIDENCE_COVERAGE_OR_PARSING_ERROR,
                "code/evidence.py (EvidenceStore ingestion & parsing)",
            )
        return (
            MismatchCategory.POLICY_AUDIT_ERROR,
            "code/audit.py (AuditEngine resource evaluation)",
        )

    # Content difference in steps
    for i, (r_s, p_s) in enumerate(zip(ref_steps, pred_steps), start=1):
        if r_s != p_s:
            if r_s.get("phase") != p_s.get("phase") or r_s.get("operation") != p_s.get("operation"):
                return (
                    MismatchCategory.CANONICAL_SORTING_OR_NUMBERING_ERROR,
                    "code/planner.py (canonical_sort_key / operation priority)",
                )
            if r_s.get("evidence_id") != p_s.get("evidence_id"):
                return (
                    MismatchCategory.EVIDENCE_COVERAGE_OR_PARSING_ERROR,
                    "code/evidence.py (EvidenceRecord attribution / recency)",
                )
            if r_s.get("resource_id") != p_s.get("resource_id") or r_s.get("system_id") != p_s.get("system_id"):
                return (
                    MismatchCategory.IDENTITY_RESOLUTION_ERROR,
                    "code/identity.py (IdentityGraph resolution)",
                )

    return (MismatchCategory.UNCLASSIFIED_MISMATCH, "code/audit.py or code/planner.py")


# ---------------------------------------------------------------------------
# Metrics and Comparison Engine
# ---------------------------------------------------------------------------

def calculate_confusion_matrix(
    predictions_by_case: Dict[str, Dict[str, Any]],
    references_by_case: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Dict[str, float]]]:
    """Calculate 3x3 confusion matrix and per-class precision, recall, and F1."""
    statuses = ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"]
    matrix: Dict[str, Dict[str, int]] = {s: {p: 0 for p in statuses} for s in statuses}

    for cid, ref in references_by_case.items():
        if cid not in predictions_by_case:
            continue
        r_st = ref["deletion_status"]
        p_st = predictions_by_case[cid]["deletion_status"]
        if r_st in matrix and p_st in matrix[r_st]:
            matrix[r_st][p_st] += 1

    per_status: Dict[str, Dict[str, float]] = {}
    for st in statuses:
        tp = matrix[st][st]
        fp = sum(matrix[other][st] for other in statuses if other != st)
        fn = sum(matrix[st][other] for other in statuses if other != st)

        prec = (tp / (tp + fp)) if (tp + fp) > 0 else (1.0 if tp == 0 and fp == 0 else 0.0)
        rec = (tp / (tp + fn)) if (tp + fn) > 0 else (1.0 if tp == 0 and fn == 0 else 0.0)
        f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0

        per_status[st] = {
            "tp": float(tp),
            "fp": float(fp),
            "fn": float(fn),
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(f1, 4),
        }

    return matrix, per_status


def compare_predictions(
    requests: List[Dict[str, str]],
    references_by_case: Dict[str, Dict[str, Any]],
    predictions_by_case: Dict[str, Dict[str, Any]],
    validation_errors_by_case: Dict[str, List[str]],
    pipeline_exceptions: Dict[str, str],
    total_elapsed_seconds: float,
) -> EvaluationSummary:
    """Perform comprehensive comparison and generate EvaluationSummary."""
    total_cases = len(requests)
    status_matches = 0
    exact_plan_matches = 0
    full_prediction_matches = 0
    status_match_plan_mismatch = 0
    schema_invalid_count = 0
    mismatch_counts: Dict[str, int] = {c.value: 0 for c in MismatchCategory}
    case_results: List[CaseEvaluationResult] = []

    for req in requests:
        cid = req["case_id"]
        ref = references_by_case.get(cid, {})
        pred = predictions_by_case.get(cid, {})
        schema_errs = validation_errors_by_case.get(cid, [])
        exc = pipeline_exceptions.get(cid)

        if schema_errs:
            schema_invalid_count += 1

        ref_st = ref.get("deletion_status", "UNKNOWN")
        pred_st = pred.get("deletion_status", "EXCEPTION" if exc else "MISSING")
        ref_steps = ref.get("steps", [])
        pred_steps = pred.get("steps", [])

        st_match = (ref_st == pred_st)
        pl_match = (ref_steps == pred_steps)
        fl_match = st_match and pl_match and not schema_errs and not exc

        if st_match:
            status_matches += 1
        if pl_match:
            exact_plan_matches += 1
        if fl_match:
            full_prediction_matches += 1
        if st_match and not pl_match:
            status_match_plan_mismatch += 1

        diff_idx: Optional[int] = None
        diff_ref: Optional[Dict[str, Any]] = None
        diff_pred: Optional[Dict[str, Any]] = None
        mismatch_cat: Optional[str] = None
        suggested_mod = ""

        if not fl_match:
            # Locate first differing step
            min_len = min(len(ref_steps), len(pred_steps))
            for i in range(min_len):
                if ref_steps[i] != pred_steps[i]:
                    diff_idx = i + 1
                    diff_ref = ref_steps[i]
                    diff_pred = pred_steps[i]
                    break
            if diff_idx is None and len(ref_steps) != len(pred_steps):
                diff_idx = min_len + 1
                diff_ref = ref_steps[min_len] if len(ref_steps) > min_len else None
                diff_pred = pred_steps[min_len] if len(pred_steps) > min_len else None

            cat, mod = classify_mismatch(req, ref, pred, schema_errs)
            mismatch_cat = cat.value
            suggested_mod = mod
            mismatch_counts[mismatch_cat] += 1

        case_results.append(
            CaseEvaluationResult(
                case_id=cid,
                email=req.get("email", ""),
                audit_at=req.get("audit_at", ""),
                ref_status=ref_st,
                pred_status=pred_st,
                status_match=st_match,
                plan_match=pl_match,
                full_match=fl_match,
                ref_step_count=len(ref_steps),
                pred_step_count=len(pred_steps),
                first_diff_step_no=diff_idx,
                first_diff_ref_step=diff_ref,
                first_diff_pred_step=diff_pred,
                schema_errors=schema_errs,
                mismatch_category=mismatch_cat,
                suggested_module=suggested_mod,
                pipeline_exception=exc,
                ref_steps=ref_steps,
                pred_steps=pred_steps,
            )
        )

    matrix, per_status = calculate_confusion_matrix(predictions_by_case, references_by_case)

    successful_runs = total_cases - len(pipeline_exceptions)
    mean_lat = (total_elapsed_seconds / successful_runs * 1000) if successful_runs > 0 else 0.0

    summary = EvaluationSummary(
        total_cases=total_cases,
        successful_runs=successful_runs,
        pipeline_exceptions=len(pipeline_exceptions),
        status_matches=status_matches,
        status_accuracy_pct=round(status_matches / total_cases * 100, 2) if total_cases > 0 else 0.0,
        exact_plan_matches=exact_plan_matches,
        exact_plan_accuracy_pct=round(exact_plan_matches / total_cases * 100, 2) if total_cases > 0 else 0.0,
        full_prediction_matches=full_prediction_matches,
        full_prediction_accuracy_pct=round(full_prediction_matches / total_cases * 100, 2) if total_cases > 0 else 0.0,
        status_match_plan_mismatch=status_match_plan_mismatch,
        schema_invalid_count=schema_invalid_count,
        confusion_matrix=matrix,
        per_status_metrics=per_status,
        total_latency_seconds=round(total_elapsed_seconds, 3),
        mean_latency_ms=round(mean_lat, 2),
        mismatch_counts_by_category=mismatch_counts,
        case_results=case_results,
    )
    return summary


# ---------------------------------------------------------------------------
# Report Generation (JSON & Markdown)
# ---------------------------------------------------------------------------

def generate_markdown_report(
    summary: EvaluationSummary,
    integrity: DatasetIntegrityReport,
) -> str:
    """Format human-readable GitHub-flavored Markdown evaluation report."""
    md = []
    md.append("# Sample Benchmark and Evaluation Report")
    md.append(f"\n**Generated**: {datetime.utcnow().isoformat()}Z  ")
    md.append("**Competition**: HackerRank Orchestrate (October 2026) — *Ghost Data Still Exists*  ")
    md.append(f"**Total Cases**: {summary.total_cases} | **Elapsed Time**: {summary.total_latency_seconds}s ({summary.mean_latency_ms} ms/case)\n")

    md.append("## 1. Executive Summary\n")
    md.append("| Metric | Count | Percentage |")
    md.append("| :--- | :---: | :---: |")
    md.append(f"| **Status Agreement** | {summary.status_matches} / {summary.total_cases} | **{summary.status_accuracy_pct}%** |")
    md.append(f"| **Exact steps_json Match** | {summary.exact_plan_matches} / {summary.total_cases} | **{summary.exact_plan_accuracy_pct}%** |")
    md.append(f"| **Full Prediction Match** | {summary.full_prediction_matches} / {summary.total_cases} | **{summary.full_prediction_accuracy_pct}%** |")
    md.append(f"| Status Match / Plan Mismatch | {summary.status_match_plan_mismatch} / {summary.total_cases} | {summary.status_match_plan_mismatch / summary.total_cases * 100:.1f}% |")
    md.append(f"| Schema Invalid Predictions | {summary.schema_invalid_count} | {summary.schema_invalid_count / summary.total_cases * 100:.1f}% |")
    md.append(f"| Pipeline Exceptions | {summary.pipeline_exceptions} | {summary.pipeline_exceptions / summary.total_cases * 100:.1f}% |\n")

    md.append("## 2. Dataset Integrity\n")
    md.append(f"- **Requests File**: `{integrity.requests_path}` ({integrity.request_count} rows)")
    md.append(f"- **Reference File**: `{integrity.references_path}` ({integrity.reference_count} rows)")
    md.append(f"- **Matched Case IDs**: {integrity.matched_case_count} (0 missing, 0 unexpected)")
    md.append(f"- **Integrity Status**: {'PASSED (Zero cross-file anomalies)' if integrity.is_valid else 'FAILED'}\n")

    md.append("## 3. Status Agreement & Confusion Matrix\n")
    md.append("| Reference \\ Predicted | COMPLETE | INCOMPLETE | NOT_APPLICABLE |")
    md.append("| :--- | :---: | :---: | :---: |")
    for r_st in ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"]:
        c = summary.confusion_matrix[r_st]
        md.append(f"| **{r_st}** | {c['COMPLETE']} | {c['INCOMPLETE']} | {c['NOT_APPLICABLE']} |")
    md.append("")

    md.append("### Per-Status Classification Metrics\n")
    md.append("| Status | Precision | Recall | F1 Score | Support |")
    md.append("| :--- | :---: | :---: | :---: | :---: |")
    for st, m in summary.per_status_metrics.items():
        support = int(m['tp'] + m['fn'])
        md.append(f"| `{st}` | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {support} |")
    md.append("")

    md.append("## 4. Mismatch Diagnostics Summary\n")
    active_mismatches = {k: v for k, v in summary.mismatch_counts_by_category.items() if v > 0}
    if not active_mismatches:
        md.append("🎉 **Zero mismatches detected.** All generated predictions match reference data exactly!\n")
    else:
        md.append("| Mismatch Category | Affected Cases | Recommended Inspection |")
        md.append("| :--- | :---: | :--- |")
        for cat, cnt in sorted(active_mismatches.items(), key=lambda x: -x[1]):
            rec = "code/audit.py or code/planner.py"
            if "EVIDENCE" in cat:
                rec = "`code/evidence.py`"
            elif "INTENT" in cat:
                rec = "`code/intent.py`"
            elif "IDENTITY" in cat:
                rec = "`code/identity.py`"
            elif "SORTING" in cat:
                rec = "`code/planner.py`"
            md.append(f"| `{cat}` | {cnt} | {rec} |")
        md.append("")

    md.append("## 5. Detailed Case-by-Case Breakdown\n")
    md.append("| Case ID | Status Match | Plan Match | Ref Steps | Pred Steps | Diagnostics / Mismatch |")
    md.append("| :--- | :---: | :---: | :---: | :---: | :--- |")
    for cr in summary.case_results:
        sm = "✅ PASS" if cr.status_match else "❌ FAIL"
        pm = "✅ PASS" if cr.plan_match else "⚠️ DIFF"
        diff_info = "Exact match"
        if not cr.plan_match:
            diff_info = f"Diff at step {cr.first_diff_step_no} ({cr.mismatch_category})"
        if cr.pipeline_exception:
            diff_info = f"Exception: {cr.pipeline_exception}"
        md.append(f"| `{cr.case_id}` | {sm} | {pm} | {cr.ref_step_count} | {cr.pred_step_count} | {diff_info} |")

    return "\n".join(md)


def generate_json_report(
    summary: EvaluationSummary,
    integrity: DatasetIntegrityReport,
) -> Dict[str, Any]:
    """Format structured machine-readable JSON evaluation report."""
    report_dict: Dict[str, Any] = {
        "metadata": {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "benchmark_suite": "sample_deletion_requests",
            "evaluator_version": "1.0.0",
        },
        "dataset_integrity": asdict(integrity),
        "metrics": {
            "total_cases": summary.total_cases,
            "successful_runs": summary.successful_runs,
            "pipeline_exceptions": summary.pipeline_exceptions,
            "status_accuracy_pct": summary.status_accuracy_pct,
            "exact_plan_accuracy_pct": summary.exact_plan_accuracy_pct,
            "full_prediction_accuracy_pct": summary.full_prediction_accuracy_pct,
            "status_match_plan_mismatch": summary.status_match_plan_mismatch,
            "schema_invalid_count": summary.schema_invalid_count,
            "total_latency_seconds": summary.total_latency_seconds,
            "mean_latency_ms": summary.mean_latency_ms,
            "confusion_matrix": summary.confusion_matrix,
            "per_status_metrics": summary.per_status_metrics,
            "mismatch_counts_by_category": summary.mismatch_counts_by_category,
        },
        "cases": [
            {
                "case_id": cr.case_id,
                "email": cr.email,
                "ref_status": cr.ref_status,
                "pred_status": cr.pred_status,
                "status_match": cr.status_match,
                "plan_match": cr.plan_match,
                "full_match": cr.full_match,
                "ref_step_count": cr.ref_step_count,
                "pred_step_count": cr.pred_step_count,
                "first_diff_step_no": cr.first_diff_step_no,
                "mismatch_category": cr.mismatch_category,
                "suggested_module": cr.suggested_module,
                "schema_errors": cr.schema_errors,
                "pipeline_exception": cr.pipeline_exception,
            }
            for cr in summary.case_results
        ],
    }
    return report_dict


# ---------------------------------------------------------------------------
# Sample Benchmark Runner
# ---------------------------------------------------------------------------

def run_sample_benchmark(
    requests_path: Union[str, Path] = DATASET_DIR / "sample_deletion_requests.csv",
    references_path: Union[str, Path] = DATASET_DIR / "sample_output.csv",
    dataset_dir: Union[str, Path] = DATASET_DIR,
    report_dir: Optional[Union[str, Path]] = None,
    save_reports: bool = True,
    quiet: bool = False,
) -> Tuple[EvaluationSummary, DatasetIntegrityReport]:
    """Execute complete in-memory sample evaluation benchmark."""
    integrity = verify_dataset_integrity(requests_path, references_path)
    if not integrity.is_valid and integrity.request_count == 0:
        raise ValueError(f"Dataset integrity verification failed: {integrity.errors}")

    req_rows, _ = load_requests(requests_path)
    ref_dict, _ = load_reference_outputs(references_path)

    # Build production pipeline
    engine, planner = build_pipeline(dataset_dir)

    predictions_by_case: Dict[str, Dict[str, Any]] = {}
    validation_errors_by_case: Dict[str, List[str]] = {}
    pipeline_exceptions: Dict[str, str] = {}

    t_start = time.time()
    for row in req_rows:
        cid = row["case_id"]
        try:
            pred = predict_case(engine, planner, row)
            predictions_by_case[cid] = pred
            v_errs = validate_prediction(pred, known_evidence_ids=planner.known_evidence_ids)
            if v_errs:
                validation_errors_by_case[cid] = v_errs
        except Exception as ex:
            pipeline_exceptions[cid] = f"{type(ex).__name__}: {str(ex)}"

    t_elapsed = time.time() - t_start

    summary = compare_predictions(
        requests=req_rows,
        references_by_case=ref_dict,
        predictions_by_case=predictions_by_case,
        validation_errors_by_case=validation_errors_by_case,
        pipeline_exceptions=pipeline_exceptions,
        total_elapsed_seconds=t_elapsed,
    )

    if save_reports:
        rep_dir = Path(report_dir) if report_dir else CODE_DIR / "evaluation" / "reports"
        rep_dir.mkdir(parents=True, exist_ok=True)

        json_path = rep_dir / "sample_benchmark.json"
        md_path = rep_dir / "sample_benchmark.md"

        json_data = generate_json_report(summary, integrity)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2)

        md_content = generate_markdown_report(summary, integrity)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)

    return summary, integrity


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def print_terminal_summary(summary: EvaluationSummary, integrity: DatasetIntegrityReport) -> None:
    """Print structured summary to stdout."""
    print("=" * 70)
    print(" GHOST DATA STILL EXISTS — SAMPLE BENCHMARK EVALUATION")
    print("=" * 70)
    print(f"Dataset: {integrity.requests_path}")
    print(f"Sample Requests Evaluated: {summary.total_cases} | Reference Rows: {integrity.reference_count}")
    print(f"Elapsed Time: {summary.total_latency_seconds:.2f}s ({summary.mean_latency_ms:.1f} ms/case)")
    print("-" * 70)
    print(f"Status Agreement:            {summary.status_matches} / {summary.total_cases} ({summary.status_accuracy_pct:.1f}%)")
    print(f"Exact steps_json Matches:    {summary.exact_plan_matches} / {summary.total_cases} ({summary.exact_plan_accuracy_pct:.1f}%)")
    print(f"Full Prediction Matches:     {summary.full_prediction_matches} / {summary.total_cases} ({summary.full_prediction_accuracy_pct:.1f}%)")
    print(f"Status Match / Plan Diff:    {summary.status_match_plan_mismatch} / {summary.total_cases}")
    print(f"Schema Validation Failures:  {summary.schema_invalid_count}")
    print(f"Pipeline Exceptions:         {summary.pipeline_exceptions}")
    print("-" * 70)
    print("Status Confusion Matrix:")
    header_ref = "Ref \\ Pred"
    print(f"  {header_ref:<15} {'COMPLETE':<12} {'INCOMPLETE':<12} {'NOT_APPLICABLE':<15}")
    for r_st in ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"]:
        c = summary.confusion_matrix[r_st]
        print(f"  {r_st:<15} {c['COMPLETE']:<12} {c['INCOMPLETE']:<12} {c['NOT_APPLICABLE']:<15}")
    print("-" * 70)

    overall_result = "PASS" if summary.full_prediction_matches == summary.total_cases else (
        "PARTIAL (Status 100%, Step Differences Identified)" if summary.status_accuracy_pct == 100.0 else "FAIL"
    )
    print(f"OVERALL EVALUATION RESULT: {overall_result}")
    print("=" * 70)


def main() -> int:
    """CLI entry point for code/evaluation/main.py."""
    parser = argparse.ArgumentParser(
        description="Ghost Data Still Exists — Sample Benchmark Evaluation Validator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Run benchmark against official sample dataset (dataset/sample_deletion_requests.csv)",
    )
    parser.add_argument(
        "--requests",
        type=str,
        default=str(DATASET_DIR / "sample_deletion_requests.csv"),
        help="Path to deletion requests CSV",
    )
    parser.add_argument(
        "--reference",
        type=str,
        default=str(DATASET_DIR / "sample_output.csv"),
        help="Path to ground truth reference CSV",
    )
    parser.add_argument(
        "--dataset-dir",
        type=str,
        default=str(DATASET_DIR),
        help="Directory containing evidence, identities, and policies",
    )
    parser.add_argument(
        "--report-dir",
        type=str,
        default=str(CODE_DIR / "evaluation" / "reports"),
        help="Directory where sample_benchmark.json and sample_benchmark.md are saved",
    )
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Return non-zero exit code if status or plan agreement is not 100%%",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress terminal summary output",
    )

    args = parser.parse_args()

    if not args.sample and not sys.argv[1:]:
        parser.print_help()
        return 0

    try:
        summary, integrity = run_sample_benchmark(
            requests_path=args.requests,
            references_path=args.reference,
            dataset_dir=args.dataset_dir,
            report_dir=args.report_dir,
            save_reports=True,
            quiet=args.quiet,
        )

        if not args.quiet:
            print_terminal_summary(summary, integrity)
            print(f"Reports saved to: {args.report_dir}/")
            print(f"  - {args.report_dir}/sample_benchmark.json")
            print(f"  - {args.report_dir}/sample_benchmark.md")

        if args.fail_on_mismatch and summary.full_prediction_matches != summary.total_cases:
            return 1

        return 0
    except Exception as ex:
        print(f"Evaluation error: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
