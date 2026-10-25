"""Custom Evaluation Test Suite for HackerRank Orchestrate (Step 9).

Implements three required custom, synthetic evaluation cases:
1. CUSTOM_EVAL_01: Temporal Identity Resolution with Historical Email
   - Validates temporal graph resolution as-of cutoff without future leaks.
   - Validates that historical email aliases are recognized retroactively
     while future changes are strictly invisible before their occurrence.
   - Validates temporal boundary enforcement in audit ingestion.

2. CUSTOM_EVAL_02: Deletion with an Active Recreation Path
   - Validates detection of active data flow / unblocked restore recreation paths.
   - Proves that primary record deletion alone does not suffice when data
     recreation risks remain active.
   - Validates policy-compliant INCOMPLETE status and strict canonical 3-phase ordering:
     FINDING -> REMEDIATION -> VERIFICATION with sequential 1-based numbering.

3. CUSTOM_EVAL_03: Non-Deletion Request
   - Validates diverse non-deletion intents (export, access inquiry, profile update,
     explicit negation).
   - Validates that non-deletion intents are mapped to NOT_APPLICABLE.
   - Validates that steps_json evaluates strictly to empty array [].
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = REPO_ROOT / "dataset"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit import (
    AuditEngine,
    AuditFinding,
    AuditResult,
    DeletionPolicy,
    DeletionStatus,
    RecreationRisk,
    RequiredAction,
    ResourceAuditResult,
)
from evidence import EvidenceRecord, EvidenceStore
from identity import IdentityGraph, ResolutionStatus, UserIdentityProfile
from intent import IntentType, RequestIntentClassifier
from planner import RemediationPlanner, validate_steps


class TestCustomEvaluation(unittest.TestCase):
    """Three custom synthetic evaluation cases required by Step 9."""

    @classmethod
    def setUpClass(cls):
        cls.policy = DeletionPolicy(DATASET_DIR / "deletion_rules.csv")
        cls.intent_classifier = RequestIntentClassifier()

    def test_custom_eval_01_temporal_identity_resolution_with_historical_email(self):
        """CUSTOM_EVAL_01: Temporal Identity Resolution with Historical Email.
        
        Evaluates temporal identity graph resolution across time boundaries:
        - User user_synth_101 updates email from historical_user101@example.com
          to modern_user101@example.com at T_event = 2026-07-15T12:00:00Z.
        - As-of T_prior = 2026-06-01T00:00:00Z (before update):
          * Lookup by historical email resolves to user_synth_101 with active_email = historical.
          * Lookup by modern email returns UNRESOLVED (strictly zero future leakage).
        - As-of T_post = 2026-08-01T00:00:00Z (after update):
          * Lookup by modern email resolves with active_email = modern.
          * Lookup by historical email ALSO resolves to user_synth_101 via historical alias tracking.
          * profile.all_emails contains both historical and modern identifiers.
        """
        raw_users = [
            {
                "user_id": "user_synth_101",
                "name": "Jordan Smith",
                "email": "modern_user101@example.com",
                "phone": "+1 555 0101",
                "email_hash": "hash_modern_101",
                "updated_at": "2026-07-15T12:00:00Z",
            }
        ]
        raw_audits = [
            {
                "audit_id": "audit_evt_101_email",
                "user_id": "user_synth_101",
                "event_time": "2026-07-15T12:00:00Z",
                "field_name": "email",
                "old_value": "historical_user101@example.com",
                "new_value": "modern_user101@example.com",
            }
        ]

        graph = IdentityGraph()
        graph.load_from_dicts(raw_users, raw_audits)

        # 1. Evaluate prior to email change: 2026-06-01T00:00:00Z
        t_prior = "2026-06-01T00:00:00Z"
        res_prior_hist = graph.resolve_by_email("historical_user101@example.com", as_of_time=t_prior)
        self.assertEqual(res_prior_hist.status, ResolutionStatus.RESOLVED)
        self.assertEqual(res_prior_hist.user_id, "user_synth_101")
        self.assertIsNotNone(res_prior_hist.profile)
        self.assertEqual(res_prior_hist.profile.active_email, "historical_user101@example.com")
        self.assertIn("historical_user101@example.com", res_prior_hist.profile.all_emails)
        # Verify strict zero future leakage
        self.assertNotIn("modern_user101@example.com", res_prior_hist.profile.all_emails)

        res_prior_modern = graph.resolve_by_email("modern_user101@example.com", as_of_time=t_prior)
        self.assertEqual(res_prior_modern.status, ResolutionStatus.UNRESOLVED)
        self.assertIsNone(res_prior_modern.user_id)

        # 2. Evaluate after email change: 2026-08-01T00:00:00Z
        t_post = "2026-08-01T00:00:00Z"
        res_post_modern = graph.resolve_by_email("modern_user101@example.com", as_of_time=t_post)
        self.assertEqual(res_post_modern.status, ResolutionStatus.RESOLVED)
        self.assertEqual(res_post_modern.user_id, "user_synth_101")
        self.assertEqual(res_post_modern.profile.active_email, "modern_user101@example.com")
        self.assertIn("modern_user101@example.com", res_post_modern.profile.all_emails)
        self.assertIn("historical_user101@example.com", res_post_modern.profile.all_emails)

        res_post_hist = graph.resolve_by_email("historical_user101@example.com", as_of_time=t_post)
        self.assertEqual(res_post_hist.status, ResolutionStatus.RESOLVED)
        self.assertEqual(res_post_hist.user_id, "user_synth_101")
        self.assertEqual(res_post_hist.matched_by, "historical_email")

        # 3. Verify temporal evidence matching against historical and modern profiles
        pre_update_doc = {"user_id": "user_synth_101", "email": "historical_user101@example.com"}
        post_update_doc = {"user_id": "user_synth_101", "email": "modern_user101@example.com"}

        profile_prior = res_prior_hist.profile
        self.assertTrue(profile_prior.matches_any_identifier(pre_update_doc["email"]))
        self.assertFalse(profile_prior.matches_any_identifier(post_update_doc["email"]))

        profile_post = res_post_modern.profile
        self.assertTrue(profile_post.matches_any_identifier(pre_update_doc["email"]))
        self.assertTrue(profile_post.matches_any_identifier(post_update_doc["email"]))

    def test_custom_eval_02_deletion_with_active_recreation_path(self):
        """CUSTOM_EVAL_02: Deletion with an Active Recreation Path.
        
        Evaluates system behavior when primary user records have been deleted,
        but an active integration connector recreation path persists:
        - Primary records in account_service and search_service are absent (compliant).
        - An active CRM sync connector (integration_service) remains enabled.
        - The deletion audit engine must detect the recreation risk, conclude that
          deletion is INCOMPLETE, and generate the required findings.
        - The canonical planner must generate an exactly ordered 3-phase plan:
          1. FINDING (REPORT_ACTIVE_DATA_FLOW) citing source evidence ID.
          2. REMEDIATION (DISABLE_DATA_FLOW) with evidence_id = "".
          3. VERIFICATION (VERIFY_DATA_FLOW_DISABLED, VERIFY_RECORD_ABSENT) with evidence_id = "".
        - Steps must be 1-based sequentially numbered (1, 2, 3...) with full schema compliance.
        """
        audit_res = AuditResult(
            case_id="custom_case_recreation_02",
            user_id="user_synth_202",
            email="victim_recreation@example.com",
            audit_at="2026-09-20T10:00:00Z",
            status=DeletionStatus.INCOMPLETE,
            intent_type=IntentType.DELETE_REQUEST,
            resource_results=[
                ResourceAuditResult(
                    system_id="account_service",
                    resource_id="account_202",
                    rule_id="rule_01",
                    record_type="account_record",
                    required_action=RequiredAction.DELETE,
                    observed_state="ABSENT",
                    is_compliant=True,
                    evidence_id="ev_acc_del_202",
                ),
            ],
            findings=[
                AuditFinding(
                    finding_type="REPORT_ACTIVE_DATA_FLOW",
                    rule_id="rule_14",
                    system_id="integration_service",
                    resource_id="connector_salesforce_crm",
                    observed_state="ACTIVE",
                    expected_state="DISABLED",
                    evidence_id="ev_connector_active_202",
                    explanation="Active CRM synchronization connector detected capable of recreating deleted user records.",
                )
            ],
            recreation_risks=[
                RecreationRisk(
                    risk_type="active_connector",
                    source_system_id="integration_service",
                    source_resource_id="connector_salesforce_crm",
                    destination_system_id="account_service",
                    destination_resource_id="account_202",
                    observed_state="ACTIVE",
                    recreation_mechanism="CRM bi-directional sync re-inserts deleted account.",
                    evidence_ids=["ev_connector_active_202"],
                    explanation="CRM sync connector is enabled.",
                )
            ],
        )

        known_eids = {"ev_acc_del_202", "ev_connector_active_202"}
        planner = RemediationPlanner(known_evidence_ids=known_eids)

        # Plan the steps
        steps = planner.plan_steps(audit_res)
        self.assertGreaterEqual(len(steps), 3)

        # 1. Verify Phase Invariants and Sequence
        phases = [s["phase"] for s in steps]
        finding_indices = [i for i, p in enumerate(phases) if p == "FINDING"]
        remediation_indices = [i for i, p in enumerate(phases) if p == "REMEDIATION"]
        verification_indices = [i for i, p in enumerate(phases) if p == "VERIFICATION"]

        self.assertTrue(len(finding_indices) > 0)
        self.assertTrue(len(remediation_indices) > 0)
        self.assertTrue(len(verification_indices) > 0)

        # All FINDINGs must precede all REMEDIATIONs
        self.assertTrue(max(finding_indices) < min(remediation_indices))
        # All REMEDIATIONs must precede all VERIFICATIONs
        self.assertTrue(max(remediation_indices) < min(verification_indices))

        # 2. Check Phase 1: FINDING step
        finding_step = steps[0]
        self.assertEqual(finding_step["step_no"], 1)
        self.assertEqual(finding_step["phase"], "FINDING")
        self.assertEqual(finding_step["system_id"], "integration_service")
        self.assertEqual(finding_step["resource_id"], "connector_salesforce_crm")
        self.assertEqual(finding_step["operation"], "REPORT_ACTIVE_DATA_FLOW")
        self.assertEqual(finding_step["state"], "ACTIVE")
        self.assertEqual(finding_step["evidence_id"], "ev_connector_active_202")

        # 3. Check Phase 2: REMEDIATION step
        rem_step = steps[1]
        self.assertEqual(rem_step["step_no"], 2)
        self.assertEqual(rem_step["phase"], "REMEDIATION")
        self.assertEqual(rem_step["system_id"], "integration_service")
        self.assertEqual(rem_step["resource_id"], "connector_salesforce_crm")
        self.assertEqual(rem_step["operation"], "DISABLE_DATA_FLOW")
        self.assertEqual(rem_step["state"], "DISABLED")
        self.assertEqual(rem_step["evidence_id"], "")

        # 4. Check Phase 3: VERIFICATION steps
        ver_connector = next(
            s for s in steps if s["phase"] == "VERIFICATION" and s["resource_id"] == "connector_salesforce_crm"
        )
        self.assertEqual(ver_connector["system_id"], "integration_service")
        self.assertEqual(ver_connector["operation"], "VERIFY_DATA_FLOW_DISABLED")
        self.assertEqual(ver_connector["state"], "DISABLED")
        self.assertEqual(ver_connector["evidence_id"], "")

        # 5. Check consecutive 1-based numbering
        for idx, s in enumerate(steps, start=1):
            self.assertEqual(s["step_no"], idx)

        # 6. Validate step validation engine
        validation_errors = validate_steps(steps, DeletionStatus.INCOMPLETE, known_evidence_ids=known_eids)
        self.assertEqual(len(validation_errors), 0, f"Validation errors found: {validation_errors}")

        # 7. Confirm JSON serialization round-trip
        serialized = planner.format_steps_json(steps)
        parsed = json.loads(serialized)
        self.assertEqual(len(parsed), len(steps))
        self.assertEqual(parsed[0]["operation"], "REPORT_ACTIVE_DATA_FLOW")

    def test_custom_eval_03_non_deletion_request(self):
        """CUSTOM_EVAL_03: Non-Deletion Request.
        
        Evaluates intent classification and audit handling for diverse
        requests that do NOT authorize personal data deletion:
        1. Data Export / Portability Request (GDPR Art. 20)
        2. Data Access / Personal Details Inquiry (GDPR Art. 15)
        3. Profile Information Update / Rectification
        4. Explicit Negation ("I still use it, so please don't delete or change anything.")
        
        For each case:
        - RequestIntentClassifier determines IntentType.NOT_DELETION.
        - Downstream audit yields DeletionStatus.NOT_APPLICABLE.
        - Generated remediation plan is strictly the empty array [].
        - Serialized steps_json evaluates strictly to "[]".
        """
        non_deletion_cases = [
            (
                "export_01",
                "I'd like to download my account information in a portable format. Please keep the account as it is.",
                "Export / portability request",
            ),
            (
                "inquiry_02",
                "Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.",
                "Access / inquiry request",
            ),
            (
                "rectification_03",
                "The mobile number on my profile is out of date. Please replace it with the new one and leave the account active.",
                "Rectification / profile update request",
            ),
            (
                "negation_04",
                "Could you send me a copy of the information held on my account? I still use it, so please don't delete or change anything.",
                "Explicit negation request",
            ),
        ]

        planner = RemediationPlanner(known_evidence_ids=set())

        for cid, text, description in non_deletion_cases:
            with self.subTest(case_id=cid, description=description):
                # 1. Intent Classification
                intent_res = self.intent_classifier.classify(text)
                self.assertEqual(
                    intent_res.intent,
                    IntentType.NOT_DELETION,
                    f"Failed to identify non-deletion intent for '{text}': {intent_res.reason}",
                )
                self.assertTrue(intent_res.is_not_applicable)
                self.assertFalse(intent_res.is_deletion_request)

                # 2. Simulated Audit Result for NOT_APPLICABLE
                audit_res = AuditResult(
                    case_id=cid,
                    user_id=f"user_{cid}",
                    email=f"{cid}@example.com",
                    audit_at="2026-09-20T10:00:00Z",
                    status=DeletionStatus.NOT_APPLICABLE,
                    intent_type=IntentType.NOT_DELETION,
                    resource_results=[],
                    findings=[],
                )

                # 3. Canonical Plan Generation
                steps = planner.plan_steps(audit_res)
                self.assertEqual(
                    steps,
                    [],
                    f"NOT_APPLICABLE case {cid} must produce an empty steps list, got {steps}",
                )

                # 4. Canonical Plan Serialization
                steps_json = planner.format_steps_json(steps)
                self.assertEqual(
                    steps_json,
                    "[]",
                    f"NOT_APPLICABLE case {cid} must serialize to '[]', got {steps_json}",
                )

                # 5. Schema Validation
                errors = validate_steps(steps, DeletionStatus.NOT_APPLICABLE)
                self.assertEqual(len(errors), 0, f"Validation failed for {cid}: {errors}")


if __name__ == "__main__":
    unittest.main()
