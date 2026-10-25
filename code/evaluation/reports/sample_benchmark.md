# Sample Benchmark and Evaluation Report

**Generated**: 2026-10-25T16:54:15.707376Z  
**Competition**: HackerRank Orchestrate (October 2026) — *Ghost Data Still Exists*  
**Total Cases**: 32 | **Elapsed Time**: 0.168s (5.25 ms/case)

## 1. Executive Summary

| Metric | Count | Percentage |
| :--- | :---: | :---: |
| **Status Agreement** | 32 / 32 | **100.0%** |
| **Exact steps_json Match** | 30 / 32 | **93.75%** |
| **Full Prediction Match** | 30 / 32 | **93.75%** |
| Status Match / Plan Mismatch | 2 / 32 | 6.2% |
| Schema Invalid Predictions | 0 | 0.0% |
| Pipeline Exceptions | 0 | 0.0% |

## 2. Dataset Integrity

- **Requests File**: `/Users/ojaswigupta/Desktop/college/alfeus tech/orchestrate hackerrank /hackerrank-orchestrate-october26/dataset/sample_deletion_requests.csv` (32 rows)
- **Reference File**: `/Users/ojaswigupta/Desktop/college/alfeus tech/orchestrate hackerrank /hackerrank-orchestrate-october26/dataset/sample_output.csv` (32 rows)
- **Matched Case IDs**: 32 (0 missing, 0 unexpected)
- **Integrity Status**: PASSED (Zero cross-file anomalies)

## 3. Status Agreement & Confusion Matrix

| Reference \ Predicted | COMPLETE | INCOMPLETE | NOT_APPLICABLE |
| :--- | :---: | :---: | :---: |
| **COMPLETE** | 10 | 0 | 0 |
| **INCOMPLETE** | 0 | 22 | 0 |
| **NOT_APPLICABLE** | 0 | 0 | 0 |

### Per-Status Classification Metrics

| Status | Precision | Recall | F1 Score | Support |
| :--- | :---: | :---: | :---: | :---: |
| `COMPLETE` | 1.0000 | 1.0000 | 1.0000 | 10 |
| `INCOMPLETE` | 1.0000 | 1.0000 | 1.0000 | 22 |
| `NOT_APPLICABLE` | 1.0000 | 1.0000 | 1.0000 | 0 |

## 4. Mismatch Diagnostics Summary

| Mismatch Category | Affected Cases | Recommended Inspection |
| :--- | :---: | :--- |
| `EVIDENCE_COVERAGE_OR_PARSING_ERROR` | 2 | `code/evidence.py` |

## 5. Detailed Case-by-Case Breakdown

| Case ID | Status Match | Plan Match | Ref Steps | Pred Steps | Diagnostics / Mismatch |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `case_01` | ✅ PASS | ✅ PASS | 1 | 1 | Exact match |
| `case_02` | ✅ PASS | ✅ PASS | 1 | 1 | Exact match |
| `case_03` | ✅ PASS | ✅ PASS | 3 | 3 | Exact match |
| `case_04` | ✅ PASS | ✅ PASS | 2 | 2 | Exact match |
| `case_05` | ✅ PASS | ✅ PASS | 3 | 3 | Exact match |
| `case_06` | ✅ PASS | ✅ PASS | 4 | 4 | Exact match |
| `case_07` | ✅ PASS | ✅ PASS | 4 | 4 | Exact match |
| `case_08` | ✅ PASS | ✅ PASS | 4 | 4 | Exact match |
| `case_09` | ✅ PASS | ✅ PASS | 5 | 5 | Exact match |
| `case_10` | ✅ PASS | ✅ PASS | 4 | 4 | Exact match |
| `case_11` | ✅ PASS | ✅ PASS | 14 | 14 | Exact match |
| `case_12` | ✅ PASS | ✅ PASS | 5 | 5 | Exact match |
| `case_13` | ✅ PASS | ✅ PASS | 8 | 8 | Exact match |
| `case_14` | ✅ PASS | ✅ PASS | 5 | 5 | Exact match |
| `case_15` | ✅ PASS | ✅ PASS | 5 | 5 | Exact match |
| `case_16` | ✅ PASS | ✅ PASS | 7 | 7 | Exact match |
| `case_17` | ✅ PASS | ✅ PASS | 5 | 5 | Exact match |
| `case_18` | ✅ PASS | ⚠️ DIFF | 14 | 13 | Diff at step 11 (EVIDENCE_COVERAGE_OR_PARSING_ERROR) |
| `case_19` | ✅ PASS | ✅ PASS | 8 | 8 | Exact match |
| `case_20` | ✅ PASS | ⚠️ DIFF | 8 | 7 | Diff at step 7 (EVIDENCE_COVERAGE_OR_PARSING_ERROR) |
| `case_21` | ✅ PASS | ✅ PASS | 3 | 3 | Exact match |
| `case_22` | ✅ PASS | ✅ PASS | 8 | 8 | Exact match |
| `case_23` | ✅ PASS | ✅ PASS | 8 | 8 | Exact match |
| `case_24` | ✅ PASS | ✅ PASS | 7 | 7 | Exact match |
| `case_25` | ✅ PASS | ✅ PASS | 3 | 3 | Exact match |
| `case_26` | ✅ PASS | ✅ PASS | 6 | 6 | Exact match |
| `case_27` | ✅ PASS | ✅ PASS | 6 | 6 | Exact match |
| `case_28` | ✅ PASS | ✅ PASS | 6 | 6 | Exact match |
| `case_29` | ✅ PASS | ✅ PASS | 14 | 14 | Exact match |
| `case_30` | ✅ PASS | ✅ PASS | 14 | 14 | Exact match |
| `case_31` | ✅ PASS | ✅ PASS | 3 | 3 | Exact match |
| `case_32` | ✅ PASS | ✅ PASS | 5 | 5 | Exact match |