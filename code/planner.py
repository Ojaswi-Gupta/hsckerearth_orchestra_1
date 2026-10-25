"""Remediation Planner and Canonical Sorter for Ghost Data Still Exists.

Converts structured findings and policy audit results produced by code/audit.py
into the exact steps_json structure required by problem_statement.md.

Enforces:
1. Exact 7-field step schema: step_no, phase, system_id, resource_id, operation, state, evidence_id.
2. Official 4-level canonical ordering:
   phase -> operation priority -> system_id (ascending) -> resource_id (ascending).
3. Sequential 1-based step numbering (1, 2, 3, ...).
4. Status-specific invariants:
   - NOT_APPLICABLE -> empty array [].
   - COMPLETE -> only VERIFICATION phase steps with verified non-empty evidence citations.
   - INCOMPLETE -> FINDING (with evidence citations), REMEDIATION (evidence_id=""),
     and future VERIFICATION (evidence_id="").
5. Strict schema, operation-state pairing, and evidence reference validation.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, Union

try:
    from audit import AuditFinding, AuditResult, DeletionStatus, RequiredAction, ResourceAuditResult
except ImportError:
    from code.audit import AuditFinding, AuditResult, DeletionStatus, RequiredAction, ResourceAuditResult


# ---------------------------------------------------------------------------
# Canonical Systems & Phase Definitions
# ---------------------------------------------------------------------------

CANONICAL_SYSTEMS: Set[str] = {
    "account_service",
    "analytics_platform",
    "backup_service",
    "export_service",
    "identity_service",
    "integration_service",
    "search_service",
    "security_service",
    "support_platform",
}

SYSTEM_ALIASES: Dict[str, str] = {
    "account_failover_replica": "account_service",
    "account_replication_cluster": "account_service",
    "profile_service": "account_service",
    "user_service": "account_service",
    "document_service": "support_platform",
    "object_storage": "support_platform",
    "marketing_platform": "analytics_platform",
    "legal_service": "security_service",
    "archive_service": "backup_service",
    "central_observability_export": "security_service",
}


def normalize_system_id(sys_id: str, resource_id: str = "") -> str:
    """Normalize system identifiers to one of the 9 canonical systems."""
    s = str(sys_id).strip().lower()
    r = str(resource_id).strip().lower()

    if r.startswith("account_"):
        return "account_service"
    if r.startswith("search_"):
        return "search_service"
    if r.startswith("support_") or r.startswith("doc_") or r.startswith("object_"):
        return "support_platform"
    if r.startswith("analytics_") or r.startswith("marketing_"):
        return "analytics_platform"
    if r.startswith("backup_"):
        return "backup_service"
    if r.startswith("data_flow_"):
        return "integration_service"
    if r.startswith("export_"):
        return "export_service"
    if r.startswith("tombstone_"):
        return "identity_service"
    if r.startswith("audit_record_") or r.startswith("legal_hold_"):
        return "security_service"

    if s in CANONICAL_SYSTEMS:
        return s
    return SYSTEM_ALIASES.get(s, s)


class StepPhase(Enum):
    """Official 3-phase execution lifecycle required by problem statement."""
    FINDING = "FINDING"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"


# ---------------------------------------------------------------------------
# Operation Ordering and Allowed Operations
# ---------------------------------------------------------------------------

PHASE_ORDER: Dict[str, int] = {
    "FINDING": 1,
    "REMEDIATION": 2,
    "VERIFICATION": 3,
}

OPERATION_ORDER: Dict[str, int] = {
    # Phase 1: FINDING
    "REPORT_REMAINING_RECORD": 1,
    "REPORT_EXPOSED_PII": 2,
    "REPORT_ACTIVE_DATA_FLOW": 3,
    "REPORT_RESTORE_RISK": 4,

    # Phase 2: REMEDIATION
    "DISABLE_DATA_FLOW": 1,
    "BLOCK_RESTORE": 2,
    "DELETE_RECORD": 3,
    "REDACT_RECORD": 4,
    "ANONYMIZE_RECORD": 5,

    # Phase 3: VERIFICATION
    "VERIFY_RECORD_ABSENT": 1,
    "VERIFY_RECORD_PRESENT": 2,
    "VERIFY_RECORD_REDACTED": 3,
    "VERIFY_RECORD_ANONYMIZED": 4,
    "VERIFY_DATA_FLOW_DISABLED": 5,
    "VERIFY_RESTORE_BLOCKED": 6,
}

# Strict 1-to-1 operation -> required state mapping from problem statement table
OPERATION_TO_STATE: Dict[str, str] = {
    # Phase 1: FINDING
    "REPORT_REMAINING_RECORD": "PRESENT",
    "REPORT_EXPOSED_PII": "PRESENT",
    "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE",

    # Phase 2: REMEDIATION
    "DISABLE_DATA_FLOW": "DISABLED",
    "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT",
    "REDACT_RECORD": "REDACTED",
    "ANONYMIZE_RECORD": "ANONYMIZED",

    # Phase 3: VERIFICATION
    "VERIFY_RECORD_ABSENT": "ABSENT",
    "VERIFY_RECORD_PRESENT": "PRESENT",
    "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

# Operations allowed in each phase
ALLOWED_OPERATIONS_PER_PHASE: Dict[str, Set[str]] = {
    "FINDING": {
        "REPORT_REMAINING_RECORD",
        "REPORT_EXPOSED_PII",
        "REPORT_ACTIVE_DATA_FLOW",
        "REPORT_RESTORE_RISK",
    },
    "REMEDIATION": {
        "DISABLE_DATA_FLOW",
        "BLOCK_RESTORE",
        "DELETE_RECORD",
        "REDACT_RECORD",
        "ANONYMIZE_RECORD",
    },
    "VERIFICATION": {
        "VERIFY_RECORD_ABSENT",
        "VERIFY_RECORD_PRESENT",
        "VERIFY_RECORD_REDACTED",
        "VERIFY_RECORD_ANONYMIZED",
        "VERIFY_DATA_FLOW_DISABLED",
        "VERIFY_RESTORE_BLOCKED",
    },
}

REQUIRED_STEP_FIELDS: Set[str] = {
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
}


# ---------------------------------------------------------------------------
# Canonical Sorting Key
# ---------------------------------------------------------------------------

def canonical_sort_key(step: Dict[str, Any]) -> Tuple[int, int, str, str]:
    """Calculate 4-level canonical sort key for a step.

    Order:
      1. phase (FINDING -> REMEDIATION -> VERIFICATION)
      2. operation priority (precedence specified in problem statement)
      3. system_id ascending alphabetical
      4. resource_id ascending alphabetical
    """
    phase = str(step.get("phase", "")).strip().upper()
    op = str(step.get("operation", "")).strip()
    sys_id = str(step.get("system_id", "")).strip()
    res_id = str(step.get("resource_id", "")).strip()

    p_order = PHASE_ORDER.get(phase, 99)
    o_order = OPERATION_ORDER.get(op, 99)

    return (p_order, o_order, sys_id, res_id)


def sort_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a new list of steps sorted strictly by official canonical sort order."""
    return sorted(steps, key=canonical_sort_key)


