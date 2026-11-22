"""Persistent atomic 0600 snapshot store and import index for session handoffs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Iterator

from importer.models import ImportedSessionRecord, SessionDetail, timestamp_to_epoch_ms
from importer.providers.base import normalize_path
from importer.safety import TRUST_BOUNDARY

_SAFE_PROVIDER_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_IMPORT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+__[0-9a-f]{16}$")


def make_import_id(provider: str, session_id: str, source_path: str = "") -> str:
    """Return deterministic import ID `'<provider>__<sha256[:16]>'`."""
    raw_provider = (provider or "").strip()
    safe_provider = _SAFE_PROVIDER_RE.sub("_", raw_provider).strip("._-") or "unknown"
    key = session_id or source_path
    digest = hashlib.sha256(f"{raw_provider}:{key}".encode("utf-8")).hexdigest()[:16]
    return f"{safe_provider}__{digest}"


def default_data_dir() -> Path:
    """Return the persistent store directory from env or Antigravity plugin_data."""
    override = os.environ.get("AGY_SESSION_IMPORTER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "antigravity" / "plugin_data" / "agy-session-importer"


def _merge_warnings(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sort_records_desc(records: list[ImportedSessionRecord]) -> list[ImportedSessionRecord]:
    return sorted(
        records,
        key=lambda rec: (timestamp_to_epoch_ms(rec.imported_at), rec.imported_at, rec.import_id),
        reverse=True,
    )


class ImportedSessionStore:
    """Thread- and process-safe persistent store for imported session snapshots and index."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir).expanduser() if data_dir is not None else default_data_dir()
        self.snapshots_dir = self.data_dir / "snapshots"
        self.index_path = self.data_dir / "imported_sessions.json"
        self._lock_path = self.data_dir / ".store.lock"
        self._thread_lock = threading.RLock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.chmod(self._lock_path, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _atomic_write_json(self, target: Path, payload: Any) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.data_dir), prefix=".tmp_", suffix=".json")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, target)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _read_records_unlocked(self) -> list[ImportedSessionRecord]:
        if not self.index_path.is_file():
            return []
        raw_text = self.index_path.read_text(encoding="utf-8")
        if not raw_text.strip():
            return []
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict):
            raw_records = parsed.get("records") or []
        elif isinstance(parsed, list):
            raw_records = parsed
        else:
            raise ValueError(f"Invalid import index structure in '{self.index_path}'")
        return [ImportedSessionRecord.from_dict(item) for item in raw_records]

    def _write_records_unlocked(self, records: list[ImportedSessionRecord]) -> None:
        ordered = _sort_records_desc(records)
        payload = {
            "schema_version": 1,
            "records": [rec.to_dict() for rec in ordered],
        }
        self._atomic_write_json(self.index_path, payload)

    def _snapshot_file_for(self, import_id: str) -> Path:
        if not _SAFE_IMPORT_ID_RE.match(import_id):
            raise ValueError(f"Invalid import_id format: {import_id!r}")
        return self.snapshots_dir / f"{import_id}.json"

    def save_import(
        self,
        detail: SessionDetail,
        *,
        provider_display_name: str,
        mode: str = "full",
        status: str = "ready",
        agy_conversation_id: str = "",
        agy_project_id: str = "",
        agy_project_name: str = "",
        error_message: str = "",
    ) -> ImportedSessionRecord:
        """Atomically persist a session snapshot (0600) and upsert its index record."""
        summary = detail.summary
        import_id = make_import_id(summary.provider, summary.session_id, summary.source_path)
        snapshot_file = self._snapshot_file_for(import_id)
        snapshot_payload = detail.to_dict()
        snapshot_payload["schema_version"] = 1
        snapshot_payload["trust_boundary"] = TRUST_BOUNDARY

        with self._locked():
            self._atomic_write_json(snapshot_file, snapshot_payload)
            records = self._read_records_unlocked()
            existing = next((rec for rec in records if rec.import_id == import_id), None)

            resolved_conv_id = agy_conversation_id
            resolved_proj_id = agy_project_id
            resolved_proj_name = agy_project_name
            if existing is not None and status != "creating":
                if not resolved_conv_id:
                    resolved_conv_id = existing.agy_conversation_id
                if not resolved_proj_id and existing.agy_project_id:
                    resolved_proj_id = existing.agy_project_id
                if not resolved_proj_name and existing.agy_project_name:
                    resolved_proj_name = existing.agy_project_name

            turn_count = len(detail.turns) if detail.turns else summary.turn_count
            warnings = _merge_warnings(list(summary.warnings), list(detail.warnings))

            record = ImportedSessionRecord(
                import_id=import_id,
                provider=summary.provider,
                provider_display_name=provider_display_name,
                session_id=summary.session_id,
                source_kind=summary.source_kind,
                source_path=summary.source_path,
                cwd=summary.cwd,
                repo_name=summary.repo_name,
                git_branch=summary.git_branch,
                title=summary.title,
                turn_count=turn_count,
                mode=mode,
                imported_at=_utc_now_iso(),
                status=status,
                agy_conversation_id=resolved_conv_id,
                agy_project_id=resolved_proj_id,
                agy_project_name=resolved_proj_name,
                snapshot_path=str(snapshot_file),
                error_message=error_message,
                warnings=warnings,
            )

            updated_records = [rec for rec in records if rec.import_id != import_id]
            updated_records.append(record)
            self._write_records_unlocked(updated_records)
            return record

    def update_status(
        self,
        import_id: str,
        *,
        status: str,
        agy_conversation_id: str | None = None,
        agy_project_id: str | None = None,
        agy_project_name: str | None = None,
        error_message: str | None = None,
        extra_warnings: list[str] | None = None,
    ) -> ImportedSessionRecord:
        """Update lifecycle status and metadata fields for an existing import record."""
        with self._locked():
            records = self._read_records_unlocked()
            target: ImportedSessionRecord | None = None
            for rec in records:
                if rec.import_id == import_id:
                    target = rec
                    break
            if target is None:
                raise KeyError(f"Unknown import_id: {import_id}")

            target.status = status
            if agy_conversation_id is not None:
                target.agy_conversation_id = agy_conversation_id
            if agy_project_id is not None:
                target.agy_project_id = agy_project_id
            if agy_project_name is not None:
                target.agy_project_name = agy_project_name
            if error_message is not None:
                target.error_message = error_message
            if extra_warnings:
                target.warnings = _merge_warnings(target.warnings, extra_warnings)

            self._write_records_unlocked(records)
            return ImportedSessionRecord.from_dict(target.to_dict())

    def list_records(self) -> list[ImportedSessionRecord]:
        """Return all persisted import records sorted by imported_at descending."""
        with self._locked():
            return _sort_records_desc(self._read_records_unlocked())

    def get_record(self, ref: str, *, provider: str | None = None) -> ImportedSessionRecord | None:
        """Look up an import record by import_id, session_id, source_path, conversation_id, or prefix."""
        needle = (ref or "").strip()
        if not needle:
            return None

        provider_filter = (provider or "").strip().lower()
        if provider_filter == "all":
            provider_filter = ""

        records = self.list_records()
        if provider_filter:
            records = [rec for rec in records if rec.provider.lower() == provider_filter]

        for rec in records:
            if rec.import_id == needle:
                return rec
        for rec in records:
            if rec.session_id and rec.session_id == needle:
                return rec

        norm_needle = normalize_path(needle) if ("/" in needle or "\\" in needle) else ""
        for rec in records:
            if rec.source_path and (
                rec.source_path == needle or (norm_needle and normalize_path(rec.source_path) == norm_needle)
            ):
                return rec

        for rec in records:
            if rec.agy_conversation_id and rec.agy_conversation_id == needle:
                return rec

        prefix_matches = [
            rec
            for rec in records
            if (rec.session_id and rec.session_id.startswith(needle))
            or rec.import_id.startswith(needle)
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        return None

    def load_snapshot(self, import_id: str) -> SessionDetail:
        """Load the full persisted SessionDetail snapshot for import_id."""
        snapshot_file = self._snapshot_file_for(import_id)
        with self._locked():
            if not snapshot_file.is_file():
                raise FileNotFoundError(f"Snapshot not found for import_id: {import_id}")
            payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
        return SessionDetail.from_dict(payload)

    def delete_record(self, import_id: str) -> bool:
        """Delete an import record and its local snapshot file (never touches source sessions)."""
        with self._locked():
            records = self._read_records_unlocked()
            remaining = [rec for rec in records if rec.import_id != import_id]
            if len(remaining) == len(records):
                return False
            self._write_records_unlocked(remaining)
            try:
                snapshot_file = self._snapshot_file_for(import_id)
                snapshot_file.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
            return True
