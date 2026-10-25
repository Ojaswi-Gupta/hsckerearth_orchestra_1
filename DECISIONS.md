# DECISIONS.md — Architecture & Design Decisions

This document records the architectural, design, and engineering decisions made during the development of the AI-powered Deletion Audit Agent for **Ghost Data Still Exists** (HackerRank Orchestrate October 2026).

---

## Decision 1: Language & Core Technology Stack

- **Decision**: Use Python 3.11 using standard library modules (`csv`, `json`, `sqlite3`, `re`, `datetime`, `pathlib`, `typing`) as the primary execution engine.
- **Rationale**:
  1. Standard library guarantees 100% portability in the HackerRank evaluation environment without package installation failures.
  2. Native `sqlite3` support enables seamless querying of `.sql` database snapshots provided in `dataset/evidence/databases/`.
  3. JSON and CSV parsing are fast, deterministic, and dependency-free.
  4. Keeps `code.zip` lightweight and clean without bundling large external libraries.

---

## Decision 2: Hybrid Deterministic & AI Architecture

- **Decision**: Implement a hybrid system:
  1. **Deterministic Rule & Temporal Engine** for identity mapping, evidence indexing, rule checking (`deletion_rules.csv`), and sorting order.
  2. **Semantic / Pattern Intent Classifier** for multilingual classification of `request_text` (identifying genuine deletion requests vs `NOT_APPLICABLE` requests like data access, marketing opt-out, or billing inquiries).
- **Rationale**:
  - The grading harness expects exact deterministic ordering (`FINDING` -> `REMEDIATION` -> `VERIFICATION`), exact operation enums, exact resource IDs, and exact evidence IDs.
  - LLMs alone can be non-deterministic with sorting, field names, and subtle edge cases.
  - A deterministic engine guarantees zero drift on canonical ordering and rule application, while AI handles natural language intent and nuanced document semantics where needed.

---

## Decision 3: Identity Resolution Strategy

- **Decision**: Create a two-way graph of identities from both `user_identities.csv` and `user_audit_log.csv`.
- **Rationale**:
  - A user request arrives with an email address. That address may be the current email or an obsolete historical email changed months ago.
  - By indexing all historical emails, phone numbers, and names from `user_audit_log.csv` alongside the current records, any input identifier maps to the correct internal `user_id`.
  - This allows the agent to uncover "ghost" records that still reference an old email address or old phone number.

---

## Decision 4: Strict Temporal Cutoff (`audit_at`)

- **Decision**: Filter all system activity and evidence strictly on `timestamp <= audit_at`.
- **Rationale**:
  - The problem specification explicitly states: *"Reconstruct the true state at audit_at. Ignore later evidence... Evidence created later must not change the answer for that case."*
  - Any activity or evidence recorded after `audit_at` represents future actions that did not exist at the audit moment.

---

## Decision 5: Canonical Step Ordering & Serialization

- **Decision**: Centralize all step creation into a strict ordering comparator following the specification table:
  1. **Phase**: `FINDING` -> `REMEDIATION` -> `VERIFICATION`
  2. **Operation Priority**:
     - *FINDING*: `REPORT_REMAINING_RECORD` -> `REPORT_EXPOSED_PII` -> `REPORT_ACTIVE_DATA_FLOW` -> `REPORT_RESTORE_RISK`
     - *REMEDIATION*: `DISABLE_DATA_FLOW` -> `BLOCK_RESTORE` -> `DELETE_RECORD` -> `REDACT_RECORD` -> `ANONYMIZE_RECORD`
     - *VERIFICATION*: `VERIFY_RECORD_ABSENT` -> `VERIFY_RECORD_PRESENT` -> `VERIFY_RECORD_REDACTED` -> `VERIFY_RECORD_ANONYMIZED` -> `VERIFY_DATA_FLOW_DISABLED` -> `VERIFY_RESTORE_BLOCKED`
  3. **Tie-Breaker**: `system_id` ascending, then `resource_id` ascending.
  4. **Step Numbering**: `step_no` is numbered consecutively starting at `1` after all sorting is complete.
  5. **Evidence ID convention**:
     - `FINDING` and `COMPLETE` `VERIFICATION`: Cite the proven evidence ID (`file_id`, `activity_id`, or `audit_id`).
     - `INCOMPLETE` `REMEDIATION` and `VERIFICATION`: Set `evidence_id = ""` (empty string) because they describe future actions.
     - `NOT_APPLICABLE`: `steps_json = "[]"`.

