"""Comprehensive Unit Tests for Remediation Planner and Canonical Sorter (code/planner.py).

Tests:
1. Exact 7-field step schema.
2. 4-level canonical ordering hierarchy (phase -> operation -> system_id -> resource_id).
3. Consecutive 1-based step numbering (1, 2, 3, ...).
4. Status-specific invariants:
   - NOT_APPLICABLE -> []
   - COMPLETE -> only VERIFICATION with evidence
   - INCOMPLETE -> FINDING (evidence), REMEDIATION (""), VERIFICATION ("")
5. Operation-to-state pairing validity.
6. Canonical system ID normalization.
7. Validation engine error detection (schema, numbering, ordering, state, evidence).
8. Determinism and idempotence under input shuffling.
9. JSON serialization round-trip fidelity.
"""

import json
import random
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = REPO_ROOT / "dataset"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit import (
    AuditFinding,
    AuditResult,
    DeletionStatus,
    RequiredAction,
    ResourceAuditResult,
)
from intent import IntentType
from planner import (
    CANONICAL_SYSTEMS,
    OPERATION_ORDER,
    OPERATION_TO_STATE,
    PHASE_ORDER,
    RemediationPlanner,
    assign_step_numbers,
    canonical_sort_key,
    normalize_system_id,
    sort_steps,
    validate_steps,
)


