"""Normalized session data models and UTC timestamp utilities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
import re
from typing import Any

_MS_THRESHOLD = 1_000_000_000_000
_FRACTIONAL_SECONDS_RE = re.compile(r"(\d{2}:\d{2}:\d{2})\.(\d+)")


def _normalize_iso_fractional_seconds(iso: str) -> str:
    """Normalize ISO-8601 fractional seconds to 6 digits for Python 3.10 compatibility."""
    return _FRACTIONAL_SECONDS_RE.sub(
        lambda m: f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}",
        iso,
        count=1,
    )


def _parse_datetime(value: Any) -> datetime | None:
    """Parse Unix seconds/ms, datetime, or ISO-8601 string into UTC datetime."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        seconds = float(value) if abs(value) < _MS_THRESHOLD else float(value) / 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        iso = f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
        iso = _normalize_iso_fractional_seconds(iso)
        try:
            dt = datetime.fromisoformat(iso)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            pass
        try:
            num = float(raw)
        except ValueError:
            return None
        return _parse_datetime(num)

    return None


def normalize_timestamp(value: Any) -> str:
    """Normalize numeric or ISO-8601 timestamp to 'YYYY-MM-DDTHH:MM:SSZ' UTC, or ''."""
    dt = _parse_datetime(value)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp_to_epoch_ms(value: Any) -> int:
    """Convert timestamp value to UTC epoch milliseconds, or 0 if invalid."""
    dt = _parse_datetime(value)
    if dt is None:
        return 0
    return int(dt.timestamp() * 1000)


def _require_mapping(cls_name: str, data: Any) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise TypeError(f"{cls_name}.from_dict expected a mapping, got {type(data).__name__}")
    return data


def _as_str(data: Mapping[str, Any], key: str, default: str = "") -> str:
    val = data.get(key)
    if val is None:
        return default
    return val if isinstance(val, str) else str(val)


def _as_int(data: Mapping[str, Any], key: str, default: int = 0) -> int:
    val = data.get(key)
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _as_bool(data: Mapping[str, Any], key: str, default: bool = False) -> bool:
    val = data.get(key)
    if val is None:
        return default
    return bool(val)


def _as_str_list(data: Mapping[str, Any], key: str) -> list[str]:
    val = data.get(key)
    if not isinstance(val, list):
        return []
    return [item if isinstance(item, str) else str(item) for item in val if item is not None]


@dataclass
class SessionSummary:
    """Lightweight metadata summary for a discovered agent session."""

    provider: str
    session_id: str
    source_kind: str
    source_path: str
    cwd: str
    repo_name: str
    git_branch: str
    title: str
    first_user_prompt: str
    last_user_prompt: str
    created_at: str
    updated_at: str
    turn_count: int
    archived: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize summary to a plain dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionSummary:
        """Deserialize a session summary from a mapping."""
        raw = _require_mapping(cls.__name__, data)
        return cls(
            provider=_as_str(raw, "provider"),
            session_id=_as_str(raw, "session_id"),
            source_kind=_as_str(raw, "source_kind"),
            source_path=_as_str(raw, "source_path"),
            cwd=_as_str(raw, "cwd"),
            repo_name=_as_str(raw, "repo_name"),
            git_branch=_as_str(raw, "git_branch"),
            title=_as_str(raw, "title"),
            first_user_prompt=_as_str(raw, "first_user_prompt"),
            last_user_prompt=_as_str(raw, "last_user_prompt"),
            created_at=_as_str(raw, "created_at"),
            updated_at=_as_str(raw, "updated_at"),
            turn_count=_as_int(raw, "turn_count"),
            archived=_as_bool(raw, "archived", False),
            warnings=_as_str_list(raw, "warnings"),
        )


@dataclass
class ToolEvent:
    """Normalized representation of a single tool invocation within a turn."""

    call_id: str
    tool_name: str
    arguments_summary: str
    output_summary: str
    status: str = "unknown"
    files_touched: list[str] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize tool event to a plain dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolEvent:
        """Deserialize a tool event from a mapping."""
        raw = _require_mapping(cls.__name__, data)
        return cls(
            call_id=_as_str(raw, "call_id"),
            tool_name=_as_str(raw, "tool_name"),
            arguments_summary=_as_str(raw, "arguments_summary"),
            output_summary=_as_str(raw, "output_summary"),
            status=_as_str(raw, "status", "unknown") or "unknown",
            files_touched=_as_str_list(raw, "files_touched"),
            truncated=_as_bool(raw, "truncated", False),
        )


