"""Normalized session data models and UTC timestamp utilities."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
from typing import Any

_MS_THRESHOLD = 1_000_000_000_000


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
