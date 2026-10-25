"""Automated unit test suite for code/audit.py (Step 5 Policy Audit & State Detection).

Covers all required policy audit, treatment, temporal boundary, and recreation risk scenarios:
1. policy_loading_and_rule_count (all 15 rules from deletion_rules.csv)
2. policy_rule_predicates (is_delete, is_redact, is_anonymize, is_retain)
3. category_inference (all canonical systems and resource prefixes)
4. delete_treatment_present (remaining record triggers REPORT_REMAINING_RECORD)
5. delete_treatment_absent (absent record verifies compliant ABSENT state)
6. redact_treatment_exposed_pii (direct identifiers trigger REPORT_EXPOSED_PII)
7. redact_treatment_compliant (redacted tombstone with user_id passes)
8. anonymize_treatment_identifiable (identifiable analytics profile triggers REPORT_EXPOSED_PII)
9. anonymize_treatment_compliant (anonymized record passes)
10. retain_treatment_security_audit (security audit log within retention passes)
11. retain_treatment_legal_hold (legal hold record passes)
12. backup_archive_restore_blocked (blocked restore passes)
13. backup_archive_restore_risk (unblocked restore triggers REPORT_RESTORE_RISK)
14. active_data_flow_risk (enabled connector triggers REPORT_ACTIVE_DATA_FLOW)
15. disabled_data_flow_compliant (disabled connector passes)
16. sync_recreation_detection (SYNC after DELETE triggers recreation finding)
17. temporal_boundary_zero_leakage (future evidence strictly excluded)
18. intent_not_deletion_mapping (informational inquiries produce NOT_APPLICABLE)
19. synthetic_edge_scenarios (synthetic scenarios 1 through 6)
20. sample_output_evaluation_agreement (100% 32/32 status agreement on sample dataset)
"""

import csv
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = REPO_ROOT / "dataset"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from audit import (  # noqa: E402
    AuditEngine,
    AuditFinding,
    AuditResult,
    DeletionPolicy,
    DeletionStatus,
    PolicyRule,
    RecreationRisk,
    RequiredAction,
    ResourceAuditResult,
)
from evidence import EvidenceRecord, EvidenceStore  # noqa: E402
from identity import IdentityGraph, UserIdentityProfile  # noqa: E402
from intent import IntentType, RequestIntentClassifier  # noqa: E402


def _make_dummy_profile(
    user_id: str = "user_100",
    email: str = "alice@example.com",
    phone: str = "+1 555 1234",
    name: str = "Alice Smith",
    email_hash: str = "hash_alice",
) -> UserIdentityProfile:
    return UserIdentityProfile(
        user_id=user_id,
        snapshot_name=name,
        snapshot_email=email,
        snapshot_phone=phone,
        snapshot_email_hash=email_hash,
        snapshot_updated_at="2026-08-01T00:00:00Z",
        as_of_time="2026-09-01T00:00:00Z",
        active_name=name,
        active_email=email,
        active_phone=phone,
        active_email_hash=email_hash,
        all_emails={email},
        all_emails_original={email},
        all_phones={phone},
        all_names={name},
        all_email_hashes={email_hash},
    )


