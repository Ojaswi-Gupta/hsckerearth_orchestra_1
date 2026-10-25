"""Automated test suite for code/identity.py.

Covers 12 focused test scenarios:
1. Current email lookup
2. Historical email lookup
3. Case-insensitive email lookup
4. Historical identifier evolution & provenance
5. Unknown email handling
6. Temporal boundary exclusion (events > audit_at excluded)
7. Same timestamp events handling
8. Identifier collision detection & ambiguity handling
9. Missing/blank values handling
10. Historical phone & hash tracking with provenance
11. Boundary inclusivity (events == audit_at included)
12. Input normalization (whitespace & mixed casing)
"""

import sys
import unittest
from pathlib import Path

# Add project root and code directory to path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = REPO_ROOT / "dataset"

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from identity import (  # noqa: E402
    IdentityGraph,
    ResolutionResult,
    ResolutionStatus,
    UserIdentityProfile,
    normalize_email,
    normalize_text,
)


class TestIdentityGraph(unittest.TestCase):
    """Test suite for IdentityGraph and UserIdentityProfile."""

    @classmethod
    def setUpClass(cls):
        """Load the official dataset once for dataset-backed tests."""
        cls.graph = IdentityGraph(
            identities_path=DATASET_DIR / "user_identities.csv",
            audit_log_path=DATASET_DIR / "user_audit_log.csv",
        )

    def test_01_current_email_lookup(self):
        """Verify direct resolution of a current active email."""
        result = self.graph.resolve_by_email("amina.elmasri@gmail.com")
        self.assertEqual(result.status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.user_id, "user_01")
        self.assertIsNotNone(result.profile)
        self.assertEqual(result.profile.user_id, "user_01")
        self.assertEqual(result.profile.snapshot_name, "Amina El-Masri")
        self.assertTrue(result.profile.matches_email("amina.elmasri@gmail.com"))

    def test_02_historical_email_lookup(self):
        """Verify resolution of an obsolete historical email to the correct user."""
        # Nadia Ionescu (user_10) changed email from nadia.ionescu@yahoo.com to nadia.petrescu@yahoo.com
        result = self.graph.resolve_by_email("nadia.ionescu@yahoo.com")
        self.assertEqual(result.status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.user_id, "user_10")
        self.assertIsNotNone(result.profile)
        self.assertTrue(result.profile.matches_email("nadia.ionescu@yahoo.com"))
        self.assertTrue(result.profile.matches_email("n.petrescu@yahoo.com"))

    def test_03_case_insensitive_lookup(self):
        """Verify resolution with uppercase and mixed-case emails."""
        result = self.graph.resolve_by_email("SIPHO.MOKOENA@GMAIL.COM")
        self.assertEqual(result.status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.user_id, "user_36")
        self.assertIsNotNone(result.profile)
        self.assertTrue(result.profile.matches_email("sipho.mokoena@gmail.com"))

    def test_04_historical_identifier_evolution(self):
        """Verify user_523's email and hash transition history and provenance."""
        # user_523 transitioned from o.walker6695@yahoo.com to o.walker66@yahoo.com
        profile = self.graph.get_profile("user_523")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.user_id, "user_523")
        self.assertIn("o.walker6695@yahoo.com", profile.all_emails)
        self.assertIn("o.walker66@yahoo.com", profile.all_emails)

        # Verify hash transition
        old_hash = "6570536e0f14e7262d2b39a7d7968f30a4f7ee35f260591a95a3a54c313363a8"
        new_hash = "9c40bdbab4944a7f0e06bb210e8990ff340ee998813b17e432000a70f5dfe554"
        self.assertTrue(profile.matches_hash(old_hash))
        self.assertTrue(profile.matches_hash(new_hash))

        # Check provenance
        audit_ids = [p.audit_id for p in profile.provenance if p.audit_id]
        self.assertIn("audit_59", audit_ids)
        self.assertIn("audit_60", audit_ids)

    def test_05_unknown_email(self):
        """Verify that an unknown email returns UNRESOLVED cleanly without exceptions."""
        result = self.graph.resolve_by_email("nonexistent_user@example.com")
        self.assertEqual(result.status, ResolutionStatus.UNRESOLVED)
        self.assertIsNone(result.user_id)
        self.assertIsNone(result.profile)
        self.assertIn("No user identity found", result.message)

    def test_06_temporal_boundary_exclusion(self):
        """Verify that audit events occurring strictly AFTER as_of_time are excluded."""
        # user_523 changed email on 2025-09-30T21:50:27Z
        # If we query as of 2025-01-01, the new email and new hash must not exist yet
        before_cutoff = "2025-01-01T00:00:00Z"
        profile = self.graph.get_profile("user_523", as_of_time=before_cutoff)
        self.assertIsNotNone(profile)

        # Before cutoff, active email was o.walker6695@yahoo.com
        self.assertEqual(profile.active_email, "o.walker6695@yahoo.com")
        self.assertIn("o.walker6695@yahoo.com", profile.all_emails)
        # Future email must NOT be in profile
        self.assertNotIn("o.walker66@yahoo.com", profile.all_emails)

        # Future hash must NOT be in profile
        new_hash = "9c40bdbab4944a7f0e06bb210e8990ff340ee998813b17e432000a70f5dfe554"
        self.assertFalse(profile.matches_hash(new_hash))

    def test_07_same_timestamp_events(self):
        """Verify deterministic handling of multiple changes at the exact same timestamp."""
        # audit_59 (email) and audit_60 (email_hash) occur at the exact same second
        profile = self.graph.get_profile("user_523")
        self.assertIsNotNone(profile)
        events_at_time = [
            e for e in profile.audit_events if e.event_time == "2025-09-30T21:50:27Z"
        ]
        self.assertEqual(len(events_at_time), 2)
        # Deterministic sorting by audit_id
        self.assertEqual(events_at_time[0].audit_id, "audit_59")
        self.assertEqual(events_at_time[1].audit_id, "audit_60")

    def test_08_identifier_collision(self):
        """Verify synthetic collision: two users associated with the same email."""
        custom_graph = IdentityGraph()
        identities = [
            {
                "user_id": "user_A",
                "name": "User Alpha",
                "email": "shared@example.com",
                "phone": "+1 555-0101",
                "email_hash": "hashA",
                "updated_at": "2025-01-01T00:00:00Z",
            },
            {
                "user_id": "user_B",
                "name": "User Beta",
                "email": "shared@example.com",
                "phone": "+1 555-0102",
                "email_hash": "hashB",
                "updated_at": "2025-01-01T00:00:00Z",
            },
        ]
        custom_graph.load_from_dicts(identities, [])
        result = custom_graph.resolve_by_email("shared@example.com")
        self.assertEqual(result.status, ResolutionStatus.AMBIGUOUS)
        self.assertIsNone(result.user_id)
        self.assertEqual(result.candidates, ["user_A", "user_B"])
        self.assertIn("ambiguously associated", result.message)

    def test_09_missing_values(self):
        """Verify safe handling of blank, whitespace, and None input parameters."""
        self.assertEqual(self.graph.resolve_by_email(None).status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(self.graph.resolve_by_email("").status, ResolutionStatus.UNRESOLVED)
        self.assertEqual(self.graph.resolve_by_email("   ").status, ResolutionStatus.UNRESOLVED)
        self.assertIsNone(self.graph.get_profile(None))
        self.assertIsNone(self.graph.get_profile(""))
        self.assertIsNone(self.graph.get_profile("nonexistent_id"))

    def test_10_historical_phone_and_hash(self):
        """Verify phone change tracking with provenance."""
        # audit_01: user_485 changed phone from +33 6 24 34 80 16 to +33 6 63 29 68 28
        profile = self.graph.get_profile("user_485")
        self.assertIsNotNone(profile)
        self.assertIn("+33 6 24 34 80 16", profile.all_phones)
        self.assertIn("+33 6 63 29 68 28", profile.all_phones)
        self.assertTrue(profile.matches_phone("+33 6 24 34 80 16"))
        self.assertTrue(profile.matches_phone("+33 6 63 29 68 28"))

        phone_provenance = [
            p for p in profile.provenance if p.field_name == "phone" and p.audit_id == "audit_01"
        ]
        self.assertTrue(len(phone_provenance) >= 1)

    def test_11_boundary_inclusivity(self):
        """Verify that an event occurring EXACTLY at as_of_time is included."""
        # audit_59 occurred at exactly 2025-09-30T21:50:27Z
        exact_time = "2025-09-30T21:50:27Z"
        profile = self.graph.get_profile("user_523", as_of_time=exact_time)
        self.assertIsNotNone(profile)
        # Since it occurred at exact cutoff, the event is included
        audit_ids = [e.audit_id for e in profile.audit_events]
        self.assertIn("audit_59", audit_ids)
        self.assertIn("audit_60", audit_ids)
        self.assertEqual(profile.active_email, "o.walker66@yahoo.com")

    def test_12_input_normalization(self):
        """Verify whitespace stripping and case normalization."""
        self.assertEqual(normalize_email("  User.Test+Tag@Domain.COM  "), "user.test+tag@domain.com")
        self.assertEqual(normalize_text("   some text   "), "some text")
        result = self.graph.resolve_by_email("   amina.elmasri@gmail.com  \n")
        self.assertEqual(result.status, ResolutionStatus.RESOLVED)
        self.assertEqual(result.user_id, "user_01")


if __name__ == "__main__":
    unittest.main()