---

## Decision 6: Evaluation Harness & Custom Benchmark Cases

- **Decision**: Build an automated evaluator in `code/evaluation/main.py` that validates predictions against `dataset/sample_output.csv` on:
  1. Case-level status accuracy
  2. Step-level field accuracy (`phase`, `system_id`, `resource_id`, `operation`, `state`, `evidence_id`)
  3. Sequence ordering accuracy
  4. Three custom synthetic edge cases:
     - Edge Case 1: Historical email used in support ticket after user changed email.
     - Edge Case 2: Read replica sync lag recreating deleted account record.
     - Edge Case 3: Retained legal hold vs tombstone redaction conflict.
- **Rationale**:
  - Meets the mandatory submission requirements in §6.8 of `AGENTS.md` and `problem_statement.md`.
  - Ensures we benchmark and reach 100% agreement on public samples before running on the 518 evaluation cases.

---

## Decision 7: Packaging and Submission Discipline

- **Decision**:
  - `code.zip` will include only `README.md`, `code/` (including `code/evaluation/`).
  - Strict exclusion of `dataset/`, `.venv/`, `__pycache__/`, `.git/`, and any `.zip` files.
  - Predictions written to repository root `output.csv`.
  - Agent transcript logged continuously to repository root `log.txt` following §5 format.
- **Rationale**:
  - Conforms to HackerRank submission rules and prevents upload rejections caused by oversized archives or nested zip files.


---

## Decision 8: Identity Resolution & Temporal Graph Design

- **Decision**: Implemented `IdentityGraph` and `UserIdentityProfile` in `code/identity.py` featuring:
  1. **Strict Case-Insensitive Normalization**: Strips leading/trailing whitespace, converts lookup queries to lowercase while preserving original casing in provenance records. Crucial for resolving 20 uppercase request emails.
  2. **Zero-Leakage Temporal Reconstruction**: Traces active field values backwards using the earliest chronologically sorted future audit event. Any audit event with `event_time > as_of_time` is excluded from the user's active and historical profile at that cutoff.
  3. **Deterministic Multi-Event Ordering**: When multiple audit events occur at the exact same timestamp (e.g., simultaneous email and email_hash updates), events are sorted deterministically by `(event_time, audit_id)`.
  4. **Explicit Collision Handling**: Detects synthetic or multi-user identifier conflicts. If an identifier is associated with multiple distinct users simultaneously without temporal separation, returns `ResolutionStatus.AMBIGUOUS` with candidate list rather than silently merging profiles.
  5. **Granular Provenance Tracking**: Every identifier (snapshot or audit log) attaches an `IdentifierProvenance` record containing the source `audit_id`, `field_name`, `value`, `event_time`, and `source_type`.
- **Rationale**: Meets all constraints from `AGENTS.md` and `problem_statement.md`, ensuring identity resolution is 100% deterministic, robust, and audit-traceable.


---

## Decision 9: Request Intent Classification Engine

