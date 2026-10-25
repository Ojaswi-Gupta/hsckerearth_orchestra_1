"""Comprehensive Unit Tests for Sample Benchmark and Evaluation Validator (code/evaluation/main.py).

Tests:
1. CSV loading for requests and reference outputs (valid paths, missing files, malformed JSON, missing columns, duplicates).
2. Cross-file dataset integrity verification (matched case IDs, missing IDs, unexpected IDs, duplicates).
3. Prediction validation (schema compliance, status validation, steps_json lifecycle invariants).
4. Confusion matrix and metric computation (3x3 matrix, TP/FP/FN, precision, recall, F1, zero-division safety).
5. Mismatch diagnostic classification across official root-cause categories.
6. Report generation (Markdown and JSON schema compliance).
7. End-to-end benchmark orchestration and determinism.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = REPO_ROOT / "dataset"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from evaluation.main import (
    CaseEvaluationResult,
    DatasetIntegrityReport,
    EvaluationSummary,
    MismatchCategory,
    calculate_confusion_matrix,
    classify_mismatch,
    compare_predictions,
    generate_json_report,
    generate_markdown_report,
    load_reference_outputs,
    load_requests,
    run_sample_benchmark,
    validate_prediction,
    verify_dataset_integrity,
)


class TestDatasetLoading(unittest.TestCase):
    """Test loading and validation of input requests and reference ground truth CSVs."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.sample_requests_path = DATASET_DIR / "sample_deletion_requests.csv"
        self.sample_output_path = DATASET_DIR / "sample_output.csv"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_requests_valid(self) -> None:
        """Verify loading official sample_deletion_requests.csv."""
        rows, errors = load_requests(self.sample_requests_path)
        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(rows), 32)
        first = rows[0]
        self.assertEqual(first["case_id"], "case_01")
        self.assertIn("email", first)
        self.assertIn("audit_at", first)

    def test_load_requests_file_not_found(self) -> None:
        """Verify FileNotFoundError on nonexistent requests file."""
        nonexistent = Path(self.temp_dir) / "does_not_exist.csv"
        with self.assertRaises(FileNotFoundError):
            load_requests(nonexistent)

    def test_load_requests_missing_columns(self) -> None:
        """Verify error when required columns are missing in requests CSV."""
        bad_csv = Path(self.temp_dir) / "bad_requests.csv"
        bad_csv.write_text("case_id,email\ncase_01,test@example.com\n", encoding="utf-8")
        rows, errors = load_requests(bad_csv)
        self.assertTrue(any("Missing required request columns" in e for e in errors))

    def test_load_requests_duplicate_case_id(self) -> None:
        """Verify detection of duplicate case IDs in requests."""
        dup_csv = Path(self.temp_dir) / "dup_requests.csv"
        dup_csv.write_text(
            "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text\n"
            "case_01,a@test.com,2026-01-01T00:00:00Z,COMPLETE,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z,Delete me\n"
            "case_01,b@test.com,2026-01-01T00:00:00Z,COMPLETE,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z,Delete me\n",
            encoding="utf-8",
        )
        rows, errors = load_requests(dup_csv)
        self.assertTrue(any("Duplicate case_id 'case_01'" in e for e in errors))

    def test_load_reference_outputs_valid(self) -> None:
        """Verify loading official sample_output.csv."""
        records, errors = load_reference_outputs(self.sample_output_path)
        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        self.assertEqual(len(records), 32)
        self.assertIn("case_01", records)
        first = records["case_01"]
        self.assertIn("deletion_status", first)
        self.assertIn("steps", first)
        self.assertIsInstance(first["steps"], list)

    def test_load_reference_outputs_file_not_found(self) -> None:
        """Verify FileNotFoundError on nonexistent reference file."""
        nonexistent = Path(self.temp_dir) / "does_not_exist.csv"
        with self.assertRaises(FileNotFoundError):
            load_reference_outputs(nonexistent)

    def test_load_reference_outputs_invalid_json(self) -> None:
        """Verify handling of invalid JSON in steps_json."""
        bad_csv = Path(self.temp_dir) / "bad_ref.csv"
        bad_csv.write_text(
            'case_id,deletion_status,steps_json\ncase_01,COMPLETE,"{invalid_json"\n',
            encoding="utf-8",
        )
        records, errors = load_reference_outputs(bad_csv)
        self.assertTrue(any("invalid JSON in steps_json" in e for e in errors))
        self.assertEqual(records["case_01"]["steps"], [])

    def test_load_reference_outputs_non_list_json(self) -> None:
        """Verify handling of non-list JSON in steps_json."""
        bad_csv = Path(self.temp_dir) / "non_list_ref.csv"
        bad_csv.write_text(
            'case_id,deletion_status,steps_json\ncase_01,COMPLETE,"{""key"": ""val""}"\n',
            encoding="utf-8",
        )
        records, errors = load_reference_outputs(bad_csv)
        self.assertTrue(any("not a JSON list" in e for e in errors))


