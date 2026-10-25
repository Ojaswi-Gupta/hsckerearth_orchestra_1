"""Identity Resolution Engine for Ghost Data Still Exists.

Resolves current and historical user identifiers (emails, phone numbers,
email hashes, names) with temporal cutoff enforcement (as_of_time <= audit_at),
collision detection, and full evidence provenance tracking.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union


class ResolutionStatus(Enum):
    """Status of an identity resolution query."""
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    AMBIGUOUS = "AMBIGUOUS"


def normalize_email(email: Optional[str]) -> str:
    """Normalize email for lookup by stripping surrounding whitespace and lowercasing.
    
    Preserves dots, tags, and structure exactly as required by policy.
    """
    if not email:
        return ""
    return email.strip().lower()


def normalize_text(text: Optional[str]) -> str:
    """Strip whitespace from a general identifier while preserving internal content."""
    if not text:
        return ""
    return text.strip()


def parse_iso_timestamp(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp string into a timezone-aware datetime."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def format_iso_timestamp(dt: Optional[datetime]) -> str:
    """Format a datetime into canonical UTC ISO-8601 string ending with Z."""
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class IdentifierProvenance:
    """Provenance tracking for a specific identifier value."""
    field_name: str
    value: str
    source_type: str  # 'SNAPSHOT' or 'AUDIT_LOG'
    audit_id: Optional[str] = None
    event_time: Optional[str] = None


@dataclass(frozen=True)
class IdentityChangeEvent:
    """Single historical change event from user_audit_log.csv."""
    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass
class UserIdentityProfile:
    """Reconstructed identity profile for a user, temporally bounded by as_of_time."""
    user_id: str
    snapshot_name: str
    snapshot_email: str
    snapshot_phone: str
    snapshot_email_hash: str
    snapshot_updated_at: str
    as_of_time: Optional[str]

    # Active identifier values valid at as_of_time
    active_name: str
    active_email: str
    active_phone: str
    active_email_hash: str

    # Cumulative sets of all valid identifiers known up to as_of_time
    all_emails: Set[str] = field(default_factory=set)           # Lowercased for lookup
    all_emails_original: Set[str] = field(default_factory=set)  # Preserved casing
    all_phones: Set[str] = field(default_factory=set)
    all_names: Set[str] = field(default_factory=set)
    all_email_hashes: Set[str] = field(default_factory=set)

    # Detailed provenance and audit events
    provenance: List[IdentifierProvenance] = field(default_factory=list)
    audit_events: List[IdentityChangeEvent] = field(default_factory=list)
    status: ResolutionStatus = ResolutionStatus.RESOLVED

    def matches_email(self, email: Optional[str]) -> bool:
        """Check if email matches any current or historical email for this user."""
        if not email:
            return False
        return normalize_email(email) in self.all_emails

    def matches_phone(self, phone: Optional[str]) -> bool:
        """Check if phone matches any current or historical phone for this user."""
        if not phone:
            return False
        return normalize_text(phone) in self.all_phones

    def matches_hash(self, email_hash: Optional[str]) -> bool:
        """Check if email_hash matches any current or historical hash for this user."""
        if not email_hash:
            return False
        return normalize_text(email_hash).lower() in {h.lower() for h in self.all_email_hashes}

    def matches_any_identifier(self, value: Optional[str]) -> bool:
        """Check if value matches any known email, phone, name, hash, or user_id."""
        if not value:
            return False
        v_clean = normalize_text(value)
        v_lower = v_clean.lower()
        if v_clean == self.user_id:
            return True
        if v_lower in self.all_emails:
            return True
        if v_clean in self.all_phones:
            return True
        if v_clean in self.all_names:
            return True
        if v_lower in {h.lower() for h in self.all_email_hashes}:
            return True
        return False


@dataclass
class ResolutionResult:
    """Result of an identity resolution query."""
    status: ResolutionStatus
    user_id: Optional[str] = None
    profile: Optional[UserIdentityProfile] = None
    matched_by: Optional[str] = None
    message: Optional[str] = None
    candidates: List[str] = field(default_factory=list)


class IdentityGraph:
    """In-memory identity graph for resolving current & historical user identifiers.
    
    Guarantees:
    1. Case-insensitive email normalization for matching.
    2. Zero leakage of future identity events beyond as_of_time.
    3. Deterministic collision detection returning explicit AMBIGUOUS results.
    4. Exact provenance retention for all historical identifiers.
    """

    def __init__(
        self,
        identities_path: Optional[Union[str, Path]] = None,
        audit_log_path: Optional[Union[str, Path]] = None,
    ) -> None:
        # Raw records
        self._raw_users: Dict[str, Dict[str, str]] = {}  # user_id -> row
        self._raw_audits: List[IdentityChangeEvent] = []  # sorted chronologically
        self._user_audits: Dict[str, List[IdentityChangeEvent]] = {}  # user_id -> events

        if identities_path and audit_log_path:
            self.load_from_csv(identities_path, audit_log_path)

    def load_from_csv(
        self,
        identities_path: Union[str, Path],
        audit_log_path: Union[str, Path],
    ) -> None:
        """Load user identities and audit log from CSV files."""
        id_path = Path(identities_path)
        audit_path = Path(audit_log_path)

        if not id_path.exists():
            raise FileNotFoundError(f"Identities file not found: {id_path}")
        if not audit_path.exists():
            raise FileNotFoundError(f"Audit log file not found: {audit_path}")

        with open(id_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            users = list(reader)

        with open(audit_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            audits = list(reader)

        self.load_from_dicts(users, audits)

    def load_from_dicts(
        self,
        identities: List[Dict[str, str]],
        audits: List[Dict[str, str]],
    ) -> None:
        """Load identity graph from in-memory dictionary lists."""
        self._raw_users.clear()
        self._raw_audits.clear()
        self._user_audits.clear()

        for u in identities:
            uid = normalize_text(u.get("user_id"))
            if uid:
                self._raw_users[uid] = u
                self._user_audits[uid] = []

        parsed_audits: List[IdentityChangeEvent] = []
        for a in audits:
            uid = normalize_text(a.get("user_id"))
            aid = normalize_text(a.get("audit_id"))
            etime = normalize_text(a.get("event_time"))
            fname = normalize_text(a.get("field_name"))
            old_val = normalize_text(a.get("old_value"))
            new_val = normalize_text(a.get("new_value"))

            if uid and aid and etime and fname:
                event = IdentityChangeEvent(
                    audit_id=aid,
                    user_id=uid,
                    event_time=etime,
                    field_name=fname,
                    old_value=old_val,
                    new_value=new_val,
                )
                parsed_audits.append(event)

        # Sort all audit events deterministically by event_time, then audit_id
        parsed_audits.sort(key=lambda e: (e.event_time, e.audit_id))
        self._raw_audits = parsed_audits

        for e in self._raw_audits:
            if e.user_id in self._user_audits:
                self._user_audits[e.user_id].append(e)

    def resolve_by_email(
        self,
        email: Optional[str],
        as_of_time: Optional[Union[str, datetime]] = None,
    ) -> ResolutionResult:
        """Resolve an email address to a user identity profile as of a specified time.
        
        Args:
            email: The email address to look up.
            as_of_time: Temporal cutoff ISO string or datetime. If provided,
                        only events with event_time <= as_of_time are considered.
                        
        Returns:
            ResolutionResult with status RESOLVED, UNRESOLVED, or AMBIGUOUS.
        """
        norm_email = normalize_email(email)
        if not norm_email:
            return ResolutionResult(
                status=ResolutionStatus.UNRESOLVED,
                message="Email argument is blank or empty.",
            )

        cutoff_str = None
        if as_of_time is not None:
            if isinstance(as_of_time, datetime):
                cutoff_str = format_iso_timestamp(as_of_time)
            else:
                cutoff_str = normalize_text(as_of_time)

        # Find all user candidates who owned this email on or before cutoff_str
        candidates: Set[str] = set()
        match_types: Dict[str, str] = {}  # user_id -> match reason

        for uid, user_row in self._raw_users.items():
            profile = self.get_profile(uid, as_of_time=cutoff_str)
            if profile and norm_email in profile.all_emails:
                candidates.add(uid)
                if norm_email == normalize_email(profile.active_email):
                    match_types[uid] = "active_email"
                else:
                    match_types[uid] = "historical_email"

        if not candidates:
            return ResolutionResult(
                status=ResolutionStatus.UNRESOLVED,
                message=f"No user identity found for email: {email}",
            )

        if len(candidates) > 1:
            # Identifier collision: multiple distinct users claimed this email at or before cutoff
            # Check if exactly one actively owned it at as_of_time
            active_owners = [
                uid for uid in candidates if match_types.get(uid) == "active_email"
            ]
            if len(active_owners) == 1:
                winning_uid = active_owners[0]
                profile = self.get_profile(winning_uid, as_of_time=cutoff_str)
                return ResolutionResult(
                    status=ResolutionStatus.RESOLVED,
                    user_id=winning_uid,
                    profile=profile,
                    matched_by="active_email_over_historical_collision",
                    candidates=sorted(list(candidates)),
                )

            return ResolutionResult(
                status=ResolutionStatus.AMBIGUOUS,
                candidates=sorted(list(candidates)),
                message=f"Email '{email}' is ambiguously associated with multiple users: {sorted(list(candidates))}",
            )

        # Exactly one candidate found
        uid = next(iter(candidates))
        profile = self.get_profile(uid, as_of_time=cutoff_str)
        return ResolutionResult(
            status=ResolutionStatus.RESOLVED,
            user_id=uid,
            profile=profile,
            matched_by=match_types.get(uid, "email"),
            candidates=[uid],
        )

    def resolve_by_user_id(
        self,
        user_id: Optional[str],
        as_of_time: Optional[Union[str, datetime]] = None,
    ) -> Optional[UserIdentityProfile]:
        """Convenience alias for get_profile."""
        return self.get_profile(user_id, as_of_time=as_of_time)

    def get_profile(
        self,
        user_id: Optional[str],
        as_of_time: Optional[Union[str, datetime]] = None,
    ) -> Optional[UserIdentityProfile]:
        """Reconstruct the identity profile for a user as of a given timestamp.
        
        Guarantees that:
        - Audit events occurring AFTER as_of_time are completely excluded.
        - Active fields at as_of_time are accurately traced backwards if later transitions occurred.
        - Provenance is recorded for all snapshot and audit values.
        """
        uid = normalize_text(user_id)
        if not uid or uid not in self._raw_users:
            return None

        u_row = self._raw_users[uid]
        cutoff_str = None
        if as_of_time is not None:
            if isinstance(as_of_time, datetime):
                cutoff_str = format_iso_timestamp(as_of_time)
            else:
                cutoff_str = normalize_text(as_of_time)

        # Collect user events
        all_user_events = self._user_audits.get(uid, [])

        # Filter events by temporal boundary
        if cutoff_str:
            past_events = [e for e in all_user_events if e.event_time <= cutoff_str]
            future_events = [e for e in all_user_events if e.event_time > cutoff_str]
        else:
            past_events = list(all_user_events)
            future_events = []

        # Reconstruct active values at cutoff_str
        # If there are future events for a field, the value at cutoff_str was the
        # old_value of the chronologically earliest future event.
        # Otherwise, it matches the latest snapshot in user_identities.csv.
        active_email = self._reconstruct_active_field(
            snapshot_val=u_row.get("email", ""),
            field_name="email",
            future_events=future_events,
        )
        active_phone = self._reconstruct_active_field(
            snapshot_val=u_row.get("phone", ""),
            field_name="phone",
            future_events=future_events,
        )
        active_hash = self._reconstruct_active_field(
            snapshot_val=u_row.get("email_hash", ""),
            field_name="email_hash",
            future_events=future_events,
        )
        active_name = u_row.get("name", "")  # name is not in audit log, remains stable

        # Aggregate cumulative historical identifier sets and provenance up to cutoff_str
        all_emails: Set[str] = set()
        all_emails_orig: Set[str] = set()
        all_phones: Set[str] = set()
        all_names: Set[str] = {active_name} if active_name else set()
        all_hashes: Set[str] = set()
        provenance: List[IdentifierProvenance] = []

        # Add active/snapshot value provenance if known at or before cutoff
        if active_email:
            all_emails.add(normalize_email(active_email))
            all_emails_orig.add(active_email)
            provenance.append(
                IdentifierProvenance(
                    field_name="email",
                    value=active_email,
                    source_type="SNAPSHOT",
                    event_time=u_row.get("updated_at"),
                )
            )

        if active_phone:
            all_phones.add(active_phone)
            provenance.append(
                IdentifierProvenance(
                    field_name="phone",
                    value=active_phone,
                    source_type="SNAPSHOT",
                    event_time=u_row.get("updated_at"),
                )
            )

        if active_hash:
            all_hashes.add(active_hash)
            provenance.append(
                IdentifierProvenance(
                    field_name="email_hash",
                    value=active_hash,
                    source_type="SNAPSHOT",
                    event_time=u_row.get("updated_at"),
                )
            )

        # Incorporate all past audit events
        for e in past_events:
            if e.field_name == "email":
                if e.old_value:
                    all_emails.add(normalize_email(e.old_value))
                    all_emails_orig.add(e.old_value)
                    provenance.append(
                        IdentifierProvenance(
                            field_name="email",
                            value=e.old_value,
                            source_type="AUDIT_LOG",
                            audit_id=e.audit_id,
                            event_time=e.event_time,
                        )
                    )
                if e.new_value:
                    all_emails.add(normalize_email(e.new_value))
                    all_emails_orig.add(e.new_value)
                    provenance.append(
                        IdentifierProvenance(
                            field_name="email",
                            value=e.new_value,
                            source_type="AUDIT_LOG",
                            audit_id=e.audit_id,
                            event_time=e.event_time,
                        )
                    )

            elif e.field_name == "phone":
                if e.old_value:
                    all_phones.add(e.old_value)
                    provenance.append(
                        IdentifierProvenance(
                            field_name="phone",
                            value=e.old_value,
                            source_type="AUDIT_LOG",
                            audit_id=e.audit_id,
                            event_time=e.event_time,
                        )
                    )
                if e.new_value:
                    all_phones.add(e.new_value)
                    provenance.append(
                        IdentifierProvenance(
                            field_name="phone",
                            value=e.new_value,
                            source_type="AUDIT_LOG",
                            audit_id=e.audit_id,
                            event_time=e.event_time,
                        )
                    )

            elif e.field_name == "email_hash":
                if e.old_value:
                    all_hashes.add(e.old_value)
                    provenance.append(
                        IdentifierProvenance(
                            field_name="email_hash",
                            value=e.old_value,
                            source_type="AUDIT_LOG",
                            audit_id=e.audit_id,
                            event_time=e.event_time,
                        )
                    )
                if e.new_value:
                    all_hashes.add(e.new_value)
                    provenance.append(
                        IdentifierProvenance(
                            field_name="email_hash",
                            value=e.new_value,
                            source_type="AUDIT_LOG",
                            audit_id=e.audit_id,
                            event_time=e.event_time,
                        )
                    )

        return UserIdentityProfile(
            user_id=uid,
            snapshot_name=u_row.get("name", ""),
            snapshot_email=u_row.get("email", ""),
            snapshot_phone=u_row.get("phone", ""),
            snapshot_email_hash=u_row.get("email_hash", ""),
            snapshot_updated_at=u_row.get("updated_at", ""),
            as_of_time=cutoff_str,
            active_name=active_name,
            active_email=active_email,
            active_phone=active_phone,
            active_email_hash=active_hash,
            all_emails=all_emails,
            all_emails_original=all_emails_orig,
            all_phones=all_phones,
            all_names=all_names,
            all_email_hashes=all_hashes,
            provenance=provenance,
            audit_events=past_events,
            status=ResolutionStatus.RESOLVED,
        )

    def _reconstruct_active_field(
        self,
        snapshot_val: str,
        field_name: str,
        future_events: List[IdentityChangeEvent],
    ) -> str:
        """Trace field value back to cutoff time using chronologically sorted future events."""
        matching_future = [e for e in future_events if e.field_name == field_name]
        if matching_future:
            # The earliest future event transitioned FROM the active value at cutoff
            return matching_future[0].old_value
        return snapshot_val