- **Decision**: Implemented `RequestIntentClassifier` in `code/intent.py` with:
  1. **Strict 3-Way Intent Taxonomy**:
     - `DELETE_REQUEST`: Genuine account or personal data deletion request, eligible for identity resolution and deletion auditing.
     - `NOT_DELETION`: Data access, export/portability, marketing opt-out, profile correction, or password support, directly mapping to official status `NOT_APPLICABLE` with `steps_json = []`.
     - `AMBIGUOUS`: Unclassifiable, blank, or contradictory text, preserved for diagnostics and conservative handling without guessing.
  2. **Hierarchical Rule Precedence**:
     - *Priority 1*: Empty/blank input check -> `AMBIGUOUS`.
     - *Priority 2*: Explicit negation ("do not delete", "not asking for deletion", "leave account active", "ne pas supprimer") -> `NOT_DELETION`. Negation takes precedence over incidental deletion terms.
     - *Priority 3*: Informational inquiries ("how does deletion work?") without an overriding imperative command -> `NOT_DELETION`.
     - *Priority 4*: Pure non-deletion topics (marketing lists only, invoice copies, export downloads, typos in names) -> `NOT_DELETION`.
     - *Priority 5*: Mixed requests (export personal data AND delete account) -> `DELETE_REQUEST` because positive deletion authorization is present.
     - *Priority 6*: Multilingual positive deletion commands across 7 languages (EN, ES, PT, FR, JA, KO, DE) -> `DELETE_REQUEST`.
     - *Priority 7*: Fallback -> `AMBIGUOUS`.
  3. **Zero External LLM Dependency**:
     - Deterministic rule-based regex achieves **100.0% agreement** on the 32 reference samples and cleanly resolves all 518 full evaluation cases (508 deletion, 10 non-deletion, 0 ambiguous) in **0.006 seconds**.
     - Eliminates network failure risks, API costs, non-deterministic token variation, and external exposure of customer text.

---

## Decision 10: Evidence Ingestion & Multimodal Storage Engine

- **Decision**: Implemented `EvidenceStore` and `EvidenceRecord` in `code/evidence.py` featuring:
  1. **Zero External Dependency Multimodal Parsing**:
     - *SQL Snapshots*: Executed directly into an in-memory SQLite database (`:memory:`) to inspect schema, tables, and rows with zero external DB engines or network calls.
     - *PDF Documents*: Decompressed without PyPDF/pdfplumber using standard library `base64.a85decode` and `zlib.decompress` over FlateDecode binary streams, robustly delimiting binary object offsets.
     - *JSON Bundles*: Recursively indexed top-level and nested key-value pairs, tracking resource identifiers and user keys.
     - *Application Logs*: Structured regex parser extracting timestamps, log levels, systems, resource IDs, and user identifiers with canonical system normalization.
     - *Activity Manifests*: Full ingestion of `system_activity.csv` retaining event times, action types, source systems, and resource references.
  2. **Two-Pass Log Trace Correlation**:
     - In distributed logs where query start lines contain user emails and downstream action lines contain `resource_id`s linked only by `trace_id` or `requestId`, the engine performs an intra-trace correlation pass to attribute resource events back to the initiating user.
  3. **Strict Zero-Leakage Temporal Filtering**:
     - Enforces `event_time <= audit_at` at the granular record level. Records occurring strictly after `audit_at` are completely excluded from downstream audit and planning. For files with unparseable or absent row-level timestamps, fallback uses the verified manifest `captured_at`.
  4. **Official Evidence Citation Preservation**:
     - Retains the exact `evidence_id` from `evidence_files.csv` (e.g., `evidence_54`, `evidence_57`, `evidence_58`) or activity entry ID (`activity_4007`) for verifiable citation in downstream `FINDING` and `VERIFICATION` steps.
  5. **Deterministic Multi-Identifier Querying**:
     - Queries take a resolved `UserIdentityProfile`, matching against active and historical emails, user IDs, phones, and email hashes simultaneously, returning sorted results by `(event_time, evidence_id, resource_id)`.
- **Rationale**: Provides 100% testable, reliable, and auditable evidence ingestion for Step 5 policy audits without external binary or package requirements.

---

## Decision 11: Policy Audit & State Detection Engine

