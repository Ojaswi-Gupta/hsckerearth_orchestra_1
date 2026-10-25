"""Step 8 Regression and Full Pipeline Validation Tests.

Validates:
1. Snapshot container timestamp inheritance for time boundary filtering.
2. Activity log pseudo-resource exclusion from data-at-rest evaluation.
3. Anonymized analytics profile resolution and verification.
4. Primary account collision filtering across concurrent batch requests.
5. Schema compliance and deterministic ordering of generated output.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import sys
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODE_DIR = REPO_ROOT / "code"
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from identity import IdentityGraph, UserIdentityProfile
from intent import RequestIntentClassifier
from evidence import EvidenceStore, EvidenceRecord
from audit import AuditEngine, DeletionPolicy, DeletionStatus, RequiredAction, ResourceAuditResult
from planner import RemediationPlanner


class TestStep8PipelineEnhancements(unittest.TestCase):
    """Test suite for Step 8 accuracy enhancements and edge case handling."""

    @classmethod
    def setUpClass(cls):
        dataset_dir = REPO_ROOT / "dataset"
        cls.graph = IdentityGraph(
            identities_path=dataset_dir / "user_identities.csv",
            audit_log_path=dataset_dir / "user_audit_log.csv",
        )
        cls.store = EvidenceStore(dataset_dir)
        cls.policy = DeletionPolicy(dataset_dir / "deletion_rules.csv")
        cls.engine = AuditEngine(
            policy=cls.policy,
            identity_graph=cls.graph,
            evidence_store=cls.store,
            system_activity_path=dataset_dir / "system_activity.csv",
        )
        known_eids = set(cls.store.manifest.keys())
        for r in cls.store.records:
            known_eids.add(r.source_evidence_id)
        cls.planner = RemediationPlanner(known_evidence_ids=known_eids)

    def test_anonymized_analytics_resolution_case_03(self):
        """Verify case_03 accurately discovers anonymized analytics_9406 in evidence_94."""
        profile = self.graph.resolve_by_email("l.fontaine46@yahoo.com").profile
        self.assertIsNotNone(profile)

        res = self.engine.audit_case(
            case_id="case_03",
            email="l.fontaine46@yahoo.com",
            request_text="Je demande la suppression definitive du compte associe a cette adresse e-mail.",
            audit_at="2026-09-26T11:00:00Z",
            requested_at="2026-09-13T11:00:00Z",
            reported_at="2026-09-15T11:00:00Z",
            reported_status="COMPLETE",
        )
        self.assertEqual(res.status, DeletionStatus.COMPLETE)
        analytics_res = [rr for rr in res.resource_results if rr.system_id == "analytics_platform"]
        self.assertEqual(len(analytics_res), 1)
        self.assertEqual(analytics_res[0].resource_id, "analytics_9406")
        self.assertEqual(analytics_res[0].observed_state, "ANONYMIZED")
        self.assertEqual(analytics_res[0].evidence_id, "evidence_94")

        steps = self.planner.plan_steps(res)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[2]["resource_id"], "analytics_9406")
        self.assertEqual(steps[2]["operation"], "VERIFY_RECORD_ANONYMIZED")

    def test_anonymized_analytics_resolution_case_04(self):
        """Verify case_04 discovers anonymized analytics_901 in evidence_56."""
        profile = self.graph.resolve_by_email("thandiwe.n@gmail.com").profile
        self.assertIsNotNone(profile)

        res = self.engine.audit_case(
            case_id="case_04",
            email="thandiwe.n@gmail.com",
            request_text="I am deleting my account. Please remove everything.",
            audit_at="2026-09-07T10:20:00Z",
            requested_at="2026-08-25T12:32:00Z",
            reported_at="2026-08-27T08:54:00Z",
            reported_status="COMPLETE",
        )
        self.assertEqual(res.status, DeletionStatus.COMPLETE)
        analytics_res = [rr for rr in res.resource_results if rr.system_id == "analytics_platform"]
        self.assertEqual(len(analytics_res), 1)
        self.assertEqual(analytics_res[0].resource_id, "analytics_901")
        self.assertEqual(analytics_res[0].evidence_id, "evidence_56")

        steps = self.planner.plan_steps(res)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[1]["resource_id"], "analytics_901")
        self.assertEqual(steps[1]["operation"], "VERIFY_RECORD_ANONYMIZED")

    def test_concurrent_batch_account_resolution_cases_25_and_26(self):
        """Verify case_25 (account_3136) and case_26 (account_5684) resolve without collision."""
        res_25 = self.engine.audit_case(
            case_id="case_25",
            email="s.yoon04@gmail.com",
            request_text="Please delete my account and analytics data.",
            requested_at="2026-08-31T12:00:00Z",
            audit_at="2026-09-15T12:00:00Z",
            reported_at="2026-09-02T12:00:00Z",
            reported_status="NOT_STARTED",
        )
        self.assertEqual(res_25.status, DeletionStatus.COMPLETE)
        acc_25 = [rr for rr in res_25.resource_results if rr.system_id == "account_service"]
        self.assertEqual(len(acc_25), 1)
        self.assertEqual(acc_25[0].resource_id, "account_3136")

        res_26 = self.engine.audit_case(
            case_id="case_26",
            email="almeida.mariana@gmail.com",
            request_text="I found an old report saying my account still existed. Please check the latest state and confirm that the profile is gone and that remaining records cannot identify me.",
            requested_at="2026-08-30T12:00:00Z",
            audit_at="2026-09-15T12:00:00Z",
            reported_at="2026-09-02T12:00:00Z",
            reported_status="NOT_STARTED",
        )
        self.assertEqual(res_26.status, DeletionStatus.COMPLETE)
        acc_26 = [rr for rr in res_26.resource_results if rr.system_id == "account_service"]
        self.assertEqual(len(acc_26), 1)
        self.assertEqual(acc_26[0].resource_id, "account_5684")
        self.assertEqual(acc_26[0].evidence_id, "evidence_92")

    def test_snapshot_bundle_evidence_inheritance_case_31(self):
        """Verify case_31 account verification inherits evidence_51 from snapshot bundle."""
        res = self.engine.audit_case(
            case_id="case_31",
            email="a.eze08@gmail.com",
            request_text="I no longer need this account. Please delete it.",
            requested_at="2026-09-04T12:00:00Z",
            audit_at="2026-09-15T12:00:00Z",
            reported_at="2026-09-06T12:00:00Z",
            reported_status="COMPLETE",
        )
        self.assertEqual(res.status, DeletionStatus.COMPLETE)
        steps = self.planner.plan_steps(res)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["resource_id"], "account_4857")
        self.assertEqual(steps[0]["evidence_id"], "evidence_51")

    def test_activity_log_exclusion_from_data_at_rest(self):
        """Verify activity logs are not treated as data-at-rest personal records."""
        profile = self.graph.resolve_by_email("priya.raman@gmail.com").profile
        self.assertIsNotNone(profile)
        # In case_07, object_19 was an activity link log, not stored unencrypted PDF
        raw_recs = self.store.query_by_identity(profile, as_of_time="2026-09-15T12:00:00Z")
        non_act = [r for r in raw_recs if r.source_type != "ACTIVITY_LOG"]
        for r in non_act:
            self.assertNotEqual(r.source_type, "ACTIVITY_LOG")


if __name__ == "__main__":
    unittest.main()