@dataclass
class NormalizedTurn:
    """Normalized user/assistant interaction turn with optional tool activity."""

    turn_index: int
    timestamp: str
    user_text: str
    assistant_text: str
    tool_events: list[ToolEvent] = field(default_factory=list)
    compaction_before: bool = False
    subagent_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize turn and nested tool events to a plain dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> NormalizedTurn:
        """Deserialize a normalized turn and its tool events from a mapping."""
        raw = _require_mapping(cls.__name__, data)
        raw_events = raw.get("tool_events")
        tool_events = [
            ev if isinstance(ev, ToolEvent) else ToolEvent.from_dict(ev)
            for ev in (raw_events if isinstance(raw_events, list) else [])
        ]
        return cls(
            turn_index=_as_int(raw, "turn_index"),
            timestamp=_as_str(raw, "timestamp"),
            user_text=_as_str(raw, "user_text"),
            assistant_text=_as_str(raw, "assistant_text"),
            tool_events=tool_events,
            compaction_before=_as_bool(raw, "compaction_before", False),
            subagent_notes=_as_str_list(raw, "subagent_notes"),
            warnings=_as_str_list(raw, "warnings"),
        )


@dataclass
class SessionDetail:
    """Full normalized session transcript and aggregated metadata."""

    summary: SessionSummary
    turns: list[NormalizedTurn]
    files_mentioned: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    compaction_count: int = 0
    total_chars: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize session detail to a plain dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionDetail:
        """Deserialize full session detail from a mapping."""
        raw = _require_mapping(cls.__name__, data)
        raw_summary = raw.get("summary")
        if isinstance(raw_summary, SessionSummary):
            summary = raw_summary
        elif isinstance(raw_summary, Mapping):
            summary = SessionSummary.from_dict(raw_summary)
        else:
            raise ValueError("SessionDetail.from_dict requires a 'summary' mapping")

        raw_turns = raw.get("turns")
        turns = [
            turn if isinstance(turn, NormalizedTurn) else NormalizedTurn.from_dict(turn)
            for turn in (raw_turns if isinstance(raw_turns, list) else [])
        ]
        return cls(
            summary=summary,
            turns=turns,
            files_mentioned=_as_str_list(raw, "files_mentioned"),
            commands_run=_as_str_list(raw, "commands_run"),
            compaction_count=_as_int(raw, "compaction_count"),
            total_chars=_as_int(raw, "total_chars"),
            warnings=_as_str_list(raw, "warnings"),
        )


@dataclass
class ImportedSessionRecord:
    """Persistent index entry tracking an imported session snapshot and Antigravity conversation."""

    import_id: str
    provider: str
    provider_display_name: str
    session_id: str
    source_kind: str
    source_path: str
    cwd: str
    repo_name: str
    git_branch: str
    title: str
    turn_count: int
    mode: str
    imported_at: str
    status: str = "ready"
    agy_conversation_id: str = ""
    agy_project_id: str = ""
    agy_project_name: str = ""
    snapshot_path: str = ""
    error_message: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize import record to a plain dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ImportedSessionRecord:
        """Deserialize an import record from a mapping."""
        raw = _require_mapping(cls.__name__, data)
        return cls(
            import_id=_as_str(raw, "import_id"),
            provider=_as_str(raw, "provider"),
            provider_display_name=_as_str(raw, "provider_display_name"),
            session_id=_as_str(raw, "session_id"),
            source_kind=_as_str(raw, "source_kind"),
            source_path=_as_str(raw, "source_path"),
            cwd=_as_str(raw, "cwd"),
            repo_name=_as_str(raw, "repo_name"),
            git_branch=_as_str(raw, "git_branch"),
            title=_as_str(raw, "title"),
            turn_count=_as_int(raw, "turn_count"),
            mode=_as_str(raw, "mode", "full") or "full",
            imported_at=_as_str(raw, "imported_at"),
            status=_as_str(raw, "status", "ready") or "ready",
            agy_conversation_id=_as_str(raw, "agy_conversation_id"),
            agy_project_id=_as_str(raw, "agy_project_id"),
            agy_project_name=_as_str(raw, "agy_project_name"),
            snapshot_path=_as_str(raw, "snapshot_path"),
            error_message=_as_str(raw, "error_message"),
            warnings=_as_str_list(raw, "warnings"),
        )