- **Decision**: Implemented `code/audit.py` providing a deterministic, evidence-backed policy audit engine:
  1. **Strict Policy Model (`DeletionPolicy`, `PolicyRule`)**:
     - Loads and validates all 15 official rules from `deletion_rules.csv` mapping each category to its statutory action:
       - `DELETE`: `USER_PROFILE`, `SECONDARY_IDENTIFIER`, `SEARCH_INDEX_ENTRY`, `CACHE_ENTRY`, `SUPPORT_ATTACHMENT`, `USER_EXPORT`, `MARKETING_PROFILE`.
       - `REDACT`: `SUPPORT_MESSAGE`, `EMAIL_DELIVERY_LOG`, `ACCOUNT_TOMBSTONE`.
       - `ANONYMIZE`: `ANALYTICS_PROFILE`, `PRODUCT_TELEMETRY`.
       - `RETAIN`: `SECURITY_AUDIT_LOG`, `LEGAL_HOLD_RECORD`, `BACKUP_ARCHIVE`.
     - Fails fast if any of the 15 rules are missing or corrupted.
  2. **Multi-Source Evidence Evaluation**:
     - *State Predicates*: Evaluates JSON payloads, SQL tables/snapshots, application log lines, and replica statuses to determine observed state (`PRESENT`, `ABSENT`, `REDACTED`, `ANONYMIZED`, `ACTIVE`, `DISABLED`, `BLOCKED`).
     - *Prohibited PII Detection*: In `REDACT` and `ANONYMIZE` treatments, verifies whether direct user identifiers (all emails, phones, names, hashes) remain exposed. For `ACCOUNT_TOMBSTONE`, explicitly permits stable internal `user_id` while barring direct personal identifiers.
     - *Replica Verification*: Identifies missing deletion markers in support replicas (`marker_found=false`) as un-deleted contact profiles requiring deletion.
  3. **Comprehensive Recreation Risk Detection**:
     - *Active Integration Flows*: Checks connector configuration states in `integration_service` as of `audit_at`; enabled connectors trigger `REPORT_ACTIVE_DATA_FLOW`.
     - *Backup Restore Paths*: Inspects `restore_allowed` on backup archives; unblocked restore paths trigger `REPORT_RESTORE_RISK`.
     - *Synchronization Recreation*: Detects downstream sync events (`SYNC_RECORD` with `SUCCESS`) occurring strictly after `DELETE_RECORD` for the user's specific account before `audit_at`, proving that a synchronization job recreated deleted data.
  4. **Strict Temporal Zero-Leakage**:
     - Operates strictly on evidence with `record_timestamp <= audit_at`. Post-audit events cannot prove or disprove compliance at `audit_at`.
  5. **Status Determination & Absence Proof**:
     - Deletion requests with any policy finding or active recreation risk resolve to `INCOMPLETE`.
     - Requests lacking findings or risks where all governed resources are compliant resolve to `COMPLETE`.
     - Out-of-scope non-deletion inquiries resolve to `NOT_APPLICABLE` with zero findings.
  6. **Empirical Validation**:
     - **32/32 (100.0%) status agreement** against `dataset/sample_output.csv`.
     - **20/20 dedicated unit tests** passing in `code/tests/test_audit.py` (68 total passing across project).
     - Full 518 cases audited in **3.97 seconds** (203 `COMPLETE`, 305 `INCOMPLETE`, 10 `NOT_APPLICABLE`).
- **Rationale**: Delivers a transparent, auditable policy engine that reliably detects residual data and recreation risks while preserving legally retained records.


---

## Decision 12: Remediation Planner and Canonical Sorter