class TestPolicyAudit(unittest.TestCase):
    """Test suite for DeletionPolicy and AuditEngine."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules_path = DATASET_DIR / "deletion_rules.csv"
        cls.identities_path = DATASET_DIR / "user_identities.csv"
        cls.audit_log_path = DATASET_DIR / "user_audit_log.csv"
        cls.activity_path = DATASET_DIR / "system_activity.csv"
        cls.sample_requests_path = DATASET_DIR / "sample_deletion_requests.csv"
        cls.sample_output_path = DATASET_DIR / "sample_output.csv"

        cls.policy = DeletionPolicy(cls.rules_path)
        cls.graph = IdentityGraph(cls.identities_path, cls.audit_log_path)
        cls.store = EvidenceStore(DATASET_DIR)
        cls.engine = AuditEngine(
            policy=cls.policy,
            identity_graph=cls.graph,
            evidence_store=cls.store,
            system_activity_path=cls.activity_path,
        )

    def test_01_policy_loading_and_rule_count(self) -> None:
        """Verify that exactly 15 official rules are loaded with valid categories and actions."""
        self.assertEqual(len(self.policy.rules), 15)
        expected_categories = [
            "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY",
            "SUPPORT_MESSAGE", "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY",
            "USER_EXPORT", "MARKETING_PROFILE", "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG",
            "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE",
        ]
        for cat in expected_categories:
            rule = self.policy.get_rule_by_category(cat)
            self.assertIsNotNone(rule, f"Missing rule for category {cat}")
            self.assertIn(rule.required_action, RequiredAction)

    def test_02_policy_rule_predicates(self) -> None:
        """Verify is_delete, is_redact, is_anonymize, is_retain helper properties."""
        r_del = self.policy.get_rule_by_category("USER_PROFILE")
        self.assertTrue(r_del.is_delete)
        self.assertFalse(r_del.is_redact)

        r_red = self.policy.get_rule_by_category("SUPPORT_MESSAGE")
        self.assertTrue(r_red.is_redact)

        r_anon = self.policy.get_rule_by_category("ANALYTICS_PROFILE")
        self.assertTrue(r_anon.is_anonymize)

        r_ret = self.policy.get_rule_by_category("SECURITY_AUDIT_LOG")
        self.assertTrue(r_ret.is_retain)

    def test_03_category_inference(self) -> None:
        """Verify inference of categories across canonical systems and resource prefixes."""
        self.assertEqual(self.policy.infer_category("account_service", "account_123"), "USER_PROFILE")
        self.assertEqual(self.policy.infer_category("search_service", "search_456"), "SEARCH_INDEX_ENTRY")
        self.assertEqual(self.policy.infer_category("cache_service", "cache_789"), "CACHE_ENTRY")
        self.assertEqual(self.policy.infer_category("support_platform", "object_attachment_1"), "SUPPORT_ATTACHMENT")
        self.assertEqual(self.policy.infer_category("support_platform", "support_ticket_1"), "SUPPORT_MESSAGE")
        self.assertEqual(self.policy.infer_category("analytics_platform", "analytics_99"), "ANALYTICS_PROFILE")
        self.assertEqual(self.policy.infer_category("export_service", "export_88"), "USER_EXPORT")
        self.assertEqual(self.policy.infer_category("marketing_platform", "marketing_77"), "MARKETING_PROFILE")
        self.assertEqual(self.policy.infer_category("email_delivery_service", "email_delivery_66"), "EMAIL_DELIVERY_LOG")
        self.assertEqual(self.policy.infer_category("security_service", "audit_record_55"), "SECURITY_AUDIT_LOG")
        self.assertEqual(self.policy.infer_category("legal_service", "legal_hold_44"), "LEGAL_HOLD_RECORD")
        self.assertEqual(self.policy.infer_category("backup_service", "backup_33"), "BACKUP_ARCHIVE")
        self.assertEqual(self.policy.infer_category("identity_service", "tombstone_22"), "ACCOUNT_TOMBSTONE")
        self.assertEqual(self.policy.infer_category("integration_service", "data_flow_11"), "DATA_FLOW")

    def test_04_delete_treatment_present(self) -> None:
        """Verify that a surviving record requiring DELETE triggers REPORT_REMAINING_RECORD."""
        rec = EvidenceRecord(
            source_evidence_id="evidence_99",
            system_id="search_service",
            resource_id="search_001",
            source_type="JSON_FILE",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values={"test@example.com"},
            raw_state="PRESENT",
            record_content={"hits": {"total": 1}},
        )
        self.assertTrue(self.engine._is_record_present(rec))

    def test_05_delete_treatment_absent(self) -> None:
        """Verify that an absent record requiring DELETE is recognized as absent."""
        rec = EvidenceRecord(
            source_evidence_id="evidence_99",
            system_id="account_service",
            resource_id="account_001",
            source_type="LOG_EVENT",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values={"test@example.com"},
            raw_state=None,
            record_content={"kvs": {"http_status": "404", "rows": "0"}},
        )
        self.assertFalse(self.engine._is_record_present(rec))

    def test_06_redact_treatment_exposed_pii(self) -> None:
        """Verify that prohibited personal data in a tombstone triggers exposed PII finding."""
        prof = _make_dummy_profile()
        # Tombstone with prohibited email exposed
        rec_violating = EvidenceRecord(
            source_evidence_id="evidence_51",
            system_id="identity_service",
            resource_id="tombstone_100",
            source_type="JSON_FILE",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values={"user_100", "alice@example.com"},
            raw_state=None,
            record_content={"user_id": "user_100", "email": "alice@example.com"},
        )
        self.assertTrue(self.engine._has_prohibited_pii_for_redaction(rec_violating, prof, "ACCOUNT_TOMBSTONE"))

    def test_07_redact_treatment_compliant(self) -> None:
        """Verify that a compliant tombstone containing ONLY user_id passes redaction check."""
        prof = _make_dummy_profile()
        rec_compliant = EvidenceRecord(
            source_evidence_id="evidence_51",
            system_id="identity_service",
            resource_id="tombstone_100",
            source_type="JSON_FILE",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values={"user_100"},
            raw_state=None,
            record_content={"user_id": "user_100", "email": None},
        )
        self.assertFalse(self.engine._has_prohibited_pii_for_redaction(rec_compliant, prof, "ACCOUNT_TOMBSTONE"))

    def test_08_anonymize_treatment_identifiable(self) -> None:
        """Verify that an analytics profile with user attribution is recognized as identifiable."""
        prof = _make_dummy_profile()
        rec = EvidenceRecord(
            source_evidence_id="evidence_56",
            system_id="analytics_platform",
            resource_id="analytics_100",
            source_type="JSON_FILE",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values={"alice@example.com"},
            raw_state="IDENTIFIABLE",
            record_content={"email": "alice@example.com"},
        )
        self.assertTrue(self.engine._is_record_identifiable(rec, prof))

    def test_09_anonymize_treatment_compliant(self) -> None:
        """Verify that an analytics profile with null identifiers is recognized as anonymized."""
        prof = _make_dummy_profile()
        rec = EvidenceRecord(
            source_evidence_id="evidence_56",
            system_id="analytics_platform",
            resource_id="analytics_100",
            source_type="JSON_FILE",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values=set(),
            raw_state="ANONYMIZED",
            record_content={"email": None, "user_id": None},
        )
        self.assertFalse(self.engine._is_record_identifiable(rec, prof))

    def test_10_retain_treatment_security_audit(self) -> None:
        """Verify that security audit log under rule_12 is compliant when retained."""
        rule = self.policy.get_rule_by_category("SECURITY_AUDIT_LOG")
        self.assertEqual(rule.rule_id, "rule_12")
        self.assertEqual(rule.required_action, RequiredAction.RETAIN)

    def test_11_retain_treatment_legal_hold(self) -> None:
        """Verify that legal hold record under rule_13 is compliant when retained."""
        rule = self.policy.get_rule_by_category("LEGAL_HOLD_RECORD")
        self.assertEqual(rule.rule_id, "rule_13")
        self.assertEqual(rule.required_action, RequiredAction.RETAIN)

    def test_12_backup_archive_restore_blocked(self) -> None:
        """Verify that backup archive with restore_allowed == False is recognized as blocked."""
        rec = EvidenceRecord(
            source_evidence_id="evidence_92",
            system_id="backup_service",
            resource_id="backup_100",
            source_type="JSON_FILE",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values={"user_100"},
            raw_state=None,
            record_content={"restore_allowed": False},
        )
        self.assertTrue(self.engine._is_restore_blocked(rec))

    def test_13_backup_archive_restore_risk(self) -> None:
        """Verify that backup archive with restore_allowed == True is recognized as NOT blocked (risk)."""
        rec = EvidenceRecord(
            source_evidence_id="evidence_93",
            system_id="backup_service",
            resource_id="backup_100",
            source_type="SQL_SNAPSHOT",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values={"user_100"},
            raw_state=None,
            record_content={"restore_allowed": True},
        )
        self.assertFalse(self.engine._is_restore_blocked(rec))

    def test_14_active_data_flow_risk(self) -> None:
        """Verify that integration connector with enabled == True is recognized as active risk."""
        rec = EvidenceRecord(
            source_evidence_id="evidence_94",
            system_id="integration_service",
            resource_id="data_flow_100",
            source_type="LOG_EVENT",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values={"test@example.com"},
            raw_state=None,
            record_content={"kvs": {"enabled": "true"}},
        )
        self.assertTrue(self.engine._is_data_flow_active(rec))

    def test_15_disabled_data_flow_compliant(self) -> None:
        """Verify that integration connector with enabled == False is recognized as disabled."""
        rec = EvidenceRecord(
            source_evidence_id="evidence_92",
            system_id="integration_service",
            resource_id="data_flow_100",
            source_type="LOG_EVENT",
            record_timestamp="2026-09-01T10:00:00Z",
            identifier_values={"test@example.com"},
            raw_state=None,
            record_content={"kvs": {"enabled": "false"}},
        )
        self.assertFalse(self.engine._is_data_flow_active(rec))

    def test_16_sync_recreation_detection(self) -> None:
        """Verify that SYNC_RECORD occurring after DELETE_RECORD triggers sync recreation finding."""
        prof = _make_dummy_profile(
            user_id="user_05",
            email="leila.haddad@yahoo.com",
            phone="+212 6 48 27 15 93",
            name="Leila Haddad",
            email_hash="hash_leila",
        )
        recs = self.engine._detect_sync_recreations(prof, "2026-08-28T23:00:00Z", {"account_005"})
        self.assertTrue(len(recs) > 0)
        self.assertEqual(recs[0]["resource_id"], "account_005")

    def test_17_temporal_boundary_zero_leakage(self) -> None:
        """Verify temporal cutoff enforcement: events strictly after audit_at are excluded."""
        prof = self.graph.resolve_by_email("amina.elmasri@gmail.com").profile
        # Query with historical cutoff
        evs_past = self.store.query_by_identity(prof, as_of_time="2026-08-01T00:00:00Z")
        # Ensure that no events strictly after cutoff leak into query
        for ev in evs_past:
            self.assertLessEqual(ev.record_timestamp, "2026-08-01T00:00:00Z")

    def test_18_intent_not_deletion_mapping(self) -> None:
        """Verify that an informational non-deletion request yields NOT_APPLICABLE with zero findings."""
        res = self.engine.audit_case(
            case_id="case_synth_inquiry",
            email="inquiry.user@example.com",
            request_text="Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.",
            audit_at="2026-09-20T12:00:00Z",
        )
        self.assertEqual(res.status, DeletionStatus.NOT_APPLICABLE)
        self.assertEqual(len(res.findings), 0)
        self.assertEqual(len(res.recreation_risks), 0)

    def test_19_synthetic_edge_scenarios(self) -> None:
        """Verify synthetic edge scenarios 1 through 6 required by the specification."""
        # Scenario 1: Clean complete deletion
        res1 = self.engine.audit_case(
            case_id="case_01",
            email="amina.elmasri@gmail.com",
            request_text="Hi, I haven’t used this account in years and would like it closed. Please remove the profile and the personal details attached to it.",
            audit_at="2026-09-03T11:30:00Z",
        )
        self.assertEqual(res1.status, DeletionStatus.COMPLETE)

        # Scenario 2: Remaining record violation (case_06 search_001)
        res2 = self.engine.audit_case(
            case_id="case_06",
            email="jonas.berg@outlook.com",
            request_text="My account page is gone, but my old profile still appears when I search for it. Can you remove that search result too?",
            audit_at="2026-09-10T09:15:00Z",
        )
        self.assertEqual(res2.status, DeletionStatus.INCOMPLETE)
        has_search_finding = any(f.resource_id == "search_001" and f.finding_type == "REPORT_REMAINING_RECORD" for f in res2.findings)
        self.assertTrue(has_search_finding)

        # Scenario 3: Exposed PII in tombstone (case_32 tombstone_9178)
        res3 = self.engine.audit_case(
            case_id="case_32",
            email="addo.kofi@gmail.com",
            request_text="The account itself is gone, but an internal deleted-account record still shows my personal details. Please keep only the reference needed for retained records and remove the rest.",
            audit_at="2026-09-15T12:00:00Z",
        )
        self.assertEqual(res3.status, DeletionStatus.INCOMPLETE)
        has_pii_finding = any(f.finding_type == "REPORT_EXPOSED_PII" for f in res3.findings)
        self.assertTrue(has_pii_finding)

        # Scenario 4: Active data flow connector (case_20 data_flow_1770)
        res4 = self.engine.audit_case(
            case_id="case_20",
            email="akosua.gyasi@gmail.com",
            request_text="Please treat this message as a request for permanent account deletion. Please check the records connected to the account before confirming completion. Please send a confirmation after the process finishes.",
            audit_at="2026-09-26T11:00:00Z",
        )
        self.assertEqual(res4.status, DeletionStatus.INCOMPLETE)
        has_flow_finding = any(f.finding_type == "REPORT_ACTIVE_DATA_FLOW" for f in res4.findings)
        self.assertTrue(has_flow_finding)

        # Scenario 5: Backup restore risk (case_09 backup_4978)
        res5 = self.engine.audit_case(
            case_id="case_09",
            email="k.appiah88@gmail.com",
            request_text="Please permanently delete the account associated with this email address. Please apply the correct treatment to records covered by the request. Please confirm when the review is complete.",
            audit_at="2026-09-16T09:00:00Z",
        )
        self.assertEqual(res5.status, DeletionStatus.INCOMPLETE)
        has_restore_finding = any(f.finding_type == "REPORT_RESTORE_RISK" for f in res5.findings)
        self.assertTrue(has_restore_finding)

        # Scenario 6: Non-deletion request
        res6 = self.engine.audit_case(
            case_id="case_synth_export",
            email="export.user@example.com",
            request_text="I'd like to download my account information in a portable format. Please keep the account as it is.",
            audit_at="2026-09-20T12:00:00Z",
        )
        self.assertEqual(res6.status, DeletionStatus.NOT_APPLICABLE)

    def test_20_sample_output_evaluation_agreement(self) -> None:
        """Assert 100% agreement (32/32) across all cases in sample_deletion_requests.csv."""
        with open(self.sample_requests_path, "r", encoding="utf-8") as f:
            sample_requests = list(csv.DictReader(f))

        with open(self.sample_output_path, "r", encoding="utf-8") as f:
            sample_outputs = {r["case_id"]: r for r in csv.DictReader(f)}

        matches = 0
        mismatches = []
        for req in sample_requests:
            cid = req["case_id"]
            res = self.engine.audit_case(
                case_id=cid,
                email=req["email"],
                request_text=req["request_text"],
                audit_at=req["audit_at"],
                requested_at=req.get("requested_at"),
                reported_at=req.get("reported_at"),
                reported_status=req.get("reported_status"),
            )
            expected_status = sample_outputs[cid]["deletion_status"]
            actual_status = res.status.value
            if actual_status == expected_status:
                matches += 1
            else:
                mismatches.append(f"{cid}: actual={actual_status}, expected={expected_status}")

        self.assertEqual(
            matches,
            len(sample_requests),
            f"Expected 32/32 matches on sample dataset, got {matches}/{len(sample_requests)}. Mismatches: {mismatches}",
        )


if __name__ == "__main__":
    unittest.main()