class TestDatasetIntegrity(unittest.TestCase):
    """Test cross-file dataset integrity checks."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.sample_requests_path = DATASET_DIR / "sample_deletion_requests.csv"
        self.sample_output_path = DATASET_DIR / "sample_output.csv"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_verify_dataset_integrity_sample_pass(self) -> None:
        """Verify official sample datasets pass integrity verification cleanly."""
        report = verify_dataset_integrity(self.sample_requests_path, self.sample_output_path)
        self.assertTrue(report.is_valid)
        self.assertEqual(report.request_count, 32)
        self.assertEqual(report.reference_count, 32)
        self.assertEqual(report.matched_case_count, 32)
        self.assertEqual(len(report.missing_in_reference), 0)
        self.assertEqual(len(report.unexpected_in_reference), 0)
        self.assertEqual(len(report.errors), 0)

    def test_verify_dataset_integrity_missing_in_reference(self) -> None:
        """Verify detection of request case missing in reference outputs."""
        req_csv = Path(self.temp_dir) / "req.csv"
        ref_csv = Path(self.temp_dir) / "ref.csv"

        req_csv.write_text(
            "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text\n"
            "case_01,a@test.com,2026-01-01T00:00:00Z,COMPLETE,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z,Delete me\n"
            "case_02,b@test.com,2026-01-01T00:00:00Z,COMPLETE,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z,Delete me\n",
            encoding="utf-8",
        )
        ref_csv.write_text(
            'case_id,deletion_status,steps_json\ncase_01,COMPLETE,"[]"\n',
            encoding="utf-8",
        )

        report = verify_dataset_integrity(req_csv, ref_csv)
        self.assertFalse(report.is_valid)
        self.assertIn("case_02", report.missing_in_reference)
        self.assertEqual(report.matched_case_count, 1)

    def test_verify_dataset_integrity_unexpected_in_reference(self) -> None:
        """Verify detection of reference case not present in requests."""
        req_csv = Path(self.temp_dir) / "req.csv"
        ref_csv = Path(self.temp_dir) / "ref.csv"

        req_csv.write_text(
            "case_id,email,requested_at,reported_status,reported_at,audit_at,request_text\n"
            "case_01,a@test.com,2026-01-01T00:00:00Z,COMPLETE,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z,Delete me\n",
            encoding="utf-8",
        )
        ref_csv.write_text(
            'case_id,deletion_status,steps_json\ncase_01,COMPLETE,"[]"\ncase_99,COMPLETE,"[]"\n',
            encoding="utf-8",
        )

        report = verify_dataset_integrity(req_csv, ref_csv)
        self.assertFalse(report.is_valid)
        self.assertIn("case_99", report.unexpected_in_reference)


class TestPredictionValidation(unittest.TestCase):
    """Test validation of prediction objects and steps_json structures."""

    def test_validate_prediction_valid_complete(self) -> None:
        """Verify valid COMPLETE prediction passes with zero errors."""
        pred = {
            "case_id": "case_01",
            "deletion_status": "COMPLETE",
            "steps": [
                {
                    "step_no": 1,
                    "phase": "VERIFICATION",
                    "system_id": "account_service",
                    "resource_id": "account_101",
                    "operation": "VERIFY_RECORD_ABSENT",
                    "state": "ABSENT",
                    "evidence_id": "evidence_01",
                }
            ],
        }
        errors = validate_prediction(pred, known_evidence_ids={"evidence_01"})
        self.assertEqual(len(errors), 0, f"Unexpected validation errors: {errors}")

    def test_validate_prediction_valid_not_applicable(self) -> None:
        """Verify valid NOT_APPLICABLE prediction with empty steps passes."""
        pred = {
            "case_id": "case_10",
            "deletion_status": "NOT_APPLICABLE",
            "steps": [],
        }
        errors = validate_prediction(pred)
        self.assertEqual(len(errors), 0)

    def test_validate_prediction_not_applicable_non_empty_fails(self) -> None:
        """Verify NOT_APPLICABLE with non-empty steps fails validation."""
        pred = {
            "case_id": "case_10",
            "deletion_status": "NOT_APPLICABLE",
            "steps": [
                {
                    "step_no": 1,
                    "phase": "VERIFICATION",
                    "system_id": "account_service",
                    "resource_id": "account_101",
                    "operation": "VERIFY_RECORD_ABSENT",
                    "state": "ABSENT",
                    "evidence_id": "evidence_01",
                }
            ],
        }
        errors = validate_prediction(pred)
        self.assertTrue(any("empty steps_json" in e for e in errors))

    def test_validate_prediction_missing_fields(self) -> None:
        """Verify error when required top-level fields are missing."""
        pred = {"case_id": "case_01"}
        errors = validate_prediction(pred)
        self.assertTrue(any("missing top-level field 'deletion_status'" in e for e in errors))
        self.assertTrue(any("missing top-level field 'steps'" in e for e in errors))

    def test_validate_prediction_invalid_status(self) -> None:
        """Verify error on invalid deletion status."""
        pred = {
            "case_id": "case_01",
            "deletion_status": "PENDING",
            "steps": [],
        }
        errors = validate_prediction(pred)
        self.assertTrue(any("invalid deletion_status 'PENDING'" in e for e in errors))

    def test_validate_prediction_step_invariant_violation(self) -> None:
        """Verify finding with empty evidence_id is rejected."""
        pred = {
            "case_id": "case_02",
            "deletion_status": "INCOMPLETE",
            "steps": [
                {
                    "step_no": 1,
                    "phase": "FINDING",
                    "system_id": "account_service",
                    "resource_id": "account_101",
                    "operation": "REPORT_REMAINING_RECORD",
                    "state": "PRESENT",
                    "evidence_id": "",  # Violation: FINDING must have evidence_id
                }
            ],
        }
        errors = validate_prediction(pred)
        self.assertTrue(any("must cite an evidence_id" in e for e in errors))


class TestConfusionMatrixAndMetrics(unittest.TestCase):
    """Test 3x3 confusion matrix calculation and precision/recall/F1 handling."""

    def test_confusion_matrix_perfect_match(self) -> None:
        """Verify confusion matrix and metrics when all predictions match references."""
        refs = {
            "c1": {"deletion_status": "COMPLETE"},
            "c2": {"deletion_status": "INCOMPLETE"},
            "c3": {"deletion_status": "NOT_APPLICABLE"},
        }
        preds = {
            "c1": {"deletion_status": "COMPLETE"},
            "c2": {"deletion_status": "INCOMPLETE"},
            "c3": {"deletion_status": "NOT_APPLICABLE"},
        }
        matrix, metrics = calculate_confusion_matrix(preds, refs)

        self.assertEqual(matrix["COMPLETE"]["COMPLETE"], 1)
        self.assertEqual(matrix["INCOMPLETE"]["INCOMPLETE"], 1)
        self.assertEqual(matrix["NOT_APPLICABLE"]["NOT_APPLICABLE"], 1)

        for st in ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"]:
            self.assertEqual(metrics[st]["precision"], 1.0)
            self.assertEqual(metrics[st]["recall"], 1.0)
            self.assertEqual(metrics[st]["f1"], 1.0)

    def test_confusion_matrix_mixed_predictions(self) -> None:
        """Verify matrix and metrics with known false positives and false negatives."""
        refs = {
            "c1": {"deletion_status": "COMPLETE"},
            "c2": {"deletion_status": "COMPLETE"},
            "c3": {"deletion_status": "INCOMPLETE"},
            "c4": {"deletion_status": "NOT_APPLICABLE"},
        }
        preds = {
            "c1": {"deletion_status": "COMPLETE"},
            "c2": {"deletion_status": "INCOMPLETE"},  # COMPLETE predicted as INCOMPLETE
            "c3": {"deletion_status": "INCOMPLETE"},
            "c4": {"deletion_status": "NOT_APPLICABLE"},
        }
        matrix, metrics = calculate_confusion_matrix(preds, refs)

        self.assertEqual(matrix["COMPLETE"]["COMPLETE"], 1)
        self.assertEqual(matrix["COMPLETE"]["INCOMPLETE"], 1)
        self.assertEqual(matrix["INCOMPLETE"]["INCOMPLETE"], 1)

        # For COMPLETE: TP=1, FP=0, FN=1 -> prec=1.0, rec=0.5, f1=0.6667
        self.assertEqual(metrics["COMPLETE"]["tp"], 1.0)
        self.assertEqual(metrics["COMPLETE"]["fn"], 1.0)
        self.assertEqual(metrics["COMPLETE"]["precision"], 1.0)
        self.assertEqual(metrics["COMPLETE"]["recall"], 0.5)
        self.assertAlmostEqual(metrics["COMPLETE"]["f1"], 0.6667, places=4)

        # For INCOMPLETE: TP=1, FP=1, FN=0 -> prec=0.5, rec=1.0, f1=0.6667
        self.assertEqual(metrics["INCOMPLETE"]["tp"], 1.0)
        self.assertEqual(metrics["INCOMPLETE"]["fp"], 1.0)
        self.assertEqual(metrics["INCOMPLETE"]["precision"], 0.5)
        self.assertEqual(metrics["INCOMPLETE"]["recall"], 1.0)

    def test_confusion_matrix_zero_division_safety(self) -> None:
        """Verify that classes with zero instances do not raise ZeroDivisionError."""
        refs = {
            "c1": {"deletion_status": "COMPLETE"},
        }
        preds = {
            "c1": {"deletion_status": "COMPLETE"},
        }
        matrix, metrics = calculate_confusion_matrix(preds, refs)
        # NOT_APPLICABLE has tp=0, fp=0, fn=0 -> precision and recall default safely to 1.0, f1 to 1.0
        self.assertEqual(metrics["NOT_APPLICABLE"]["precision"], 1.0)
        self.assertEqual(metrics["NOT_APPLICABLE"]["recall"], 1.0)
        self.assertEqual(metrics["NOT_APPLICABLE"]["f1"], 1.0)


class TestMismatchClassification(unittest.TestCase):
    """Test diagnostic classification into official root cause categories."""

    def test_classify_schema_error(self) -> None:
        """Verify schema errors are prioritized as SCHEMA_OR_SERIALIZATION_ERROR."""
        cat, mod = classify_mismatch(
            case_row={"case_id": "c1"},
            ref={"deletion_status": "COMPLETE", "steps": []},
            pred={"deletion_status": "COMPLETE", "steps": []},
            schema_errors=["Step 1 has invalid state"],
        )
        self.assertEqual(cat, MismatchCategory.SCHEMA_OR_SERIALIZATION_ERROR)
        self.assertIn("code/planner.py", mod)

    def test_classify_intent_error(self) -> None:
        """Verify mismatch involving NOT_APPLICABLE is classified as intent error."""
        cat, mod = classify_mismatch(
            case_row={"case_id": "c1"},
            ref={"deletion_status": "NOT_APPLICABLE", "steps": []},
            pred={"deletion_status": "INCOMPLETE", "steps": []},
            schema_errors=[],
        )
        self.assertEqual(cat, MismatchCategory.INTENT_CLASSIFICATION_ERROR)
        self.assertIn("code/intent.py", mod)

    def test_classify_policy_audit_error(self) -> None:
        """Verify mismatch between COMPLETE and INCOMPLETE is classified as policy audit error."""
        cat, mod = classify_mismatch(
            case_row={"case_id": "c1"},
            ref={"deletion_status": "COMPLETE", "steps": []},
            pred={"deletion_status": "INCOMPLETE", "steps": []},
            schema_errors=[],
        )
        self.assertEqual(cat, MismatchCategory.POLICY_AUDIT_ERROR)
        self.assertIn("code/audit.py", mod)

    def test_classify_evidence_system_mismatch(self) -> None:
        """Verify missing system in steps is classified as evidence coverage error."""
        ref_step = {"step_no": 1, "phase": "VERIFICATION", "system_id": "search_indexer", "resource_id": "r1"}
        pred_step = {"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "r1"}
        cat, mod = classify_mismatch(
            case_row={"case_id": "c1"},
            ref={"deletion_status": "COMPLETE", "steps": [ref_step, pred_step]},
            pred={"deletion_status": "COMPLETE", "steps": [pred_step]},
            schema_errors=[],
        )
        self.assertEqual(cat, MismatchCategory.EVIDENCE_COVERAGE_OR_PARSING_ERROR)
        self.assertIn("code/evidence.py", mod)

    def test_classify_canonical_sort_error(self) -> None:
        """Verify phase/operation difference is classified as sorting error."""
        s1 = {"step_no": 1, "phase": "FINDING", "operation": "REPORT_REMAINING_RECORD", "system_id": "account_service", "resource_id": "r1"}
        s2 = {"step_no": 1, "phase": "REMEDIATION", "operation": "DELETE_RECORD", "system_id": "account_service", "resource_id": "r1"}
        cat, mod = classify_mismatch(
            case_row={"case_id": "c1"},
            ref={"deletion_status": "INCOMPLETE", "steps": [s1]},
            pred={"deletion_status": "INCOMPLETE", "steps": [s2]},
            schema_errors=[],
        )
        self.assertEqual(cat, MismatchCategory.CANONICAL_SORTING_OR_NUMBERING_ERROR)
        self.assertIn("code/planner.py", mod)

    def test_classify_evidence_id_mismatch(self) -> None:
        """Verify evidence_id difference is classified as evidence error."""
        s1 = {"step_no": 1, "phase": "FINDING", "operation": "REPORT_REMAINING_RECORD", "system_id": "account_service", "resource_id": "r1", "evidence_id": "ev_01"}
        s2 = {"step_no": 1, "phase": "FINDING", "operation": "REPORT_REMAINING_RECORD", "system_id": "account_service", "resource_id": "r1", "evidence_id": "ev_02"}
        cat, mod = classify_mismatch(
            case_row={"case_id": "c1"},
            ref={"deletion_status": "INCOMPLETE", "steps": [s1]},
            pred={"deletion_status": "INCOMPLETE", "steps": [s2]},
            schema_errors=[],
        )
        self.assertEqual(cat, MismatchCategory.EVIDENCE_COVERAGE_OR_PARSING_ERROR)
        self.assertIn("code/evidence.py", mod)

    def test_classify_resource_id_mismatch(self) -> None:
        """Verify resource_id difference is classified as identity resolution error."""
        s1 = {"step_no": 1, "phase": "VERIFICATION", "operation": "VERIFY_RECORD_ABSENT", "system_id": "account_service", "resource_id": "account_101", "evidence_id": "ev_01"}
        s2 = {"step_no": 1, "phase": "VERIFICATION", "operation": "VERIFY_RECORD_ABSENT", "system_id": "account_service", "resource_id": "account_999", "evidence_id": "ev_01"}
        cat, mod = classify_mismatch(
            case_row={"case_id": "c1"},
            ref={"deletion_status": "COMPLETE", "steps": [s1]},
            pred={"deletion_status": "COMPLETE", "steps": [s2]},
            schema_errors=[],
        )
        self.assertEqual(cat, MismatchCategory.IDENTITY_RESOLUTION_ERROR)
        self.assertIn("code/identity.py", mod)


class TestComparisonEngine(unittest.TestCase):
    """Test prediction comparison, metric aggregation, and summary construction."""

    def test_compare_predictions_aggregated_metrics(self) -> None:
        """Verify comparison aggregation across matching and non-matching predictions."""
        requests = [
            {"case_id": "c1", "email": "c1@test.com", "audit_at": "2026-01-01T00:00:00Z"},
            {"case_id": "c2", "email": "c2@test.com", "audit_at": "2026-01-01T00:00:00Z"},
        ]
        step1 = {"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "a1", "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": "e1"}
        step2_alt = {"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "a2", "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": "e1"}

        refs = {
            "c1": {"deletion_status": "COMPLETE", "steps": [step1]},
            "c2": {"deletion_status": "COMPLETE", "steps": [step1]},
        }
        preds = {
            "c1": {"deletion_status": "COMPLETE", "steps": [step1]},
            "c2": {"deletion_status": "COMPLETE", "steps": [step2_alt]},  # Plan mismatch
        }

        summary = compare_predictions(
            requests=requests,
            references_by_case=refs,
            predictions_by_case=preds,
            validation_errors_by_case={},
            pipeline_exceptions={},
            total_elapsed_seconds=0.150,
        )

        self.assertEqual(summary.total_cases, 2)
        self.assertEqual(summary.status_matches, 2)
        self.assertEqual(summary.status_accuracy_pct, 100.0)
        self.assertEqual(summary.exact_plan_matches, 1)
        self.assertEqual(summary.exact_plan_accuracy_pct, 50.0)
        self.assertEqual(summary.full_prediction_matches, 1)
        self.assertEqual(summary.status_match_plan_mismatch, 1)
        self.assertEqual(summary.total_latency_seconds, 0.150)
        self.assertAlmostEqual(summary.mean_latency_ms, 75.0, places=1)


class TestReportGeneration(unittest.TestCase):
    """Test formatting and contents of Markdown and JSON reports."""

    def setUp(self) -> None:
        self.summary = EvaluationSummary(
            total_cases=2,
            successful_runs=2,
            pipeline_exceptions=0,
            status_matches=2,
            status_accuracy_pct=100.0,
            exact_plan_matches=2,
            exact_plan_accuracy_pct=100.0,
            full_prediction_matches=2,
            full_prediction_accuracy_pct=100.0,
            status_match_plan_mismatch=0,
            schema_invalid_count=0,
            confusion_matrix={
                "COMPLETE": {"COMPLETE": 2, "INCOMPLETE": 0, "NOT_APPLICABLE": 0},
                "INCOMPLETE": {"COMPLETE": 0, "INCOMPLETE": 0, "NOT_APPLICABLE": 0},
                "NOT_APPLICABLE": {"COMPLETE": 0, "INCOMPLETE": 0, "NOT_APPLICABLE": 0},
            },
            per_status_metrics={
                "COMPLETE": {"tp": 2.0, "fp": 0.0, "fn": 0.0, "precision": 1.0, "recall": 1.0, "f1": 1.0},
                "INCOMPLETE": {"tp": 0.0, "fp": 0.0, "fn": 0.0, "precision": 1.0, "recall": 1.0, "f1": 1.0},
                "NOT_APPLICABLE": {"tp": 0.0, "fp": 0.0, "fn": 0.0, "precision": 1.0, "recall": 1.0, "f1": 1.0},
            },
            total_latency_seconds=0.05,
            mean_latency_ms=25.0,
            mismatch_counts_by_category={},
            case_results=[
                CaseEvaluationResult(
                    case_id="case_01",
                    email="test1@example.com",
                    audit_at="2026-01-01T00:00:00Z",
                    ref_status="COMPLETE",
                    pred_status="COMPLETE",
                    status_match=True,
                    plan_match=True,
                    full_match=True,
                    ref_step_count=1,
                    pred_step_count=1,
                ),
            ],
        )
        self.integrity = DatasetIntegrityReport(
            requests_path="dummy_requests.csv",
            references_path="dummy_output.csv",
            request_count=2,
            reference_count=2,
            matched_case_count=2,
            is_valid=True,
        )

    def test_generate_markdown_report_structure(self) -> None:
        """Verify Markdown report contains all expected sections."""
        md = generate_markdown_report(self.summary, self.integrity)
        self.assertIn("# Sample Benchmark and Evaluation Report", md)
        self.assertIn("## 1. Executive Summary", md)
        self.assertIn("## 2. Dataset Integrity", md)
        self.assertIn("## 3. Status Agreement & Confusion Matrix", md)
        self.assertIn("## 4. Mismatch Diagnostics Summary", md)
        self.assertIn("## 5. Detailed Case-by-Case Breakdown", md)
        self.assertIn("100.0%", md)
        self.assertIn("case_01", md)

    def test_generate_json_report_structure(self) -> None:
        """Verify JSON report dictionary structure and types."""
        json_data = generate_json_report(self.summary, self.integrity)
        self.assertIn("metadata", json_data)
        self.assertIn("dataset_integrity", json_data)
        self.assertIn("metrics", json_data)
        self.assertIn("cases", json_data)

        metrics = json_data["metrics"]
        self.assertEqual(metrics["total_cases"], 2)
        self.assertEqual(metrics["status_accuracy_pct"], 100.0)
        self.assertEqual(metrics["exact_plan_accuracy_pct"], 100.0)
        self.assertEqual(len(json_data["cases"]), 1)


class TestBenchmarkRunnerAndDeterminism(unittest.TestCase):
    """Test full execution of run_sample_benchmark and verify determinism."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_run_sample_benchmark_end_to_end(self) -> None:
        """Verify live execution of sample benchmark and artifact generation."""
        summary, integrity = run_sample_benchmark(
            requests_path=DATASET_DIR / "sample_deletion_requests.csv",
            references_path=DATASET_DIR / "sample_output.csv",
            dataset_dir=DATASET_DIR,
            report_dir=self.temp_dir,
            save_reports=True,
            quiet=True,
        )

        self.assertEqual(summary.total_cases, 32)
        self.assertEqual(summary.status_matches, 32)
        self.assertEqual(summary.status_accuracy_pct, 100.0)
        self.assertEqual(summary.schema_invalid_count, 0)
        self.assertEqual(summary.pipeline_exceptions, 0)

        # Verify reports generated on disk
        json_file = Path(self.temp_dir) / "sample_benchmark.json"
        md_file = Path(self.temp_dir) / "sample_benchmark.md"
        self.assertTrue(json_file.exists(), "sample_benchmark.json not generated")
        self.assertTrue(md_file.exists(), "sample_benchmark.md not generated")

        # Verify JSON is parseable
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.assertEqual(data["metrics"]["status_accuracy_pct"], 100.0)

    def test_benchmark_determinism(self) -> None:
        """Verify that running the benchmark twice produces identical results."""
        summary1, _ = run_sample_benchmark(
            requests_path=DATASET_DIR / "sample_deletion_requests.csv",
            references_path=DATASET_DIR / "sample_output.csv",
            dataset_dir=DATASET_DIR,
            report_dir=self.temp_dir,
            save_reports=False,
            quiet=True,
        )
        summary2, _ = run_sample_benchmark(
            requests_path=DATASET_DIR / "sample_deletion_requests.csv",
            references_path=DATASET_DIR / "sample_output.csv",
            dataset_dir=DATASET_DIR,
            report_dir=self.temp_dir,
            save_reports=False,
            quiet=True,
        )

        self.assertEqual(summary1.status_matches, summary2.status_matches)
        self.assertEqual(summary1.exact_plan_matches, summary2.exact_plan_matches)
        self.assertEqual(summary1.full_prediction_matches, summary2.full_prediction_matches)
        self.assertEqual(
            [c.status_match for c in summary1.case_results],
            [c.status_match for c in summary2.case_results],
        )
        self.assertEqual(
            [c.plan_match for c in summary1.case_results],
            [c.plan_match for c in summary2.case_results],
        )


if __name__ == "__main__":
    unittest.main()