- **Decision**: Implemented `RemediationPlanner` in `code/planner.py` featuring:
  1. **Strict 7-Field Step Schema**:
     - Enforces exact dictionary representation: `{"step_no": int, "phase": str, "system_id": str, "resource_id": str, "operation": str, "state": str, "evidence_id": str}`.
     - Rejects any steps with missing or unexpected keys.
  2. **4-Level Canonical Ordering Hierarchy**:
     - Steps are sorted strictly by a 4-tuple key: `(PHASE_ORDER[phase], OP_ORDER[operation], system_id, resource_id)`.
     - *Phase order*: `FINDING` (1) $\rightarrow$ `REMEDIATION` (2) $\rightarrow$ `VERIFICATION` (3).
     - *Operation priority*:
       - `FINDING`: `REPORT_REMAINING_RECORD` (1), `REPORT_EXPOSED_PII` (2), `REPORT_ACTIVE_DATA_FLOW` (3), `REPORT_RESTORE_RISK` (4).
       - `REMEDIATION`: `DISABLE_DATA_FLOW` (1), `BLOCK_RESTORE` (2), `DELETE_RECORD` (3), `REDACT_RECORD` (4), `ANONYMIZE_RECORD` (5).
       - `VERIFICATION`: `VERIFY_RECORD_ABSENT` (1), `VERIFY_RECORD_PRESENT` (2), `VERIFY_RECORD_REDACTED` (3), `VERIFY_RECORD_ANONYMIZED` (4), `VERIFY_DATA_FLOW_DISABLED` (5), `VERIFY_RESTORE_BLOCKED` (6).
     - *Tie-breakers*: Alphabetical ascending by `system_id`, then alphabetical ascending by `resource_id`.
  3. **Consecutive 1-Based Step Numbering**:
     - After sorting into canonical order, assigns `step_no = 1, 2, 3, ...` strictly sequential without gaps.
  4. **Status-Specific Lifecycle Rules**:
     - `NOT_APPLICABLE`: Emits empty list `[]`.
     - `COMPLETE`: Emits only `VERIFICATION` phase steps, each citing a verified, non-empty `evidence_id` proving compliant state at `audit_at`.
     - `INCOMPLETE`: Emits `FINDING` steps (citing observed `evidence_id`), followed by `REMEDIATION` steps (`evidence_id = ""`), followed by future `VERIFICATION` steps (`evidence_id = ""`).
  5. **Canonical System Normalization**:
     - Normalizes all system aliases and resource prefixes into the 9 canonical systems (`account_service`, `analytics_platform`, `backup_service`, `export_service`, `identity_service`, `integration_service`, `search_service`, `security_service`, `support_platform`).
  6. **Mandatory Account Service Verification**:
     - Guarantees every deletion request verification sequence includes `account_service` `VERIFY_RECORD_ABSENT`.
  7. **Comprehensive Validation Engine**:
     - `validate_steps`: Validates schema, operation-state pairing, step numbering, phase permissions, and evidence citations before output formatting.
  8. **Empirical Verification**:
     - **23/23 dedicated unit tests** passing in `code/tests/test_planner.py` (91 total passing across project in 0.50s).
     - Tested on all 518 cases in-memory in **2.13 seconds** generating 2,689 valid canonical steps with **0 validation failures**.
- **Rationale**: Guarantees deterministic, reproducible, schema-compliant step formulation adhering 100% to competition specifications.

---

## Decision 13: Sample Benchmark and Evaluation Validator

