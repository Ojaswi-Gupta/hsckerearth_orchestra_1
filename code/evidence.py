"""Evidence and Activity Ingestion Engine for Ghost Data Still Exists.

Parses evidence metadata, structured records, activity logs, snapshots,
JSON bundles, SQLite-compatible SQL database snapshots, text handovers,
and local documents/images under dataset/.

Preserves source provenance, supports identity-aware retrieval,
enforces audit_at temporal boundaries, and provides trustworthy evidence
for downstream deletion auditing.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import re
import sqlite3
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# Import identity models for type annotations
try:
    from identity import UserIdentityProfile, normalize_email, normalize_text
except ImportError:
    # Fallback for standalone usage
    def normalize_email(e: Optional[str]) -> str:
        return e.strip().lower() if e else ""

    def normalize_text(t: Optional[str]) -> str:
        return t.strip() if t else ""

    UserIdentityProfile = Any


@dataclass(frozen=True)
class ManifestEntry:
    """Entry from evidence_files.csv."""
    file_id: str
    file_path: str
    origin: str
    captured_at: str
    full_path: Path


@dataclass
class EvidenceRecord:
    """Unified internal record representing a single piece of evidence."""
    source_evidence_id: str  # e.g., 'evidence_54', 'activity_4000', etc.
    source_type: str         # 'SQL_SNAPSHOT', 'JSON_RECORD', 'LOG_EVENT', 'ACTIVITY_LOG', 'TEXT_RECORD', 'DOCUMENT_PDF', 'IMAGE_MEDIA'
    system_id: str           # e.g., 'account_service', 'search_service', 'support_platform'
    resource_id: str         # e.g., 'account_005', 'search_001', 'support_002', 'data_flow_9522'
    source_file_id: Optional[str] = None
    source_path: Optional[str] = None
    record_timestamp: Optional[str] = None  # Specific event/observed/updated timestamp
    timestamp_source_field: Optional[str] = None
    record_type: Optional[str] = None       # e.g., 'USER_PROFILE', 'SEARCH_INDEX_ENTRY', 'ANALYTICS_PROFILE'
    raw_state: Optional[str] = None         # e.g., 'FOUND', 'NOT_FOUND', 'IDENTIFIABLE', 'ACTIVE', 'DISABLED'
    user_id: Optional[str] = None
    identifier_values: Set[str] = field(default_factory=set)  # All emails, phones, hashes, names
    record_content: Dict[str, Any] = field(default_factory=dict)
    original_metadata: Dict[str, Any] = field(default_factory=dict)
    ingestion_status: str = "PARSED"        # 'PARSED', 'PARTIAL', 'UNSUPPORTED_FORMAT'
    parse_warnings: List[str] = field(default_factory=list)

    def matches_identifier(self, identifier: Optional[str]) -> bool:
        """Check if this record contains a specific identifier (case-insensitive for email)."""
        if not identifier:
            return False
        clean = normalize_text(identifier)
        clean_lower = clean.lower()
        if self.user_id and self.user_id == clean:
            return True
        for val in self.identifier_values:
            if val == clean or val.lower() == clean_lower:
                return True
        return False


@dataclass
class IngestionReport:
    """Summary diagnostics of evidence ingestion."""
    manifest_entries_count: int = 0
    files_discovered_count: int = 0
    files_parsed_count: int = 0
    files_missing_count: int = 0
    files_unsupported_count: int = 0
    activity_records_count: int = 0
    sql_records_count: int = 0
    json_records_count: int = 0
    log_records_count: int = 0
    text_records_count: int = 0
    pdf_records_count: int = 0
    image_records_count: int = 0
    total_records_count: int = 0
    records_with_timestamp_count: int = 0
    records_with_unknown_timestamp_count: int = 0
    warnings: List[str] = field(default_factory=list)


def parse_iso_dt(ts: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 UTC timestamp string."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class EvidenceStore:
    """Central store and index for all evidence files and system activity.
    
    Provides:
    1. Parsing across SQL, JSON, LOG, TXT, PDF, and activity records.
    2. Identity-aware queries using resolved UserIdentityProfile.
    3. Strict temporal filtering using audit_at (boundary-inclusive, zero future leakage).
    4. Exact source evidence citations (file_id, activity_id).
    """

    def __init__(self, dataset_dir: Optional[Union[str, Path]] = None) -> None:
        self.dataset_dir = Path(dataset_dir) if dataset_dir else None
        self.manifest: Dict[str, ManifestEntry] = {}
        self.records: List[EvidenceRecord] = []
        self.report = IngestionReport()

        # Indexes for O(1) retrieval
        self._by_source_id: Dict[str, List[EvidenceRecord]] = {}
        self._by_file_id: Dict[str, List[EvidenceRecord]] = {}
        self._by_resource_id: Dict[str, List[EvidenceRecord]] = {}
        self._by_user_id: Dict[str, List[EvidenceRecord]] = {}
        self._by_email: Dict[str, List[EvidenceRecord]] = {}
        self._by_phone: Dict[str, List[EvidenceRecord]] = {}
        self._by_hash: Dict[str, List[EvidenceRecord]] = {}

        if self.dataset_dir:
            self.load_all(self.dataset_dir)

    def load_all(self, dataset_dir: Union[str, Path]) -> IngestionReport:
        """Load manifest, activity log, and all physical evidence files."""
        base_dir = Path(dataset_dir)
        if not base_dir.exists():
            raise FileNotFoundError(f"Dataset directory not found: {base_dir}")

        self.dataset_dir = base_dir
        self.records.clear()
        self.manifest.clear()
        self._clear_indexes()
        self.report = IngestionReport()

        # 1. Load manifest (evidence_files.csv)
        manifest_path = base_dir / "evidence_files.csv"
        if manifest_path.exists():
            self._load_manifest(manifest_path, base_dir)
        else:
            self.report.warnings.append("evidence_files.csv not found in dataset directory.")

        # 2. Load system activity (system_activity.csv)
        activity_path = base_dir / "system_activity.csv"
        if activity_path.exists():
            self._load_activity(activity_path)
        else:
            self.report.warnings.append("system_activity.csv not found in dataset directory.")

        # 3. Ingest each physical evidence file from manifest
        for file_id, entry in self.manifest.items():
            if not entry.full_path.exists():
                self.report.files_missing_count += 1
                self.report.warnings.append(f"Missing file: {entry.file_path} ({file_id})")
                continue

            self.report.files_discovered_count += 1
            ext = entry.full_path.suffix.lower()

            try:
                if ext == ".sql":
                    recs = self._parse_sql_file(entry)
                    self.report.sql_records_count += len(recs)
                    self.records.extend(recs)
                    self.report.files_parsed_count += 1

                elif ext == ".json":
                    recs = self._parse_json_file(entry)
                    self.report.json_records_count += len(recs)
                    self.records.extend(recs)
                    self.report.files_parsed_count += 1

                elif ext == ".log":
                    recs = self._parse_log_file(entry)
                    self.report.log_records_count += len(recs)
                    self.records.extend(recs)
                    self.report.files_parsed_count += 1

                elif ext == ".txt":
                    recs = self._parse_text_file(entry)
                    self.report.text_records_count += len(recs)
                    self.records.extend(recs)
                    self.report.files_parsed_count += 1

                elif ext == ".pdf":
                    recs = self._parse_pdf_file(entry)
                    self.report.pdf_records_count += len(recs)
                    self.records.extend(recs)
                    self.report.files_parsed_count += 1

                elif ext == ".png":
                    recs = self._parse_image_file(entry)
                    self.report.image_records_count += len(recs)
                    self.records.extend(recs)
                    self.report.files_parsed_count += 1

                else:
                    self.report.files_unsupported_count += 1
                    self.report.warnings.append(f"Unsupported format: {entry.file_path}")

            except Exception as e:
                self.report.warnings.append(f"Failed parsing {entry.file_path}: {e}")

        # 4. Build indexes over all accumulated records
        self._build_indexes()

        self.report.total_records_count = len(self.records)
        self.report.records_with_timestamp_count = sum(
            1 for r in self.records if r.record_timestamp is not None
        )
        self.report.records_with_unknown_timestamp_count = (
            self.report.total_records_count - self.report.records_with_timestamp_count
        )

        return self.report

    def _clear_indexes(self) -> None:
        self._by_source_id.clear()
        self._by_file_id.clear()
        self._by_resource_id.clear()
        self._by_user_id.clear()
        self._by_email.clear()
        self._by_phone.clear()
        self._by_hash.clear()

    def _load_manifest(self, manifest_path: Path, base_dir: Path) -> None:
        with open(manifest_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                fid = normalize_text(row.get("file_id"))
                fpath = normalize_text(row.get("file_path"))
                origin = normalize_text(row.get("origin"))
                captured_at = normalize_text(row.get("captured_at"))

                if fid and fpath:
                    full_p = base_dir / fpath
                    entry = ManifestEntry(
                        file_id=fid,
                        file_path=fpath,
                        origin=origin,
                        captured_at=captured_at,
                        full_path=full_p,
                    )
                    self.manifest[fid] = entry
        self.report.manifest_entries_count = len(self.manifest)

    def _load_activity(self, activity_path: Path) -> None:
        with open(activity_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                aid = normalize_text(row.get("activity_id"))
                etime = normalize_text(row.get("event_time"))
                etype = normalize_text(row.get("event_type"))
                src = normalize_text(row.get("source_system"))
                tgt = normalize_text(row.get("target_system"))
                rid = normalize_text(row.get("resource_id"))
                res = normalize_text(row.get("reported_result"))
                details = normalize_text(row.get("details"))

                # Extract user_id and email from details or resource
                identifiers: Set[str] = set()
                uid = None
                uid_m = re.search(r"\buser_\d+\b", details)
                if uid_m:
                    uid = uid_m.group(0)
                    identifiers.add(uid)

                email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", details)
                if email_m:
                    em = email_m.group(0)
                    identifiers.add(em)
                    identifiers.add(em.lower())

                rec = EvidenceRecord(
                    source_evidence_id=aid,
                    source_type="ACTIVITY_LOG",
                    system_id=tgt if tgt else src,
                    resource_id=rid,
                    record_timestamp=etime,
                    timestamp_source_field="event_time",
                    record_type=etype,
                    raw_state=res,
                    user_id=uid,
                    identifier_values=identifiers,
                    record_content={
                        "activity_id": aid,
                        "event_type": etype,
                        "source_system": src,
                        "target_system": tgt,
                        "resource_id": rid,
                        "reported_result": res,
                        "details": details,
                    },
                    original_metadata={"source": "system_activity.csv"},
                )
                self.records.append(rec)
        self.report.activity_records_count = len(
            [r for r in self.records if r.source_type == "ACTIVITY_LOG"]
        )

    def _parse_sql_file(self, entry: ManifestEntry) -> List[EvidenceRecord]:
        """Safely parse SQLite-compatible SQL snapshots into EvidenceRecords."""
        records: List[EvidenceRecord] = []
        conn = sqlite3.connect(":memory:")
        try:
            with open(entry.full_path, "r", encoding="utf-8") as f:
                conn.executescript(f.read())

            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [t[0] for t in cursor.fetchall()]

            for tname in tables:
                cursor.execute(f"PRAGMA table_info('{tname}');")
                columns = [c[1] for c in cursor.fetchall()]

                cursor.execute(f"SELECT * FROM '{tname}';")
                rows = cursor.fetchall()

                for row in rows:
                    row_dict = dict(zip(columns, row))
                    rid = normalize_text(str(row_dict.get("resource_id", "")))
                    uid = normalize_text(str(row_dict.get("user_id", ""))) or None

                    # If record_json is present, parse it
                    json_data = {}
                    if "record_json" in row_dict and row_dict["record_json"]:
                        try:
                            json_data = json.loads(row_dict["record_json"])
                            if not rid and "resource_id" in json_data:
                                rid = normalize_text(str(json_data["resource_id"]))
                            if not uid:
                                u_val = (
                                    json_data.get("user_id")
                                    or json_data.get("subject_reference")
                                    or json_data.get("customer_reference")
                                )
                                if u_val:
                                    uid = normalize_text(str(u_val))
                        except Exception:
                            pass

                    # Extract identifiers
                    identifiers: Set[str] = set()
                    if uid:
                        identifiers.add(uid)
                    for k in [
                        "email", "owner_email", "customer_email", "requester_email",
                        "phone", "owner_phone", "customer_phone", "requester_phone",
                        "email_hash", "full_name", "display_name",
                        "subject_reference", "customer_reference", "user_id"
                    ]:
                        v = row_dict.get(k) or json_data.get(k)
                        if v and v != "null":
                            s = normalize_text(str(v))
                            identifiers.add(s)
                            if "@" in s:
                                identifiers.add(s.lower())

                    # Check nested document if present
                    doc = json_data.get("document")
                    if isinstance(doc, dict):
                        for dk in ["email", "phone", "email_hash", "customer_reference", "display_name"]:
                            dv = doc.get(dk)
                            if dv and dv != "null":
                                ds = normalize_text(str(dv))
                                identifiers.add(ds)
                                if "@" in ds:
                                    identifiers.add(ds.lower())
                                if dk == "customer_reference" and not uid:
                                    uid = ds

                    # Timestamp determination
                    ts = (
                        row_dict.get("updated_at")
                        or row_dict.get("indexed_at")
                        or row_dict.get("created_at")
                        or json_data.get("updated_at")
                        or json_data.get("indexed_at")
                        or entry.captured_at
                    )

                    # System determination from table name or file origin
                    sys_id = self._infer_system_id(tname, entry.origin, rid)

                    rec = EvidenceRecord(
                        source_evidence_id=entry.file_id,
                        source_type="SQL_SNAPSHOT",
                        source_file_id=entry.file_id,
                        source_path=entry.file_path,
                        system_id=sys_id,
                        resource_id=rid or f"{tname}_{len(records)}",
                        record_timestamp=ts,
                        timestamp_source_field="sql_row",
                        user_id=uid,
                        identifier_values=identifiers,
                        record_content={"table": tname, "data": row_dict, "json_payload": json_data},
                        original_metadata={"origin": entry.origin, "captured_at": entry.captured_at},
                    )
                    records.append(rec)

        finally:
            conn.close()

        return records

    def _parse_json_file(self, entry: ManifestEntry) -> List[EvidenceRecord]:
        """Parse structured and nested JSON evidence bundles."""
        records: List[EvidenceRecord] = []
        with open(entry.full_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        raw_records: List[Tuple[Dict[str, Any], Optional[str], Optional[str], Optional[str]]] = []

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    raw_records.append((item, None, None, None))

        elif isinstance(data, dict):
            # Format A: {"records": [...], "captured_at": ...}
            if "records" in data and isinstance(data["records"], list):
                coll = data.get("collection")
                src = data.get("source")
                cat = data.get("captured_at") or data.get("exported_at")
                for r in data["records"]:
                    if isinstance(r, dict):
                        raw_records.append((r, coll, src, cat))

            # Format B: {"snapshots": [{"data_store": ..., "records": [...]}]}
            elif "snapshots" in data and isinstance(data["snapshots"], list):
                for s in data["snapshots"]:
                    if isinstance(s, dict) and "records" in s and isinstance(s["records"], list):
                        store = s.get("data_store")
                        coll = s.get("collection")
                        cat = s.get("captured_at") or data.get("exported_at")
                        for r in s["records"]:
                            if isinstance(r, dict):
                                raw_records.append((r, coll, store, cat))

            # Format C: {"service_exports": {"account_service": {"profile_lookups": [...]}}}
            elif "service_exports" in data and isinstance(data["service_exports"], dict):
                cat = data.get("exported_at")
                for sname, sdata in data["service_exports"].items():
                    if isinstance(sdata, dict):
                        for look_type, items in sdata.items():
                            if isinstance(items, list):
                                for item in items:
                                    if isinstance(item, dict):
                                        raw_records.append((item, look_type, sname, cat))

        for item, coll_name, service_name, container_ts in raw_records:
            rid = normalize_text(str(item.get("resource_id", "")))
            uid_val = (
                item.get("user_id")
                or item.get("customer_reference")
                or item.get("subject_reference")
            )
            uid = normalize_text(str(uid_val)) if uid_val and uid_val != "null" else None

            identifiers: Set[str] = set()
            if uid:
                identifiers.add(uid)
            for k in [
                "email", "owner_email", "requester_email", "customer_email",
                "phone", "owner_phone", "requester_phone",
                "email_hash", "name", "full_name", "display_name",
                "lookup_key", "query"
            ]:
                v = item.get(k)
                if v and v != "null":
                    s = normalize_text(str(v))
                    identifiers.add(s)
                    if "@" in s:
                        identifiers.add(s.lower())

            # Look for nested hits (search index results)
            hits = item.get("hits")
            if isinstance(hits, dict):
                docs = hits.get("documents", [])
                if isinstance(docs, list):
                    for doc in docs:
                        if isinstance(doc, dict):
                            for dk in ["email", "phone", "email_hash", "customer_reference", "display_name"]:
                                dv = doc.get(dk)
                                if dv and dv != "null":
                                    ds = normalize_text(str(dv))
                                    identifiers.add(ds)
                                    if "@" in ds:
                                        identifiers.add(ds.lower())
                                    if dk == "customer_reference" and not uid:
                                        uid = ds

            # Look for nested profile dict
            prof = item.get("profile")
            if isinstance(prof, dict):
                for k in ["user_id", "email", "phone", "email_hash", "full_name", "customer_reference"]:
                    v = prof.get(k)
                    if v and v != "null":
                        s = normalize_text(str(v))
                        identifiers.add(s)
                        if "@" in s:
                            identifiers.add(s.lower())
                        if k in ("user_id", "customer_reference") and not uid:
                            uid = s

            ts = (
                item.get("observed_at")
                or item.get("captured_at")
                or item.get("created_at")
                or item.get("updated_at")
                or container_ts
                or data.get("captured_at")
                or data.get("exported_at")
                or entry.captured_at
            )

            sys_id = self._infer_system_id(f"{service_name or ''} {coll_name or ''}", entry.origin, rid)

            rec = EvidenceRecord(
                source_evidence_id=entry.file_id,
                source_type="JSON_RECORD",
                source_file_id=entry.file_id,
                source_path=entry.file_path,
                system_id=sys_id,
                resource_id=rid or f"record_{len(records)}",
                record_timestamp=ts,
                timestamp_source_field="json_record",
                user_id=uid,
                identifier_values=identifiers,
                record_content=item,
                original_metadata={"collection": coll_name, "origin": entry.origin},
            )
            records.append(rec)

        return records

    def _parse_log_file(self, entry: ManifestEntry) -> List[EvidenceRecord]:
        """Parse observability and diagnostic log files with intra-trace correlation."""
        records: List[EvidenceRecord] = []
        parsed_lines: List[Tuple[int, str, Dict[str, str], str]] = []

        with open(entry.full_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean_l = line.strip()
                if not clean_l:
                    continue

                ts_match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", clean_l)
                ts = ts_match.group(1) if ts_match else entry.captured_at

                # Parse key-value pairs or JSON objects
                kvs: Dict[str, str] = {}
                brace_idx = clean_l.find("{")
                if brace_idx != -1 and clean_l.endswith("}"):
                    try:
                        json_obj = json.loads(clean_l[brace_idx:])
                        if isinstance(json_obj, dict):
                            kvs.update({str(k): str(v) for k, v in json_obj.items()})
                    except Exception:
                        pass
                for m in re.finditer(r'(\w+)=(?:"([^"]*)"|(\S+))', clean_l):
                    k = m.group(1)
                    val = m.group(2) if m.group(2) is not None else m.group(3)
                    kvs[k] = val

                parsed_lines.append((line_no, ts, kvs, clean_l))

        # First pass: build trace correlation map
        trace_to_rids: Dict[str, Set[str]] = {}
        trace_to_idents: Dict[str, Set[str]] = {}
        for _, _, kvs, _ in parsed_lines:
            tid = kvs.get("trace_id") or kvs.get("requestId")
            if tid:
                if "resource_id" in kvs and kvs["resource_id"]:
                    trace_to_rids.setdefault(tid, set()).add(normalize_text(kvs["resource_id"]))
                for k in ["lookup_key", "correlation_key", "subject_key", "owner_key", "subject_reference", "subject_ref", "match_key", "email", "customer_reference"]:
                    v = kvs.get(k)
                    if v and v != "null":
                        trace_to_idents.setdefault(tid, set()).add(normalize_text(v))

        # Second pass: construct records with inherited trace context
        for line_no, ts, kvs, clean_l in parsed_lines:
            tid = kvs.get("trace_id") or kvs.get("requestId")

            # Resolve resource_id (direct or inherited from trace)
            rid = normalize_text(kvs.get("resource_id", ""))
            if not rid and tid and tid in trace_to_rids:
                rid = next(iter(trace_to_rids[tid]))

            sys_candidate = kvs.get("service") or kvs.get("function") or kvs.get("system") or ""
            sys_id = self._infer_system_id(sys_candidate, entry.origin, rid)

            identifiers: Set[str] = set()
            uid = None

            # Collect direct identifiers
            for k in ["lookup_key", "correlation_key", "subject_key", "owner_key", "subject_reference", "subject_ref", "match_key", "email", "customer_reference"]:
                v = kvs.get(k)
                if v and v != "null":
                    s = normalize_text(v)
                    if s.startswith("user_"):
                        uid = s
                        identifiers.add(s)
                    elif "@" in s:
                        identifiers.add(s)
                        identifiers.add(s.lower())
                    else:
                        identifiers.add(s)

            # Inherit identifiers from trace if present
            if tid and tid in trace_to_idents:
                for s in trace_to_idents[tid]:
                    if s.startswith("user_") and not uid:
                        uid = s
                    identifiers.add(s)
                    if "@" in s:
                        identifiers.add(s.lower())

            rec = EvidenceRecord(
                source_evidence_id=entry.file_id,
                source_type="LOG_EVENT",
                source_file_id=entry.file_id,
                source_path=entry.file_path,
                system_id=sys_id,
                resource_id=rid or f"log_{line_no}",
                record_timestamp=ts,
                timestamp_source_field="log_line",
                user_id=uid,
                identifier_values=identifiers,
                record_content={"line_no": line_no, "kvs": kvs, "raw": clean_l},
                original_metadata={"origin": entry.origin},
            )
            records.append(rec)

        return records

    def _parse_text_file(self, entry: ManifestEntry) -> List[EvidenceRecord]:
        """Parse pipe-separated handover and shift verification files."""
        records: List[EvidenceRecord] = []
        with open(entry.full_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                clean_l = line.strip()
                if clean_l.startswith("- ") and "|" in clean_l:
                    # Format: - 2026-10-01T12:00:00Z | account_service | account_146 | USER_PROFILE | NOT_FOUND | e.lindberg18@yahoo.com
                    parts = [p.strip() for p in clean_l[2:].split("|")]
                    if len(parts) >= 6:
                        ts, sys_id, rid, rtype, state, email = parts[:6]
                        identifiers = {email, email.lower()}
                        rec = EvidenceRecord(
                            source_evidence_id=entry.file_id,
                            source_type="TEXT_RECORD",
                            source_file_id=entry.file_id,
                            source_path=entry.file_path,
                            system_id=sys_id,
                            resource_id=rid,
                            record_timestamp=ts,
                            timestamp_source_field="text_row",
                            record_type=rtype,
                            raw_state=state,
                            identifier_values=identifiers,
                            record_content={"line_no": line_no, "parts": parts},
                            original_metadata={"origin": entry.origin},
                        )
                        records.append(rec)
        return records

    def _parse_pdf_file(self, entry: ManifestEntry) -> List[EvidenceRecord]:
        """Extract text tokens and identifiers from PDF content streams using ASCII85 & FlateDecode."""
        records: List[EvidenceRecord] = []
        with open(entry.full_path, "rb") as f:
            data = f.read()

        extracted_texts: List[str] = []

        # Find all stream ... endstream blocks directly by offset
        idx = 0
        while True:
            s_idx = data.find(b"stream", idx)
            if s_idx == -1:
                break
            e_idx = data.find(b"endstream", s_idx)
            if e_idx == -1:
                break

            raw_stream = data[s_idx + 6:e_idx].strip()
            idx = e_idx + 9

            # Attempt Adobe ASCII85 then zlib FlateDecode
            decompressed = None
            try:
                dec_a85 = base64.a85decode(raw_stream, adobe=True)
                decompressed = zlib.decompress(dec_a85)
            except Exception:
                try:
                    decompressed = zlib.decompress(raw_stream)
                except Exception:
                    pass

            if decompressed:
                # Extract text chunks in parentheses: (text) Tj
                tokens = re.findall(rb"\((.*?)\)\s*Tj", decompressed)
                for t in tokens:
                    s = t.decode("latin1", errors="ignore").strip()
                    if s:
                        extracted_texts.append(s)

        full_doc_text = " ".join(extracted_texts)

        # Extract resource_ids (e.g. support_001, CS-4827)
        rid = None
        rid_m = re.search(r"\b(support_\d+|account_\d+|export_\d+|object_\d+)\b", full_doc_text)
        if rid_m:
            rid = rid_m.group(0)

        # Extract email and phone
        identifiers: Set[str] = set()
        email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", full_doc_text)
        if email_m:
            em = email_m.group(0)
            identifiers.add(em)
            identifiers.add(em.lower())

        phone_m = re.search(r"\+\d{1,3}\s+[\d\s-]{6,15}\d", full_doc_text)
        if phone_m:
            identifiers.add(phone_m.group(0).strip())

        # Extract name if known (e.g. Priya Raman)
        name_m = re.search(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b", full_doc_text)
        if name_m:
            identifiers.add(name_m.group(0).strip())

        rec = EvidenceRecord(
            source_evidence_id=entry.file_id,
            source_type="DOCUMENT_PDF",
            source_file_id=entry.file_id,
            source_path=entry.file_path,
            system_id="support_platform" if "support" in entry.origin else "document_service",
            resource_id=rid or f"doc_{entry.file_id}",
            record_timestamp=entry.captured_at,
            timestamp_source_field="manifest_capture_time",
            record_type="SUPPORT_ATTACHMENT" if "attachment" in entry.file_path else "DOCUMENT",
            identifier_values=identifiers,
            record_content={"extracted_tokens": extracted_texts[:30], "full_text_sample": full_doc_text[:200]},
            original_metadata={"origin": entry.origin, "captured_at": entry.captured_at},
        )
        records.append(rec)
        return records

    def _parse_image_file(self, entry: ManifestEntry) -> List[EvidenceRecord]:
        """Ingest image evidence metadata and associate with system origin."""
        # When local OCR is unavailable, document the provenance and status explicitly
        rec = EvidenceRecord(
            source_evidence_id=entry.file_id,
            source_type="IMAGE_MEDIA",
            source_file_id=entry.file_id,
            source_path=entry.file_path,
            system_id=self._infer_system_id("", entry.origin, ""),
            resource_id=f"media_{entry.file_id}",
            record_timestamp=entry.captured_at,
            timestamp_source_field="manifest_capture_time",
            record_type="SCREENSHOT_CAPTURE",
            raw_state="CAPTURED",
            record_content={"file_name": entry.full_path.name, "size_bytes": entry.full_path.stat().st_size},
            original_metadata={"origin": entry.origin, "captured_at": entry.captured_at},
            ingestion_status="PARSED",
            parse_warnings=["Local OCR library not installed; metadata and origin preserved."],
        )
        return [rec]

    def _infer_system_id(self, table_or_coll: str, origin: str, resource_id: str) -> str:
        """Infer canonical system_id based on table name, manifest origin, or resource prefix."""
        rid = resource_id.lower()
        if rid.startswith("account_"):
            return "account_service"
        if rid.startswith("search_"):
            return "search_service"
        if rid.startswith("support_"):
            return "support_platform"
        if rid.startswith("analytics_"):
            return "analytics_platform"
        if rid.startswith("backup_"):
            return "backup_service"
        if rid.startswith("data_flow_"):
            return "integration_service"
        if rid.startswith("export_"):
            return "export_service"
        if rid.startswith("marketing_"):
            return "marketing_platform"
        if rid.startswith("tombstone_"):
            return "identity_service"
        if rid.startswith("audit_record_"):
            return "security_service"
        if rid.startswith("email_delivery_"):
            return "email_delivery_service"
        if rid.startswith("legal_hold_"):
            return "legal_service"

        s = f"{table_or_coll} {origin} {resource_id}".lower()
        if "account" in s or "profile" in s:
            return "account_service"
        if "search" in s or "index" in s:
            return "search_service"
        if "support" in s or "ticket" in s:
            return "support_platform"
        if "analytics" in s or "telemetry" in s:
            return "analytics_platform"
        if "backup" in s or "restore" in s:
            return "backup_service"
        if "integration" in s or "connector" in s or "data_flow" in s:
            return "integration_service"
        if "export" in s:
            return "export_service"
        if "marketing" in s:
            return "marketing_platform"
        if "identity" in s or "tombstone" in s:
            return "identity_service"
        if "security" in s or "audit" in s:
            return "security_service"
        return "platform_service"

    def _build_indexes(self) -> None:
        """Build O(1) multi-indexes over accumulated records."""
        self._clear_indexes()
        for r in self.records:
            # By source evidence ID (e.g. evidence_54, activity_4000)
            self._by_source_id.setdefault(r.source_evidence_id, []).append(r)

            # By file_id
            if r.source_file_id:
                self._by_file_id.setdefault(r.source_file_id, []).append(r)

            # By resource_id
            if r.resource_id:
                self._by_resource_id.setdefault(r.resource_id, []).append(r)

            # By user_id
            if r.user_id:
                self._by_user_id.setdefault(r.user_id, []).append(r)

            # By identifiers (emails, phones, hashes)
            for ident in r.identifier_values:
                if "@" in ident:
                    self._by_email.setdefault(ident.lower(), []).append(r)
                elif re.match(r"^[0-9a-f]{64}$", ident.lower()):
                    self._by_hash.setdefault(ident.lower(), []).append(r)
                elif ident.startswith("user_") or ident.startswith("usr_"):
                    self._by_user_id.setdefault(ident, []).append(r)
                elif re.search(r"\d{3,}", ident):
                    self._by_phone.setdefault(ident, []).append(r)

    def query_by_identity(
        self,
        profile: Any,
        as_of_time: Optional[Union[str, datetime]] = None,
    ) -> List[EvidenceRecord]:
        """Retrieve all evidence matching a user's identity profile on or before as_of_time."""
        matched_records: Dict[Tuple[str, str, str], EvidenceRecord] = {}

        uids = {profile.user_id} if hasattr(profile, "user_id") and profile.user_id else set()
        emails = profile.all_emails if hasattr(profile, "all_emails") else set()
        phones = profile.all_phones if hasattr(profile, "all_phones") else set()
        hashes = profile.all_email_hashes if hasattr(profile, "all_email_hashes") else set()

        # Query by user_id
        for uid in uids:
            for r in self._by_user_id.get(uid, []):
                key = (r.source_evidence_id, r.resource_id, r.system_id)
                matched_records[key] = r

        # Query by emails
        for em in emails:
            for r in self._by_email.get(em.lower(), []):
                key = (r.source_evidence_id, r.resource_id, r.system_id)
                matched_records[key] = r

        # Query by phones
        for ph in phones:
            for r in self._by_phone.get(ph, []):
                key = (r.source_evidence_id, r.resource_id, r.system_id)
                matched_records[key] = r

        # Query by hashes
        for h in hashes:
            for r in self._by_hash.get(h.lower(), []):
                key = (r.source_evidence_id, r.resource_id, r.system_id)
                matched_records[key] = r

        candidate_list = list(matched_records.values())

        # Apply temporal filtering
        if as_of_time is not None:
            candidate_list = self.filter_by_timestamp(candidate_list, as_of_time)

        # Sort deterministically by (record_timestamp, resource_id, source_evidence_id)
        candidate_list.sort(key=lambda r: (r.record_timestamp or "", r.resource_id, r.source_evidence_id))
        return candidate_list

    def filter_by_timestamp(
        self,
        records: List[EvidenceRecord],
        as_of_time: Union[str, datetime],
    ) -> List[EvidenceRecord]:
        """Filter records strictly on record_timestamp <= as_of_time (boundary inclusive)."""
        cutoff_dt = parse_iso_dt(as_of_time if isinstance(as_of_time, str) else as_of_time.isoformat())
        if not cutoff_dt:
            return records

        filtered: List[EvidenceRecord] = []
        for r in records:
            if r.record_timestamp:
                rec_dt = parse_iso_dt(r.record_timestamp)
                if rec_dt and rec_dt <= cutoff_dt:
                    filtered.append(r)
            else:
                # If record timestamp is unknown, check source file captured_at
                file_entry = self.manifest.get(r.source_evidence_id)
                if file_entry and file_entry.captured_at:
                    f_dt = parse_iso_dt(file_entry.captured_at)
                    if f_dt and f_dt <= cutoff_dt:
                        filtered.append(r)
                else:
                    # Expose records without confirmed timestamps
                    filtered.append(r)

        return filtered

    def query_by_resource_id(
        self,
        resource_id: str,
        as_of_time: Optional[Union[str, datetime]] = None,
    ) -> List[EvidenceRecord]:
        """Retrieve all evidence matching a specific resource_id."""
        records = self._by_resource_id.get(resource_id, [])
        if as_of_time is not None:
            records = self.filter_by_timestamp(records, as_of_time)
        return records

    def get_by_source_id(self, source_id: str) -> List[EvidenceRecord]:
        """Retrieve all records under a specific source evidence ID."""
        return self._by_source_id.get(source_id, [])

    def get_ingestion_report(self) -> IngestionReport:
        """Retrieve the ingestion summary report."""
        return self.report