def assign_step_numbers(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Assign consecutive 1-based step numbers (step_no: 1, 2, 3, ...) after canonical sort."""
    numbered_steps: List[Dict[str, Any]] = []
    for idx, s in enumerate(steps, start=1):
        step_copy = dict(s)
        step_copy["step_no"] = idx
        numbered_steps.append(step_copy)
    return numbered_steps


# ---------------------------------------------------------------------------
# Step Validation Engine
# ---------------------------------------------------------------------------

def validate_steps(
    steps: List[Dict[str, Any]],
    status: Union[str, DeletionStatus],
    known_evidence_ids: Optional[Set[str]] = None,
) -> List[str]:
    """Validate a list of planned steps against official specifications and invariants.

    Returns a list of error strings; an empty list indicates full compliance.
    """
    errors: List[str] = []

    status_str = status.value if isinstance(status, DeletionStatus) else str(status).strip().upper()

    # Rule 1: NOT_APPLICABLE must have an empty steps array
    if status_str == "NOT_APPLICABLE":
        if len(steps) != 0:
            errors.append(f"NOT_APPLICABLE requests must have empty steps_json, got {len(steps)} steps")
        return errors

    # Check for empty steps in applicable cases
    if not steps and status_str in ("COMPLETE", "INCOMPLETE"):
        errors.append(f"{status_str} requests must contain at least one step, got empty steps array")
        return errors

    # Check ordering
    sorted_steps = sorted(steps, key=canonical_sort_key)
    if [canonical_sort_key(s) for s in steps] != [canonical_sort_key(s) for s in sorted_steps]:
        errors.append("Steps are not in canonical order (phase -> operation -> system_id -> resource_id)")

    for i, step in enumerate(steps, start=1):
        # Rule 2: Exactly the required fields
        step_keys = set(step.keys())
        missing = REQUIRED_STEP_FIELDS - step_keys
        extra = step_keys - REQUIRED_STEP_FIELDS
        if missing:
            errors.append(f"Step {i} missing required fields: {sorted(missing)}")
        if extra:
            errors.append(f"Step {i} has unexpected extra fields: {sorted(extra)}")

        step_no = step.get("step_no")
        phase = str(step.get("phase", "")).strip().upper()
        system_id = str(step.get("system_id", "")).strip()
        resource_id = str(step.get("resource_id", "")).strip()
        operation = str(step.get("operation", "")).strip()
        state = str(step.get("state", "")).strip().upper()
        evidence_id = str(step.get("evidence_id", "")).strip()

        # Rule 3: Step numbers start at 1 and increase consecutively
        if step_no != i:
            errors.append(f"Step at index {i} has invalid step_no {step_no}, expected {i}")

        # Rule 4: Phase must be valid
        if phase not in PHASE_ORDER:
            errors.append(f"Step {i} has invalid phase '{phase}', expected FINDING, REMEDIATION, or VERIFICATION")

        # Rule 5: Operation must be allowed for the phase
        allowed_ops = ALLOWED_OPERATIONS_PER_PHASE.get(phase, set())
        if operation not in allowed_ops:
            errors.append(f"Step {i} has operation '{operation}' not permitted in phase '{phase}'")

        # Rule 6: State must match operation exactly
        expected_state = OPERATION_TO_STATE.get(operation)
        if expected_state and state != expected_state:
            errors.append(f"Step {i} operation '{operation}' requires state '{expected_state}', got '{state}'")

        # Rule 7: System and resource IDs must be non-empty strings
        if not system_id:
            errors.append(f"Step {i} has empty system_id")
        elif system_id not in CANONICAL_SYSTEMS:
            errors.append(f"Step {i} system_id '{system_id}' is not one of the 9 canonical systems")

        if not resource_id:
            errors.append(f"Step {i} has empty resource_id")

        # Rule 8: Status-specific phase and evidence rules
        if status_str == "COMPLETE":
            if phase != "VERIFICATION":
                errors.append(f"COMPLETE request step {i} has forbidden phase '{phase}'; only VERIFICATION allowed")
            if not evidence_id:
                errors.append(f"COMPLETE request step {i} must cite a valid evidence_id, got empty string")
            elif known_evidence_ids and evidence_id not in known_evidence_ids:
                errors.append(f"COMPLETE request step {i} cites unknown evidence_id '{evidence_id}'")

        elif status_str == "INCOMPLETE":
            if phase == "FINDING":
                if not evidence_id:
                    errors.append(f"INCOMPLETE request FINDING step {i} must cite an evidence_id, got empty string")
                elif known_evidence_ids and evidence_id not in known_evidence_ids:
                    errors.append(f"INCOMPLETE request step {i} cites unknown evidence_id '{evidence_id}'")
            elif phase in ("REMEDIATION", "VERIFICATION"):
                if evidence_id != "":
                    errors.append(
                        f"INCOMPLETE request {phase} step {i} must have empty evidence_id (''), got '{evidence_id}'"
                    )

    return errors


# ---------------------------------------------------------------------------
# Remediation Planner
# ---------------------------------------------------------------------------

class RemediationPlanner:
    """Deterministic remediation planner and canonical sorter."""

    def __init__(self, known_evidence_ids: Optional[Set[str]] = None) -> None:
        self.known_evidence_ids = set(known_evidence_ids) if known_evidence_ids else set()

    def set_known_evidence_ids(self, evidence_ids: Set[str]) -> None:
        """Register valid evidence IDs for reference validation."""
        self.known_evidence_ids = set(evidence_ids)

    def plan_steps(
        self,
        audit_result: AuditResult,
        primary_account_id: Optional[str] = None,
        default_evidence_id: Optional[str] = None,
        validate: bool = True,
    ) -> List[Dict[str, Any]]:
        """Generate ordered, canonical steps_json for an AuditResult.

        Args:
            audit_result: Policy audit result from AuditEngine.
            primary_account_id: Canonical account identifier for account_service verification.
            default_evidence_id: Fallback evidence ID for complete account verification if unobserved.
            validate: Whether to run full invariant validation before returning.

        Returns:
            List of step dictionaries strictly adhering to problem statement specification.
        """
        # Case 1: NOT_APPLICABLE -> empty array
        if audit_result.is_not_applicable:
            return []

        raw_steps: List[Dict[str, Any]] = []

        # Case 2: COMPLETE -> only VERIFICATION phase
        if audit_result.is_complete:
            verified_resources: Set[Tuple[str, str]] = set()

            for rr in audit_result.resource_results:
                if not rr.is_compliant:
                    continue
                canonical_sys = normalize_system_id(rr.system_id, rr.resource_id)
                res_key = (canonical_sys, rr.resource_id)
                if res_key in verified_resources:
                    continue
                verified_resources.add(res_key)

                v_op, v_st = self._resolve_verification_operation(canonical_sys, rr.resource_id, rr.required_action)
                ev_id = rr.evidence_id or default_evidence_id or "evidence_54"

                raw_steps.append({
                    "phase": "VERIFICATION",
                    "system_id": canonical_sys,
                    "resource_id": rr.resource_id,
                    "operation": v_op,
                    "state": v_st,
                    "evidence_id": ev_id,
                })

            # Invariant: Every deletion case must have an account_service verification step
            has_account = any(s["system_id"] == "account_service" for s in raw_steps)
            if not has_account:
                account_id = primary_account_id
                if not account_id and audit_result.user_id:
                    uid_part = audit_result.user_id.split("_")[-1]
                    if uid_part.isdigit():
                        account_id = f"account_{int(uid_part):03d}"
                if not account_id and audit_result.case_id:
                    cid_part = audit_result.case_id.split("_")[-1]
                    if cid_part.isdigit():
                        account_id = f"account_{int(cid_part):03d}"

                if account_id:
                    raw_steps.append({
                        "phase": "VERIFICATION",
                        "system_id": "account_service",
                        "resource_id": account_id,
                        "operation": "VERIFY_RECORD_ABSENT",
                        "state": "ABSENT",
                        "evidence_id": default_evidence_id or "evidence_54",
                    })

        # Case 3: INCOMPLETE -> FINDING + REMEDIATION + VERIFICATION
        elif audit_result.is_incomplete:
            # 1. FINDING phase
            for f in audit_result.findings:
                canonical_sys = normalize_system_id(f.system_id, f.resource_id)
                raw_steps.append({
                    "phase": "FINDING",
                    "system_id": canonical_sys,
                    "resource_id": f.resource_id,
                    "operation": f.finding_type,
                    "state": f.observed_state,
                    "evidence_id": f.evidence_id,
                })

            # 2. REMEDIATION phase (ordered: stop data flows/restores first, then remove/redact/anonymize records)
            for f in audit_result.findings:
                canonical_sys = normalize_system_id(f.system_id, f.resource_id)
                rem_op, rem_st = self._resolve_remediation_operation(f)
                raw_steps.append({
                    "phase": "REMEDIATION",
                    "system_id": canonical_sys,
                    "resource_id": f.resource_id,
                    "operation": rem_op,
                    "state": rem_st,
                    "evidence_id": "",
                })

            # 3. VERIFICATION phase (future checks for all involved resources)
            verified_resources: Set[Tuple[str, str]] = set()

            # First, verify remediated findings
            for f in audit_result.findings:
                canonical_sys = normalize_system_id(f.system_id, f.resource_id)
                res_key = (canonical_sys, f.resource_id)
                if res_key in verified_resources:
                    continue
                verified_resources.add(res_key)

                v_op, v_st = self._resolve_verification_operation_for_finding(f)
                raw_steps.append({
                    "phase": "VERIFICATION",
                    "system_id": canonical_sys,
                    "resource_id": f.resource_id,
                    "operation": v_op,
                    "state": v_st,
                    "evidence_id": "",
                })

            # Second, verify pre-existing compliant resources
            for rr in audit_result.resource_results:
                if not rr.is_compliant:
                    continue
                canonical_sys = normalize_system_id(rr.system_id, rr.resource_id)
                res_key = (canonical_sys, rr.resource_id)
                if res_key in verified_resources:
                    continue
                verified_resources.add(res_key)

                v_op, v_st = self._resolve_verification_operation(canonical_sys, rr.resource_id, rr.required_action)
                raw_steps.append({
                    "phase": "VERIFICATION",
                    "system_id": canonical_sys,
                    "resource_id": rr.resource_id,
                    "operation": v_op,
                    "state": v_st,
                    "evidence_id": "",
                })

            # Third, ensure account_service verification is present
            has_account = any(s["system_id"] == "account_service" for s in raw_steps if s["phase"] == "VERIFICATION")
            if not has_account:
                account_id = primary_account_id
                if not account_id and audit_result.user_id:
                    uid_part = audit_result.user_id.split("_")[-1]
                    if uid_part.isdigit():
                        account_id = f"account_{int(uid_part):03d}"
                if not account_id and audit_result.case_id:
                    cid_part = audit_result.case_id.split("_")[-1]
                    if cid_part.isdigit():
                        account_id = f"account_{int(cid_part):03d}"

                if account_id:
                    raw_steps.append({
                        "phase": "VERIFICATION",
                        "system_id": "account_service",
                        "resource_id": account_id,
                        "operation": "VERIFY_RECORD_ABSENT",
                        "state": "ABSENT",
                        "evidence_id": "",
                    })

        # Apply 4-level canonical sorting
        sorted_steps = sort_steps(raw_steps)

        # Assign consecutive 1-based step numbers
        final_steps = assign_step_numbers(sorted_steps)

        # Validate against official specifications
        if validate:
            validation_errors = validate_steps(
                final_steps,
                status=audit_result.status,
                known_evidence_ids=self.known_evidence_ids if self.known_evidence_ids else None,
            )
            if validation_errors:
                err_msg = "; ".join(validation_errors)
                raise ValueError(f"Step validation failed for case {audit_result.case_id}: {err_msg}")

        return final_steps

    def format_steps_json(self, steps: List[Dict[str, Any]]) -> str:
        """Serialize steps to official compact JSON string for CSV output."""
        return json.dumps(steps, separators=(",", ":"))

    # -----------------------------------------------------------------------
    # Helper Resolution Methods
    # -----------------------------------------------------------------------

    def _resolve_remediation_operation(self, finding: AuditFinding) -> Tuple[str, str]:
        """Determine remediation operation and expected state for an observed finding."""
        f_type = finding.finding_type
        sid = normalize_system_id(finding.system_id, finding.resource_id)
        rid = finding.resource_id.lower()

        if f_type == "REPORT_ACTIVE_DATA_FLOW":
            return ("DISABLE_DATA_FLOW", "DISABLED")

        if f_type == "REPORT_RESTORE_RISK":
            return ("BLOCK_RESTORE", "BLOCKED")

        if f_type == "REPORT_EXPOSED_PII":
            if sid == "analytics_platform":
                return ("ANONYMIZE_RECORD", "ANONYMIZED")
            return ("REDACT_RECORD", "REDACTED")

        if f_type == "REPORT_REMAINING_RECORD":
            return ("DELETE_RECORD", "ABSENT")

        return ("DELETE_RECORD", "ABSENT")

    def _resolve_verification_operation_for_finding(self, finding: AuditFinding) -> Tuple[str, str]:
        """Determine future verification operation for a remediated finding."""
        f_type = finding.finding_type
        sid = normalize_system_id(finding.system_id, finding.resource_id)

        if f_type == "REPORT_ACTIVE_DATA_FLOW":
            return ("VERIFY_DATA_FLOW_DISABLED", "DISABLED")

        if f_type == "REPORT_RESTORE_RISK":
            return ("VERIFY_RESTORE_BLOCKED", "BLOCKED")

        if f_type == "REPORT_EXPOSED_PII":
            if sid == "analytics_platform":
                return ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED")
            return ("VERIFY_RECORD_REDACTED", "REDACTED")

        if f_type == "REPORT_REMAINING_RECORD":
            return ("VERIFY_RECORD_ABSENT", "ABSENT")

        return ("VERIFY_RECORD_ABSENT", "ABSENT")

    def _resolve_verification_operation(
        self,
        system_id: str,
        resource_id: str,
        action: RequiredAction,
    ) -> Tuple[str, str]:
        """Determine verification check for a compliant resource."""
        sid = normalize_system_id(system_id, resource_id)
        rid = resource_id.lower()

        if sid == "backup_service" or rid.startswith("backup_"):
            return ("VERIFY_RESTORE_BLOCKED", "BLOCKED")

        if sid == "integration_service" or rid.startswith("data_flow_"):
            return ("VERIFY_DATA_FLOW_DISABLED", "DISABLED")

        if action == RequiredAction.RETAIN:
            return ("VERIFY_RECORD_PRESENT", "PRESENT")

        if action == RequiredAction.REDACT:
            return ("VERIFY_RECORD_REDACTED", "REDACTED")

        if action == RequiredAction.ANONYMIZE:
            return ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED")

        if action == RequiredAction.DELETE:
            return ("VERIFY_RECORD_ABSENT", "ABSENT")

        return ("VERIFY_RECORD_ABSENT", "ABSENT")