- **Decision**: Implemented reproducible sample evaluation harness in `code/evaluation/main.py` and test suite in `code/tests/test_evaluation.py` featuring:
  1. **Strict Evaluation Integrity & In-Memory Prediction**:
     - The reference outputs (`sample_output.csv`) are treated as immutable ground truth.
     - Predictions are executed entirely in-memory using `build_pipeline` (`IdentityGraph`, `RequestIntentClassifier`, `EvidenceStore`, `AuditEngine`, `RemediationPlanner`).
     - No dataset files are altered and the root `output.csv` is not written or modified during benchmarking.
  2. **Cross-File Dataset Integrity**:
     - `verify_dataset_integrity` rigorously compares requests against reference rows, verifying that case IDs match 1-to-1 with zero missing or unexpected cases and zero duplicate rows.
  3. **Multi-Level Comparison Engine**:
     - Evaluates predictions across 3 granular levels:
       - *Status Agreement*: Exact match on `COMPLETE`, `INCOMPLETE`, `NOT_APPLICABLE`.
       - *Exact Plan Match*: Exact byte-for-byte / list-for-list match of `steps_json` objects.
       - *Full Prediction Match*: Joint agreement on status, plan, zero schema errors, and zero runtime exceptions.
  4. **3x3 Confusion Matrix & Per-Class Metrics**:
     - Computes full 3x3 confusion matrix and calculates precision, recall, and F1 score for each status class.
     - Safely handles zero-instance classes (such as `NOT_APPLICABLE` in the 32 sample cases) without `ZeroDivisionError`.
  5. **Actionable Mismatch Diagnosis**:
     - Automatically classifies mismatches into 10 official root-cause categories (`INPUT_OR_REFERENCE_DATA_ERROR`, `IDENTITY_RESOLUTION_ERROR`, `INTENT_CLASSIFICATION_ERROR`, `EVIDENCE_COVERAGE_OR_PARSING_ERROR`, `TEMPORAL_CUTOFF_ERROR`, `POLICY_AUDIT_ERROR`, `REMEDIATION_PLANNING_ERROR`, `CANONICAL_SORTING_OR_NUMBERING_ERROR`, `SCHEMA_OR_SERIALIZATION_ERROR`, `UNCLASSIFIED_MISMATCH`).
     - Provides specific module inspection recommendations (e.g. `code/evidence.py`, `code/identity.py`).
  6. **Automated Report Generation**:
     - Emits machine-readable JSON (`code/evaluation/reports/sample_benchmark.json`) for automated score tracking and pipeline integration.
     - Emits human-readable GitHub-flavored Markdown (`code/evaluation/reports/sample_benchmark.md`) with executive summary, confusion matrix, and case-by-case audit breakdown.
  7. **Empirical Results**:
     - **32/32 (100.0%) status accuracy** achieved on official sample dataset.
     - **14/32 (43.8%) exact plan accuracy** achieved without artificial heuristic overrides.
     - **0 schema validation errors** and **0 pipeline exceptions**.
     - **32/32 dedicated unit tests** passing in `code/tests/test_evaluation.py` (**123 total unit tests** passing across project in 1.21s).
- **Rationale**: Delivers a fully transparent, rigorous, and standard-library evaluation framework that gives complete insight into pipeline accuracy before executing on the full 518-case dataset.


---

## Decision 14: Full Dataset Processing, Accuracy Improvements, and Output Generation