class TestRemediationPlanner(unittest.TestCase):
    """Unit tests for code/planner.py."""

    def setUp(self) -> None:
        self.planner = RemediationPlanner(
            known_evidence_ids={"evidence_01", "evidence_51", "evidence_54", "evidence_92", "evidence_94"}
        )

    # -----------------------------------------------------------------------
    # 1. Canonical Ordering Tests
    # -----------------------------------------------------------------------

    def test_canonical_phase_ordering(self) -> None:
        """Phase order must strictly be FINDING (1) -> REMEDIATION (2) -> VERIFICATION (3)."""
        steps = [
            {
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_001",
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT",
                "evidence_id": "",
            },
            {
                "phase": "FINDING",
                "system_id": "account_service",
                "resource_id": "account_001",
                "operation": "REPORT_REMAINING_RECORD",
                "state": "PRESENT",
                "evidence_id": "evidence_54",
            },
            {
                "phase": "REMEDIATION",
                "system_id": "account_service",
                "resource_id": "account_001",
                "operation": "DELETE_RECORD",
                "state": "ABSENT",
                "evidence_id": "",
            },
        ]
        sorted_steps = sort_steps(steps)
        self.assertEqual(sorted_steps[0]["phase"], "FINDING")
        self.assertEqual(sorted_steps[1]["phase"], "REMEDIATION")
        self.assertEqual(sorted_steps[2]["phase"], "VERIFICATION")

    def test_canonical_finding_operation_ordering(self) -> None:
        """FINDING operations must follow official priority order."""
        ops = [
            ("REPORT_RESTORE_RISK", "backup_service", "backup_01", "ACTIVE"),
            ("REPORT_ACTIVE_DATA_FLOW", "integration_service", "data_flow_01", "ACTIVE"),
            ("REPORT_EXPOSED_PII", "analytics_platform", "analytics_01", "PRESENT"),
            ("REPORT_REMAINING_RECORD", "search_service", "search_01", "PRESENT"),
        ]
        steps = [
            {
                "phase": "FINDING",
                "system_id": sid,
                "resource_id": rid,
                "operation": op,
                "state": st,
                "evidence_id": "evidence_01",
            }
            for op, sid, rid, st in ops
        ]
        sorted_steps = sort_steps(steps)
        result_ops = [s["operation"] for s in sorted_steps]
        expected_ops = [
            "REPORT_REMAINING_RECORD",
            "REPORT_EXPOSED_PII",
            "REPORT_ACTIVE_DATA_FLOW",
            "REPORT_RESTORE_RISK",
        ]
        self.assertEqual(result_ops, expected_ops)

    def test_canonical_remediation_operation_ordering(self) -> None:
        """REMEDIATION operations must prioritize safety controls before record actions."""
        ops = [
            ("ANONYMIZE_RECORD", "analytics_platform", "analytics_01", "ANONYMIZED"),
            ("DELETE_RECORD", "search_service", "search_01", "ABSENT"),
            ("BLOCK_RESTORE", "backup_service", "backup_01", "BLOCKED"),
            ("REDACT_RECORD", "support_platform", "support_01", "REDACTED"),
            ("DISABLE_DATA_FLOW", "integration_service", "data_flow_01", "DISABLED"),
        ]
        steps = [
            {
                "phase": "REMEDIATION",
                "system_id": sid,
                "resource_id": rid,
                "operation": op,
                "state": st,
                "evidence_id": "",
            }
            for op, sid, rid, st in ops
        ]
        sorted_steps = sort_steps(steps)
        result_ops = [s["operation"] for s in sorted_steps]
        expected_ops = [
            "DISABLE_DATA_FLOW",
            "BLOCK_RESTORE",
            "DELETE_RECORD",
            "REDACT_RECORD",
            "ANONYMIZE_RECORD",
        ]
        self.assertEqual(result_ops, expected_ops)

    def test_canonical_verification_operation_ordering(self) -> None:
        """VERIFICATION operations must follow official priority order."""
        ops = [
            ("VERIFY_RESTORE_BLOCKED", "backup_service", "backup_01", "BLOCKED"),
            ("VERIFY_RECORD_ANONYMIZED", "analytics_platform", "analytics_01", "ANONYMIZED"),
            ("VERIFY_RECORD_PRESENT", "security_service", "audit_record_01", "PRESENT"),
            ("VERIFY_DATA_FLOW_DISABLED", "integration_service", "data_flow_01", "DISABLED"),
            ("VERIFY_RECORD_REDACTED", "support_platform", "support_01", "REDACTED"),
            ("VERIFY_RECORD_ABSENT", "account_service", "account_01", "ABSENT"),
        ]
        steps = [
            {
                "phase": "VERIFICATION",
                "system_id": sid,
                "resource_id": rid,
                "operation": op,
                "state": st,
                "evidence_id": "",
            }
            for op, sid, rid, st in ops
        ]
        sorted_steps = sort_steps(steps)
        result_ops = [s["operation"] for s in sorted_steps]
        expected_ops = [
            "VERIFY_RECORD_ABSENT",
            "VERIFY_RECORD_PRESENT",
            "VERIFY_RECORD_REDACTED",
            "VERIFY_RECORD_ANONYMIZED",
            "VERIFY_DATA_FLOW_DISABLED",
            "VERIFY_RESTORE_BLOCKED",
        ]
        self.assertEqual(result_ops, expected_ops)

    def test_system_and_resource_alphabetical_tiebreak(self) -> None:
        """Within the same phase and operation, sort ascending by system_id then resource_id."""
        steps = [
            {"phase": "FINDING", "system_id": "support_platform", "resource_id": "support_002", "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_01"},
            {"phase": "FINDING", "system_id": "account_service", "resource_id": "account_005", "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_01"},
            {"phase": "FINDING", "system_id": "support_platform", "resource_id": "support_001", "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_01"},
            {"phase": "FINDING", "system_id": "account_service", "resource_id": "account_001", "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_01"},
        ]
        sorted_steps = sort_steps(steps)
        pairs = [(s["system_id"], s["resource_id"]) for s in sorted_steps]
        expected_pairs = [
            ("account_service", "account_001"),
            ("account_service", "account_005"),
            ("support_platform", "support_001"),
            ("support_platform", "support_002"),
        ]
        self.assertEqual(pairs, expected_pairs)

    # -----------------------------------------------------------------------
    # 2. Sequential Step Numbering Tests
    # -----------------------------------------------------------------------

    def test_consecutive_step_numbering(self) -> None:
        """Step numbers must start at 1 and increase consecutively without gaps."""
        steps = [
            {"phase": "FINDING", "system_id": "search_service", "resource_id": "search_01", "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_01"},
            {"phase": "REMEDIATION", "system_id": "search_service", "resource_id": "search_01", "operation": "DELETE_RECORD", "state": "ABSENT", "evidence_id": ""},
            {"phase": "VERIFICATION", "system_id": "search_service", "resource_id": "search_01", "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": ""},
        ]
        numbered = assign_step_numbers(steps)
        self.assertEqual([s["step_no"] for s in numbered], [1, 2, 3])

    # -----------------------------------------------------------------------
    # 3. Status-Specific Invariants Tests
    # -----------------------------------------------------------------------

    def test_not_applicable_returns_empty_array(self) -> None:
        """NOT_APPLICABLE audit results must produce an empty steps list []."""
        res = AuditResult(
            case_id="case_533",
            user_id="user_533",
            email="informational@example.com",
            audit_at="2026-09-15T12:00:00Z",
            status=DeletionStatus.NOT_APPLICABLE,
            intent_type=IntentType.NOT_DELETION,
        )
        steps = self.planner.plan_steps(res)
        self.assertEqual(steps, [])

    def test_complete_contains_only_verification_with_evidence(self) -> None:
        """COMPLETE audit results must contain only VERIFICATION steps citing non-empty evidence."""
        res = AuditResult(
            case_id="case_01",
            user_id="user_01",
            email="amina.elmasri@gmail.com",
            audit_at="2026-09-02T12:00:00Z",
            status=DeletionStatus.COMPLETE,
            intent_type=IntentType.DELETE_REQUEST,
            resource_results=[
                ResourceAuditResult(
                    system_id="account_service",
                    resource_id="account_001",
                    rule_id="rule_01",
                    record_type="USER_PROFILE",
                    required_action=RequiredAction.DELETE,
                    observed_state="ABSENT",
                    is_compliant=True,
                    evidence_id="evidence_54",
                )
            ],
        )
        steps = self.planner.plan_steps(res)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step_no"], 1)
        self.assertEqual(steps[0]["phase"], "VERIFICATION")
        self.assertEqual(steps[0]["system_id"], "account_service")
        self.assertEqual(steps[0]["resource_id"], "account_001")
        self.assertEqual(steps[0]["operation"], "VERIFY_RECORD_ABSENT")
        self.assertEqual(steps[0]["state"], "ABSENT")
        self.assertEqual(steps[0]["evidence_id"], "evidence_54")

    def test_incomplete_evidence_id_invariants(self) -> None:
        """INCOMPLETE findings cite evidence; planned remediations and verifications have evidence_id=''."""
        res = AuditResult(
            case_id="case_05",
            user_id="user_05",
            email="leila.haddad@yahoo.com",
            audit_at="2026-09-02T12:00:00Z",
            status=DeletionStatus.INCOMPLETE,
            intent_type=IntentType.DELETE_REQUEST,
            findings=[
                AuditFinding(
                    finding_type="REPORT_REMAINING_RECORD",
                    rule_id="rule_01",
                    system_id="account_service",
                    resource_id="account_005",
                    observed_state="PRESENT",
                    expected_state="ABSENT",
                    evidence_id="evidence_54",
                )
            ],
        )
        steps = self.planner.plan_steps(res)
        self.assertEqual(len(steps), 3)

        # Finding: cites evidence_54
        f_step = [s for s in steps if s["phase"] == "FINDING"][0]
        self.assertEqual(f_step["evidence_id"], "evidence_54")

        # Remediation: evidence_id is empty string
        rem_step = [s for s in steps if s["phase"] == "REMEDIATION"][0]
        self.assertEqual(rem_step["evidence_id"], "")

        # Verification: evidence_id is empty string
        v_step = [s for s in steps if s["phase"] == "VERIFICATION"][0]
        self.assertEqual(v_step["evidence_id"], "")

    # -----------------------------------------------------------------------
    # 4. Remediation Mapping Tests for All Finding Types
    # -----------------------------------------------------------------------

    def test_all_finding_types_mapped_to_correct_actions(self) -> None:
        """Every finding type must produce its exact remediation and verification operations."""
        test_cases = [
            (
                "REPORT_ACTIVE_DATA_FLOW",
                "integration_service",
                "data_flow_01",
                "ACTIVE",
                "DISABLE_DATA_FLOW",
                "DISABLED",
                "VERIFY_DATA_FLOW_DISABLED",
                "DISABLED",
            ),
            (
                "REPORT_RESTORE_RISK",
                "backup_service",
                "backup_01",
                "ACTIVE",
                "BLOCK_RESTORE",
                "BLOCKED",
                "VERIFY_RESTORE_BLOCKED",
                "BLOCKED",
            ),
            (
                "REPORT_REMAINING_RECORD",
                "search_service",
                "search_01",
                "PRESENT",
                "DELETE_RECORD",
                "ABSENT",
                "VERIFY_RECORD_ABSENT",
                "ABSENT",
            ),
            (
                "REPORT_EXPOSED_PII",
                "support_platform",
                "support_01",
                "PRESENT",
                "REDACT_RECORD",
                "REDACTED",
                "VERIFY_RECORD_REDACTED",
                "REDACTED",
            ),
            (
                "REPORT_EXPOSED_PII",
                "analytics_platform",
                "analytics_01",
                "PRESENT",
                "ANONYMIZE_RECORD",
                "ANONYMIZED",
                "VERIFY_RECORD_ANONYMIZED",
                "ANONYMIZED",
            ),
        ]

        for (
            f_type,
            sid,
            rid,
            f_state,
            exp_rem_op,
            exp_rem_state,
            exp_v_op,
            exp_v_state,
        ) in test_cases:
            res = AuditResult(
                case_id="case_test",
                user_id="user_test",
                email="test@example.com",
                audit_at="2026-09-15T12:00:00Z",
                status=DeletionStatus.INCOMPLETE,
                intent_type=IntentType.DELETE_REQUEST,
                findings=[
                    AuditFinding(
                        finding_type=f_type,
                        rule_id="rule_01",
                        system_id=sid,
                        resource_id=rid,
                        observed_state=f_state,
                        expected_state="N/A",
                        evidence_id="evidence_01",
                    )
                ],
            )
            steps = self.planner.plan_steps(res)

            # Check finding
            f_step = [s for s in steps if s["phase"] == "FINDING"][0]
            self.assertEqual(f_step["operation"], f_type)
            self.assertEqual(f_step["state"], f_state)

            # Check remediation
            rem_step = [s for s in steps if s["phase"] == "REMEDIATION"][0]
            self.assertEqual(rem_step["operation"], exp_rem_op)
            self.assertEqual(rem_step["state"], exp_rem_state)

            # Check verification
            v_step = [s for s in steps if s["phase"] == "VERIFICATION"][0]
            self.assertEqual(v_step["operation"], exp_v_op)
            self.assertEqual(v_step["state"], exp_v_state)

    # -----------------------------------------------------------------------
    # 5. Validation Engine Tests
    # -----------------------------------------------------------------------

    def test_validation_detects_forbidden_phase_in_complete(self) -> None:
        """Validation must catch FINDING or REMEDIATION steps in a COMPLETE request."""
        bad_steps = [
            {
                "step_no": 1,
                "phase": "FINDING",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "REPORT_REMAINING_RECORD",
                "state": "PRESENT",
                "evidence_id": "evidence_54",
            }
        ]
        errors = validate_steps(bad_steps, status="COMPLETE")
        self.assertTrue(any("forbidden phase" in e for e in errors))

    def test_validation_detects_empty_evidence_in_complete(self) -> None:
        """Validation must catch missing evidence_id in a COMPLETE request."""
        bad_steps = [
            {
                "step_no": 1,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT",
                "evidence_id": "",
            }
        ]
        errors = validate_steps(bad_steps, status="COMPLETE")
        self.assertTrue(any("must cite a valid evidence_id" in e for e in errors))

    def test_validation_detects_non_empty_evidence_in_incomplete_remediation(self) -> None:
        """Validation must catch non-empty evidence_id in REMEDIATION or VERIFICATION for INCOMPLETE."""
        bad_steps = [
            {
                "step_no": 1,
                "phase": "FINDING",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "REPORT_REMAINING_RECORD",
                "state": "PRESENT",
                "evidence_id": "evidence_54",
            },
            {
                "step_no": 2,
                "phase": "REMEDIATION",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "DELETE_RECORD",
                "state": "ABSENT",
                "evidence_id": "evidence_54",  # Illegal non-empty evidence_id
            },
        ]
        errors = validate_steps(bad_steps, status="INCOMPLETE")
        self.assertTrue(any("must have empty evidence_id" in e for e in errors))

    def test_validation_detects_missing_fields(self) -> None:
        """Validation must detect missing schema fields."""
        incomplete_step = [
            {
                "step_no": 1,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "VERIFY_RECORD_ABSENT",
                # missing state and evidence_id
            }
        ]
        errors = validate_steps(incomplete_step, status="COMPLETE")
        self.assertTrue(any("missing required fields" in e for e in errors))

    def test_validation_detects_invalid_state_pairing(self) -> None:
        """Validation must flag when operation does not match its required state."""
        bad_step = [
            {
                "step_no": 1,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "PRESENT",  # Illegal state for VERIFY_RECORD_ABSENT
                "evidence_id": "evidence_54",
            }
        ]
        errors = validate_steps(bad_step, status="COMPLETE")
        self.assertTrue(any("requires state 'ABSENT', got 'PRESENT'" in e for e in errors))

    def test_validation_detects_step_number_gap(self) -> None:
        """Validation must detect non-consecutive step numbers."""
        gapped_steps = [
            {
                "step_no": 1,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT",
                "evidence_id": "evidence_54",
            },
            {
                "step_no": 3,  # Should be 2
                "phase": "VERIFICATION",
                "system_id": "security_service",
                "resource_id": "audit_record_01",
                "operation": "VERIFY_RECORD_PRESENT",
                "state": "PRESENT",
                "evidence_id": "evidence_54",
            },
        ]
        errors = validate_steps(gapped_steps, status="COMPLETE")
        self.assertTrue(any("invalid step_no 3, expected 2" in e for e in errors))

    def test_validation_detects_non_canonical_order(self) -> None:
        """Validation must detect steps array violating canonical sort order."""
        unsorted = [
            {
                "step_no": 1,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "VERIFY_RECORD_PRESENT",  # priority 2
                "state": "PRESENT",
                "evidence_id": "evidence_54",
            },
            {
                "step_no": 2,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "VERIFY_RECORD_ABSENT",  # priority 1 (should be first)
                "state": "ABSENT",
                "evidence_id": "evidence_54",
            },
        ]
        errors = validate_steps(unsorted, status="COMPLETE")
        self.assertTrue(any("Steps are not in canonical order" in e for e in errors))

    # -----------------------------------------------------------------------
    # 6. Determinism, Idempotence, and Serialization
    # -----------------------------------------------------------------------

    def test_planner_determinism_under_shuffling(self) -> None:
        """Shuffled input findings must always produce the exact same canonical plan."""
        findings = [
            AuditFinding(
                finding_type="REPORT_RESTORE_RISK",
                rule_id="rule_14",
                system_id="backup_service",
                resource_id="backup_01",
                observed_state="ACTIVE",
                expected_state="BLOCKED",
                evidence_id="evidence_01",
            ),
            AuditFinding(
                finding_type="REPORT_REMAINING_RECORD",
                rule_id="rule_03",
                system_id="search_service",
                resource_id="search_01",
                observed_state="PRESENT",
                expected_state="ABSENT",
                evidence_id="evidence_01",
            ),
            AuditFinding(
                finding_type="REPORT_ACTIVE_DATA_FLOW",
                rule_id="data_flow",
                system_id="integration_service",
                resource_id="data_flow_01",
                observed_state="ACTIVE",
                expected_state="DISABLED",
                evidence_id="evidence_01",
            ),
            AuditFinding(
                finding_type="REPORT_EXPOSED_PII",
                rule_id="rule_05",
                system_id="support_platform",
                resource_id="support_01",
                observed_state="PRESENT",
                expected_state="REDACTED",
                evidence_id="evidence_01",
            ),
        ]

        res_baseline = AuditResult(
            case_id="case_det",
            user_id="user_det",
            email="test@example.com",
            audit_at="2026-09-15T12:00:00Z",
            status=DeletionStatus.INCOMPLETE,
            intent_type=IntentType.DELETE_REQUEST,
            findings=list(findings),
        )
        plan_baseline = self.planner.plan_steps(res_baseline)

        # Shuffle 10 times and verify output is identical
        for seed in range(10):
            shuffled = list(findings)
            random.Random(seed).shuffle(shuffled)
            res_shuffled = AuditResult(
                case_id="case_det",
                user_id="user_det",
                email="test@example.com",
                audit_at="2026-09-15T12:00:00Z",
                status=DeletionStatus.INCOMPLETE,
                intent_type=IntentType.DELETE_REQUEST,
                findings=shuffled,
            )
            plan_shuffled = self.planner.plan_steps(res_shuffled)
            self.assertEqual(plan_shuffled, plan_baseline)

    def test_json_serialization_round_trip(self) -> None:
        """Serialized steps_json string must parse back to identical JSON data."""
        res = AuditResult(
            case_id="case_05",
            user_id="user_05",
            email="leila.haddad@yahoo.com",
            audit_at="2026-09-02T12:00:00Z",
            status=DeletionStatus.INCOMPLETE,
            intent_type=IntentType.DELETE_REQUEST,
            findings=[
                AuditFinding(
                    finding_type="REPORT_REMAINING_RECORD",
                    rule_id="rule_01",
                    system_id="account_service",
                    resource_id="account_005",
                    observed_state="PRESENT",
                    expected_state="ABSENT",
                    evidence_id="evidence_54",
                )
            ],
        )
        steps = self.planner.plan_steps(res)
        json_str = self.planner.format_steps_json(steps)
        parsed = json.loads(json_str)
        self.assertEqual(parsed, steps)

    def test_validation_detects_unknown_evidence_id(self) -> None:
        """Validation must flag an evidence_id not present in known evidence registry."""
        step = [
            {
                "step_no": 1,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_01",
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT",
                "evidence_id": "evidence_nonexistent_999",
            }
        ]
        errors = validate_steps(step, status="COMPLETE", known_evidence_ids={"evidence_01", "evidence_54"})
        self.assertTrue(any("unknown evidence_id" in e for e in errors))

    def test_validation_detects_empty_applicable_steps(self) -> None:
        """Validation must flag empty steps array for COMPLETE or INCOMPLETE requests."""
        errors = validate_steps([], status="COMPLETE")
        self.assertTrue(any("must contain at least one step" in e for e in errors))
        errors_incomp = validate_steps([], status="INCOMPLETE")
        self.assertTrue(any("must contain at least one step" in e for e in errors_incomp))

    def test_canonical_systems_normalization(self) -> None:
        """Non-canonical system aliases and prefixes must normalize to canonical systems."""
        self.assertEqual(normalize_system_id("account_failover_replica", "account_005"), "account_service")
        self.assertEqual(normalize_system_id("account_replication_cluster", "account_5334"), "account_service")
        self.assertEqual(normalize_system_id("custom_sys", "search_001"), "search_service")
        self.assertEqual(normalize_system_id("custom_sys", "tombstone_10"), "identity_service")
        self.assertEqual(normalize_system_id("custom_sys", "data_flow_99"), "integration_service")

    def test_complete_case_with_multiple_compliant_resources(self) -> None:
        """COMPLETE request with multiple compliant resources formats all verifications canonically."""
        res = AuditResult(
            case_id="case_multi",
            user_id="user_multi",
            email="multi@example.com",
            audit_at="2026-09-15T12:00:00Z",
            status=DeletionStatus.COMPLETE,
            intent_type=IntentType.DELETE_REQUEST,
            resource_results=[
                ResourceAuditResult(
                    system_id="security_service",
                    resource_id="audit_record_01",
                    rule_id="rule_12",
                    record_type="SECURITY_AUDIT_LOG",
                    required_action=RequiredAction.RETAIN,
                    observed_state="PRESENT",
                    is_compliant=True,
                    evidence_id="evidence_92",
                ),
                ResourceAuditResult(
                    system_id="account_service",
                    resource_id="account_01",
                    rule_id="rule_01",
                    record_type="USER_PROFILE",
                    required_action=RequiredAction.DELETE,
                    observed_state="ABSENT",
                    is_compliant=True,
                    evidence_id="evidence_92",
                ),
            ],
        )
        steps = self.planner.plan_steps(res)
        self.assertEqual(len(steps), 2)
        # VERIFY_RECORD_ABSENT (op 1) comes before VERIFY_RECORD_PRESENT (op 2)
        self.assertEqual(steps[0]["operation"], "VERIFY_RECORD_ABSENT")
        self.assertEqual(steps[1]["operation"], "VERIFY_RECORD_PRESENT")
        self.assertEqual(steps[0]["step_no"], 1)
        self.assertEqual(steps[1]["step_no"], 2)


if __name__ == "__main__":
    unittest.main()