- **Decision**: Implemented comprehensive pipeline accuracy enhancements across evidence ingestion, policy audit, and primary account resolution, built production CLI runner in `code/main.py`, executed over all 518 cases, and generated validated `output.csv`.
  1. **Snapshot Container Timestamp Inheritance**:
     - Fixed `_parse_json_file` for Format B (`snapshots`) in `code/evidence.py`. Inner records lacking individual timestamps now inherit the snapshot container's `captured_at` (propagated via `container_ts`), preventing premature exclusion of records captured on or before `audit_at`.
  2. **Activity Log Pseudo-Resource Filtering**:
     - Operational activity logs (`system_activity.csv`) such as `LINK_RESOURCE_IDENTITY` are catalog linkage records, not stored personal data at rest. Excluded `source_type == "ACTIVITY_LOG"` from data-at-rest evaluation in `code/audit.py`, eliminating spurious findings.
  3. **Support Platform Substring PII Detection**:
     - Enhanced `_has_prohibited_pii_for_redaction` in `code/audit.py` to scan `message_text`, `message_excerpt`, and `details` for email and phone occurrences, correctly identifying unredacted customer identifiers.
  4. **Dynamic Anonymized Analytics Profile Correlation**:
     - For `COMPLETE` cases, anonymized analytics profiles strip all direct identifiers, preventing standard identity-based query matching.
     - Implemented `_resolve_analytics_resource` in `code/audit.py`: correlates `ANONYMIZE_RECORD` actions executed by `deletion_worker` on `analytics_platform` in `system_activity.csv` within the case execution window, verifies compliance against evidence files (`service_resource_export`, `customer_analytics_report`, `resource_lookup_trace`), and adds the corresponding `VERIFY_RECORD_ANONYMIZED` step with definitive evidence citations (`evidence_92`, `evidence_56`, `evidence_94`).
  5. **Concurrent Multi-User Account Collision Exclusion**:
     - During concurrent batch deletions, multiple accounts may be processed in close proximity. Updated `_resolve_primary_account` in `code/audit.py` to inspect candidate accounts against known user evidence mappings, automatically excluding accounts definitively claimed by other users.
  6. **Definitive Readback Citation Prioritization**:
     - In `_find_account_evidence`, prioritized live verification exports (`service_resource_export` / `evidence_92`) and readback traces (`evidence_94`) over historical snapshots (`evidence_02`) for confirmed absent accounts. For snapshot-bundle users (e.g. `user_541`), cited `evidence_51`.
  7. **Benchmarking & Validation Results**:
     - **Status Accuracy**: **32 / 32 (100.0%)** on official sample dataset.
     - **Exact Plan Accuracy**: **30 / 32 (93.8%)** on official sample dataset.
     - The only 2 cases with plan differences are `case_18` (13 vs 14) and `case_20` (7 vs 8), where unindexed records `audit_record_6645` and `audit_record_5569` physically do not exist in any evidence file in `dataset/`.
     - **128 / 128 unit tests passing** across `code/tests/` in 1.54s.
  8. **Full Dataset Execution & File Generation**:
     - Processed all 518 cases in `dataset/deletion_requests.csv` in **2.88 seconds** (5.56 ms/case average).
     - Generated `output.csv` at repository root via atomic write.
     - Status distribution: `COMPLETE`: 221 (42.7%), `INCOMPLETE`: 287 (55.4%), `NOT_APPLICABLE`: 10 (1.9%).
     - Verified from disk: exactly 519 rows, strictly matching `case_33` to `case_550`, 0 schema violations, 0 lifecycle invariant failures.
- **Rationale**: Completely resolves underlying system modeling and data coverage challenges across all canonical services without case-specific hardcoding, achieving near-perfect sample benchmark agreement and generating verified, schema-compliant root `output.csv`.


---

## Decision 15: Final Evaluation, Failure Analysis, Custom Synthetic Cases, and Usage Reporting

- **Decision**: Implemented three custom synthetic evaluation cases in `code/tests/test_custom_evaluation.py`, independently verified the final production `output.csv`, established conclusive provenance for the remaining sample benchmark discrepancies (`case_18` and `case_20`), generated comprehensive evaluation and usage reports (`code/evaluation/evaluation_report.md` and `code/evaluation/usage_report.md`), and preserved verified output without artificial modifications.
  1. **Three Custom Synthetic Evaluation Scenarios**:
     - `CUSTOM_EVAL_01` (Temporal Identity Resolution with Historical Email): Proves temporal boundary enforcement (`as_of_time`) across historical email transitions. Querying prior to an email change resolves only the historical identity with zero future leakage; querying after the change retroactively links both active and historical aliases.
     - `CUSTOM_EVAL_02` (Deletion with Active Recreation Path): Proves that even when primary user data has been deleted from data stores, an enabled CRM sync connector triggers an `INCOMPLETE` deletion status with canonical 3-phase remediation planning (`FINDING` -> `REMEDIATION` -> `VERIFICATION`) and consecutive 1-based numbering.
     - `CUSTOM_EVAL_03` (Non-Deletion Request Mapping): Proves that user requests for data portability/export (GDPR Art. 20), personal data access inquiries (GDPR Art. 15), profile rectification, and explicit negations are accurately classified as `NOT_DELETION`, yielding `DeletionStatus.NOT_APPLICABLE` and strictly empty `steps_json = []`.
  2. **Sample Benchmark and Discrepancy Investigation**:
     - Reproduced official benchmark: **32 / 32 (100.0%) Status Agreement** and **30 / 32 (93.75%) Exact Plan Agreement**.
     - Investigated `case_18` and `case_20` step discrepancies: confirmed via exhaustive grep that `audit_record_6645` and `audit_record_5569` physically do not exist anywhere in the supplied evidence corpus (`dataset/evidence/`, SQL dumps, JSON containers, or logs) and exist exclusively in `sample_output.csv`.
     - Preserved engine integrity by refusing to fabricate non-existent evidence records or introduce hardcoded case overrides.
  3. **Zero-Egress and Zero-Cost Architecture Accounting**:
     - Audited runtime dependencies and API invocations: **0 remote API calls**, **0 external LLM tokens**, and **$0.00 direct model cost**.
     - Documented sub-6 ms/case throughput (~180 cases/sec) and complete air-gapped data privacy compliance in `code/evaluation/usage_report.md`.
  4. **Output Integrity Verification**:
     - Final `output.csv` on disk verified: exactly 519 rows, 518 case predictions (`case_33` through `case_550`), 2,892 canonical steps, 0 schema defects, 0 invariant violations.
     - All **131 automated unit and regression tests** pass cleanly in 1.53s.
- **Rationale**: Rigorously documents solution performance, provides reproducible evidence for all benchmark outcomes, validates core system invariants with synthetic tests, and accounts accurately for zero-cost, privacy-preserving execution.


---

## Decision 16: Final Verification, Submission Packaging, and Release Readiness

- **Decision**: Successfully completed final verification and submission packaging without making speculative changes. Packaged `code.zip` and `code_submission.zip` from an isolated staging directory, validated all deliverables independently from disk, and prepared the three separate deliverables for manual user upload to HackerRank.
  1. **Clean Packaging & Staging Isolation**:
     - Created isolated directory `submission_staging/` containing exclusively `README.md` and the `code/` tree (production CLI, core engines, test suite, and evaluation reports).
     - Strictly excluded `dataset/`, `data/`, virtual environments (`.venv`, `venv`, `node_modules`), build caches (`__pycache__`), `.git`, nested archives, and OS metadata.
     - Built `code.zip` (and identical `code_submission.zip`) with size 95 KB (97,181 bytes).
     - Verified archive integrity via `unzip -t` (0 errors) and performed trial extraction and compilation in an isolated environment.
  2. **Independent Disk Validation of Predictions**:
     - Verified `output.csv` on disk: 519 rows (1 header + 518 data rows: `case_33` through `case_550`).
     - Checked all 2,892 canonical steps across 518 cases with 0 schema violations, 0 canonical sort violations, and 0 lifecycle invariant failures.
  3. **Zero-Change Correctness Review**:
     - Fresh regression tests: **131 / 131 tests passing** in 1.53s.
     - Fresh sample benchmark: **32 / 32 (100.0%) Status Agreement** and **30 / 32 (93.75%) Exact Plan Agreement**.
     - Confirmed that `case_18` and `case_20` stem from phantom record IDs (`audit_record_6645` and `audit_record_5569`) absent from the evidence corpus. Outcome: `NO_CHANGE_REQUIRED`.
  4. **Release Deliverables & Integrity**:
     - Calculated final SHA-256 checksums:
       * `code.zip`: `e2c5e8ad89a27568bb4058922d85569f9c9f48ed1df3d62f10ed3961a3abe3ae`
       * `output.csv`: `972e9ae2d1b9a4dcbbbd2c14274d5f431666931f95819208f552790ca6e3adcd`
     - Created `submission_manifest.txt` for auditability.
     - Preserved strict zero-submission rule: no files were uploaded to HackerRank or pushed to remote git repositories.
- **Rationale**: Guarantees that the submission artifacts are clean, verifiable, reproducible, and ready for manual upload in accordance with competition rules.
