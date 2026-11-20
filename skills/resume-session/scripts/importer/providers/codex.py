"""Codex session provider supporting rollout JSONL/.jsonl.zst and SQLite fallback."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from typing import Any

from importer.models import (
    NormalizedTurn,
    SessionDetail,
    SessionSummary,
    ToolEvent,
    normalize_timestamp,
    timestamp_to_epoch_ms,
)
from importer.providers.base import (
    ProviderReadError,
    SessionProvider,
    derive_repo_name,
    highest_numbered_file,
    normalize_path,
    open_sqlite_readonly,
)
from importer.safety import clean_text, truncate_text

_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (?:Update File|Add File|Delete File|Move to):\s*(.+?)\s*$",
    re.MULTILINE,
)
_EXIT_CODE_RE = re.compile(
    r"(?im)(?:^|\b)(?:exit[ _]?code|exited with code|return[ _]?code)\s*[:=]?\s*(-?[1-9]\d*)\b"
)
_EXIT_ZERO_RE = re.compile(
    r"(?im)(?:^|\b)(?:exit[ _]?code|exited with code|return[ _]?code)\s*[:=]?\s*0\b"
)
_AGENTS_MD_RE = re.compile(
    r"^\s*#\s*AGENTS\.md instructions\b[\s\S]*?<INSTRUCTIONS>[\s\S]*?</INSTRUCTIONS>\s*",
    re.IGNORECASE,
)
_USER_INSTRUCTIONS_RE = re.compile(
    r"<user_instructions\b[^>]*>[\s\S]*?</user_instructions>",
    re.IGNORECASE,
)

_CODEX_RECORD_TYPES = {"session_meta", "turn_context", "response_item", "event_msg", "compacted"}
_SKIP_TOP_TYPES = {"token_usage_record", "world_state", "environment_context", "user_instructions"}
_SKIP_RESPONSE_TYPES = {"reasoning", "additional_tools", "configuration_update"}
_SKIP_EVENT_TYPES = {"token_count", "thread_settings_applied", "item_completed"}
_SHELL_TOOL_NAMES = {
    "shell",
    "exec_command",
    "shell_command",
    "exec",
    "bash",
    "terminal",
    "run_command",
    "local_shell",
}
_FILE_ARG_KEYS = ("path", "file_path", "filepath", "target_file", "file")
_SKIP_PROVENANCE_TYPES = {"agents_md", "harness"}
_SKIP_ITEM_KINDS = {"agents_md.instructions", "environments.environment_context"}

_PROMPT_PREVIEW_LIMIT = 240
_TITLE_PREVIEW_LIMIT = 120
_CMD_PREVIEW_LIMIT = 240
_ZST_TIMEOUT_SECONDS = 15


def _add_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _strip_codex_envelopes(text: str) -> str:
    cleaned = _AGENTS_MD_RE.sub("", text)
    return _USER_INSTRUCTIONS_RE.sub("", cleaned)


def _clean_text_counted(
    value: Any,
    secret_counter: list[int],
    *,
    strip_tags: bool = True,
) -> str:
    raw = "" if value is None else (value if isinstance(value, str) else str(value))
    if strip_tags:
        raw = _strip_codex_envelopes(raw)
    cleaned, count = clean_text(raw, strip_tags=strip_tags)
    secret_counter[0] += count
    return cleaned


def _preview_line(value: Any, limit: int, secret_counter: list[int]) -> str:
    cleaned = _clean_text_counted(value, secret_counter, strip_tags=True)
    text, _ = truncate_text(cleaned, limit, single_line=True)
    return text


def _extract_session_id_from_name(path: Path) -> str:
    matches = _UUID_RE.findall(path.name)
    return matches[-1].lower() if matches else ""


def _resolve_existing_path(raw: str, root: Path) -> Path | None:
    if not raw or not raw.strip():
        return None
    candidate = Path(raw).expanduser()
    try:
        if candidate.is_file():
            return candidate
        if not candidate.is_absolute():
            rel = root / candidate
            if rel.is_file():
                return rel
    except OSError:
        return None
    return None


def _is_rollout_filename(path: Path) -> bool:
    lower = path.name.lower()
    return lower.endswith(".jsonl") or lower.endswith(".jsonl.zst")


def _extract_patch_files(text: str) -> list[str]:
    files: list[str] = []
    for match in _PATCH_FILE_RE.findall(text):
        cleaned = match.strip()
        if cleaned and cleaned != "/dev/null":
            _add_unique(files, cleaned)
    return files


def _extract_files_from_mapping(mapping: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for key in _FILE_ARG_KEYS:
        val = mapping.get(key)
        if isinstance(val, str) and val.strip():
            _add_unique(files, val.strip())
    paths_val = mapping.get("paths")
    if isinstance(paths_val, list):
        for entry in paths_val:
            if isinstance(entry, str) and entry.strip():
                _add_unique(files, entry.strip())
    for patch_key in ("patch", "diff", "input"):
        patch_val = mapping.get(patch_key)
        if isinstance(patch_val, str) and "*** " in patch_val:
            for touched in _extract_patch_files(patch_val):
                _add_unique(files, touched)
    return files


def _short_tool_name(tool_name: str) -> str:
    base = tool_name.rsplit(".", 1)[-1].rsplit(":", 1)[-1]
    return base.strip().lower()


def _command_to_string(cmd: Any) -> str:
    if isinstance(cmd, str):
        return cmd.strip()
    if isinstance(cmd, list):
        parts = [str(item).strip() for item in cmd if item is not None and str(item).strip()]
        return " ".join(parts)
    return ""


def _extract_command_from_args(tool_name: str, parsed: Any, raw_text: str = "") -> str:
    if _short_tool_name(tool_name) not in _SHELL_TOOL_NAMES:
        return ""
    if isinstance(parsed, dict):
        for key in ("command", "cmd"):
            cmd = _command_to_string(parsed.get(key))
            if cmd:
                return cmd
        action = parsed.get("action")
        if isinstance(action, dict):
            cmd = _command_to_string(action.get("command") or action.get("cmd"))
            if cmd:
                return cmd
    if raw_text and not raw_text.lstrip().startswith("{"):
        return raw_text.strip()
    return ""


def _should_skip_block(metadata: dict[str, Any] | None, idx: int) -> bool:
    if not isinstance(metadata, dict):
        return False
    kinds = metadata.get("content_item_kinds")
    if isinstance(kinds, list) and idx < len(kinds):
        kind = str(kinds[idx] or "")
        if kind in _SKIP_ITEM_KINDS or kind.startswith("additional_content."):
            return True
    item_meta = metadata.get("content_item_metadata")
    if isinstance(item_meta, list) and idx < len(item_meta):
        entry = item_meta[idx]
        if isinstance(entry, dict):
            prov = entry.get("provenance")
            if isinstance(prov, dict) and str(prov.get("type") or "") in _SKIP_PROVENANCE_TYPES:
                return True
    return False


def _extract_message_text(content: Any, metadata: dict[str, Any] | None = None) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for idx, block in enumerate(content):
        if _should_skip_block(metadata, idx):
            continue
        if isinstance(block, str):
            if block:
                parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").lower()
        if "image" in btype or "image_url" in block:
            parts.append("[image omitted]")
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _try_parse_json(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    stripped = raw.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _status_from_mapping(mapping: dict[str, Any]) -> str | None:
    if mapping.get("success") is False or mapping.get("is_error") is True:
        return "error"
    if mapping.get("error"):
        return "error"
    status_val = str(mapping.get("status") or "").strip().lower()
    if status_val in {"failed", "error"}:
        return "error"
    if status_val in {"aborted", "cancelled", "interrupted"}:
        return "aborted"
    for key in ("exit_code", "exitCode"):
        if key in mapping and isinstance(mapping[key], int) and not isinstance(mapping[key], bool):
            return "error" if mapping[key] != 0 else "ok"
    meta = mapping.get("metadata")
    if isinstance(meta, dict):
        nested = _status_from_mapping(meta)
        if nested is not None:
            return nested
    if mapping.get("success") is True or status_val in {"completed", "ok", "success"}:
        return "ok"
    return None


def _text_from_output_mapping(mapping: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("body", "text", "output", "stdout", "stderr", "error", "message"):
        val = mapping.get(key)
        if isinstance(val, str) and val:
            pieces.append(val)
        elif isinstance(val, (dict, list)):
            pieces.append(json.dumps(val, ensure_ascii=False))
    if pieces:
        return "\n".join(pieces)
    return json.dumps(mapping, ensure_ascii=False)


def _normalize_tool_output(output: Any) -> tuple[str, str | None]:
    if output is None:
        return "", None
    detected_status: str | None = None
    if isinstance(output, dict):
        detected_status = _status_from_mapping(output)
        raw_text = _text_from_output_mapping(output)
    elif isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                itype = str(item.get("type") or "").lower()
                if "image" in itype or "image_url" in item:
                    parts.append("[image omitted]")
                elif isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(_text_from_output_mapping(item))
        raw_text = "\n".join(p for p in parts if p)
    elif isinstance(output, str):
        raw_text = output
        parsed = _try_parse_json(output)
        if isinstance(parsed, dict):
            detected_status = _status_from_mapping(parsed)
    else:
        raw_text = str(output)

    if detected_status is None:
        if _EXIT_CODE_RE.search(raw_text):
            detected_status = "error"
        elif _EXIT_ZERO_RE.search(raw_text):
            detected_status = "ok"
    return raw_text, detected_status


def _format_args_summary(
    value: Any,
    max_tool_chars: int,
    secret_counter: list[int],
) -> tuple[str, bool]:
    if value is None:
        return "", False
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    cleaned = _clean_text_counted(raw, secret_counter, strip_tags=False)
    return truncate_text(cleaned, max_tool_chars, single_line=True)


def _format_output_summary(
    text: str,
    max_tool_chars: int,
    secret_counter: list[int],
) -> tuple[str, bool]:
    if not text:
        return "", False
    cleaned = _clean_text_counted(text, secret_counter, strip_tags=True)
    return truncate_text(cleaned, max_tool_chars, single_line=False)


def _iter_rollout_lines(path: Path, warnings: list[str]) -> list[str] | None:
    lower = path.name.lower()
    if lower.endswith(".jsonl.zst"):
        zstd_bin = shutil.which("zstd")
        if not zstd_bin:
            _add_unique(warnings, f"codex_zstd_unavailable:{path}")
            return None
        try:
            proc = subprocess.run(
                [zstd_bin, "-d", "-c", "-q", str(path)],
                capture_output=True,
                timeout=_ZST_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _add_unique(warnings, f"codex_zstd_decompress_failed:{path}:{exc}")
            return None
        if proc.returncode != 0:
            _add_unique(warnings, f"codex_zstd_decompress_failed:{path}:exit_{proc.returncode}")
            return None
        return proc.stdout.decode("utf-8", errors="replace").splitlines()

    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        _add_unique(warnings, f"codex_rollout_read_error:{path}:{exc}")
        return None


def _parse_rollout_json_lines(
    lines: list[str],
    warnings: list[str],
) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            malformed += 1
    if malformed:
        _add_unique(warnings, f"codex_malformed_jsonl_lines:{malformed}")
    return records, malformed


def _summarize_tool_call(
    *,
    call_id: str,
    tool_name: str,
    raw_args: Any,
    max_tool_chars: int,
    secret_counter: list[int],
    is_patch: bool = False,
) -> tuple[ToolEvent, str]:
    parsed = _try_parse_json(raw_args)
    files_touched: list[str] = []
    if is_patch and isinstance(raw_args, str):
        for touched in _extract_patch_files(raw_args):
            _add_unique(files_touched, touched)
    if isinstance(parsed, dict):
        for touched in _extract_files_from_mapping(parsed):
            _add_unique(files_touched, touched)
    elif isinstance(raw_args, str) and "*** " in raw_args:
        for touched in _extract_patch_files(raw_args):
            _add_unique(files_touched, touched)

    raw_text = raw_args if isinstance(raw_args, str) else ""
    command = _extract_command_from_args(tool_name, parsed, raw_text)
    if command:
        command = _preview_line(command, _CMD_PREVIEW_LIMIT, secret_counter)

    args_summary, arg_truncated = _format_args_summary(
        parsed if parsed is not None else raw_args,
        max_tool_chars,
        secret_counter,
    )
    event = ToolEvent(
        call_id=call_id,
        tool_name=tool_name,
        arguments_summary=args_summary,
        output_summary="",
        status="unknown",
        files_touched=files_touched,
        truncated=arg_truncated,
    )
    return event, command


def _qualify_tool_name(name: str, namespace: str) -> str:
    clean_name = name.strip() or "tool"
    clean_ns = namespace.strip()
    if clean_ns and not clean_name.startswith(f"{clean_ns}."):
        return f"{clean_ns}.{clean_name}"
    return clean_name


def _merge_index_metadata(
    raw_title: str,
    updated_at: str,
    first_prompt: str,
    index_entry: dict[str, str],
    secret_counter: list[int],
) -> tuple[str, str]:
    merged_updated = updated_at
    idx_updated = index_entry.get("updated_at", "")
    if idx_updated and timestamp_to_epoch_ms(idx_updated) > timestamp_to_epoch_ms(merged_updated):
        merged_updated = idx_updated
    chosen_title = raw_title.strip() or index_entry.get("title", "")
    title = _preview_line(chosen_title, _TITLE_PREVIEW_LIMIT, secret_counter) or _preview_line(
        first_prompt, _TITLE_PREVIEW_LIMIT, secret_counter
    )
    return title, merged_updated


class _TurnAccumulator:
    """Shared turn grouping and boundary state across Codex rollout and SQLite readers."""

    def __init__(self, on_new_turn: Any = None) -> None:
        self.turns: list[NormalizedTurn] = []
        self.turn_ids: list[str] = []
        self.turn_commands: list[list[str]] = []
        self.pending_compaction = False
        self._on_new_turn = on_new_turn

    def new_turn(self, ts: str, turn_id: str) -> NormalizedTurn:
        turn = NormalizedTurn(
            turn_index=len(self.turns) + 1,
            timestamp=ts,
            user_text="",
            assistant_text="",
            compaction_before=self.pending_compaction,
        )
        self.pending_compaction = False
        if self._on_new_turn is not None:
            self._on_new_turn(turn, turn_id)
        self.turns.append(turn)
        self.turn_ids.append(turn_id)
        self.turn_commands.append([])
        return turn

    def active_turn(self, ts: str, turn_id: str) -> NormalizedTurn:
        if not self.turns:
            return self.new_turn(ts, turn_id)
        if turn_id and self.turn_ids[-1] and turn_id != self.turn_ids[-1]:
            return self.new_turn(ts, turn_id)
        if turn_id and not self.turn_ids[-1]:
            self.turn_ids[-1] = turn_id
        if not self.turns[-1].timestamp and ts:
            self.turns[-1].timestamp = ts
        return self.turns[-1]

    def add_user_message(self, cleaned: str, ts: str, turn_id: str) -> None:
        if not cleaned:
            return
        if (
            self.turns
            and not self.turns[-1].user_text
            and not self.turns[-1].assistant_text
            and not self.turns[-1].tool_events
            and (not turn_id or not self.turn_ids[-1] or self.turn_ids[-1] == turn_id)
        ):
            turn = self.turns[-1]
            turn.user_text = cleaned
            if not turn.timestamp and ts:
                turn.timestamp = ts
            if turn_id and not self.turn_ids[-1]:
                self.turn_ids[-1] = turn_id
        else:
            turn = self.new_turn(ts, turn_id)
            turn.user_text = cleaned

    def add_assistant_message(self, cleaned: str, ts: str, turn_id: str) -> None:
        if not cleaned:
            return
        turn = self.active_turn(ts, turn_id)
        turn.assistant_text = f"{turn.assistant_text}\n\n{cleaned}" if turn.assistant_text else cleaned

    def add_command(self, cmd: str) -> None:
        if cmd and self.turn_commands:
            _add_unique(self.turn_commands[-1], cmd)

    def rollback(self, count: int) -> bool:
        if count <= 0 or not self.turns:
            return False
        del self.turns[-count:]
        del self.turn_ids[-count:]
        del self.turn_commands[-count:]
        return True

    def find_turn(self, turn_id: str) -> NormalizedTurn | None:
        if turn_id:
            for turn, tid in zip(reversed(self.turns), reversed(self.turn_ids)):
                if tid == turn_id:
                    return turn
        return self.turns[-1] if self.turns else None


class CodexProvider(SessionProvider):
    """Read-only provider for OpenAI Codex rollout JSONL and SQLite stores."""

    name = "codex"
    display_name = "Codex"
    aliases = ("openai-codex", "codex-cli")

    def __init__(self, root: Path | None = None) -> None:
        if root is not None:
            self.root = Path(root).expanduser()
        else:
            env_root = os.environ.get("CODEX_HOME", "").strip()
            self.root = Path(env_root).expanduser() if env_root else Path.home() / ".codex"

    def _find_state_db(self) -> Path | None:
        return highest_numbered_file(self.root, "state_", ".sqlite")

    def _find_history_db(self) -> Path | None:
        return highest_numbered_file(self.root, "thread_history_", ".sqlite")

    def is_available(self) -> bool:
        try:
            if not self.root.is_dir():
                return False
            return (
                (self.root / "sessions").is_dir()
                or (self.root / "archived_sessions").is_dir()
                or (self.root / "session_index.jsonl").is_file()
                or self._find_state_db() is not None
                or self._find_history_db() is not None
            )
        except OSError:
            return False

    def can_handle_path(self, path: Path) -> bool:
        target = Path(path)
        if not _is_rollout_filename(target):
            return False
        if target.name in {"session_index.jsonl", "history.jsonl"}:
            return False
        try:
            if target.resolve().is_relative_to(self.root.resolve()):
                return True
        except OSError:
            pass
        if target.name.startswith("rollout-"):
            return True
        try:
            if not target.is_file():
                return False
            if target.name.lower().endswith(".jsonl.zst"):
                return True
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                checked = 0
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    checked += 1
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        if checked >= 10:
                            break
                        continue
                    if isinstance(obj, dict) and obj.get("type") in _CODEX_RECORD_TYPES:
                        return True
                    if checked >= 10:
                        break
        except OSError:
            return False
        return False

    def _read_session_index(self, warnings: list[str]) -> dict[str, dict[str, str]]:
        index_path = self.root / "session_index.jsonl"
        entries: dict[str, dict[str, str]] = {}
        if not index_path.is_file():
            return entries
        malformed = 0
        try:
            with index_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        malformed += 1
                        continue
                    if not isinstance(obj, dict):
                        malformed += 1
                        continue
                    sid = str(obj.get("id") or obj.get("session_id") or "").strip()
                    if not sid:
                        continue
                    title = str(obj.get("thread_name") or obj.get("title") or obj.get("name") or "").strip()
                    updated = normalize_timestamp(obj.get("updated_at"))
                    prev = entries.get(sid, {})
                    entries[sid] = {
                        "title": title or prev.get("title", ""),
                        "updated_at": updated or prev.get("updated_at", ""),
                    }
        except OSError as exc:
            _add_unique(warnings, f"codex_session_index_error:{exc}")
        if malformed:
            _add_unique(warnings, f"codex_session_index_malformed_lines:{malformed}")
        return entries

    def _read_state_threads(self, warnings: list[str]) -> tuple[dict[str, dict[str, Any]], Path | None]:
        state_db = self._find_state_db()
        if state_db is None:
            return {}, None
        rows_by_id: dict[str, dict[str, Any]] = {}
        try:
            conn = open_sqlite_readonly(state_db)
        except ProviderReadError as exc:
            _add_unique(warnings, f"codex_state_db_error:{exc}")
            return {}, state_db
        try:
            conn.row_factory = sqlite3.Row
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "threads" not in tables:
                _add_unique(warnings, f"codex_state_db_missing_threads_table:{state_db.name}")
                return {}, state_db
            for row in conn.execute("SELECT * FROM threads").fetchall():
                data = dict(row)
                sid = str(data.get("id") or "").strip()
                if sid:
                    rows_by_id[sid] = data
        except sqlite3.Error as exc:
            _add_unique(warnings, f"codex_state_db_error:{exc}")
        finally:
            conn.close()
        return rows_by_id, state_db

    def _read_history_overview(self, warnings: list[str]) -> tuple[dict[str, dict[str, Any]], Path | None]:
        history_db = self._find_history_db()
        if history_db is None:
            return {}, None
        overview: dict[str, dict[str, Any]] = {}
        try:
            conn = open_sqlite_readonly(history_db)
        except ProviderReadError as exc:
            _add_unique(warnings, f"codex_history_db_error:{exc}")
            return {}, history_db
        try:
            conn.row_factory = sqlite3.Row
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "thread_items" not in tables:
                return {}, history_db

            for row in conn.execute(
                "SELECT thread_id, COUNT(*) AS cnt FROM thread_items GROUP BY thread_id"
            ).fetchall():
                sid = str(row["thread_id"] or "").strip()
                if sid and row["cnt"] > 0:
                    overview[sid] = {
                        "item_count": int(row["cnt"]),
                        "turn_count": 0,
                        "first_prompt": "",
                        "last_prompt": "",
                    }

            cols = {r[1] for r in conn.execute("PRAGMA table_info(thread_items)").fetchall()}
            order_cols = [c for c in ("rollout_ordinal", "created_at_ms") if c in cols]
            order_sql = f" ORDER BY {', '.join(order_cols)}" if order_cols else ""
            if "item_type" in cols:
                has_empty_type = conn.execute(
                    "SELECT 1 FROM thread_items WHERE item_type = '' LIMIT 1"
                ).fetchone()
                if has_empty_type:
                    sql = (
                        "SELECT thread_id, item_type, item_json FROM thread_items "
                        "WHERE item_type = 'userMessage' OR item_type = ''"
                        f"{order_sql}"
                    )
                else:
                    sql = (
                        "SELECT thread_id, item_type, item_json FROM thread_items "
                        "WHERE item_type = 'userMessage'"
                        f"{order_sql}"
                    )
            else:
                sql = f"SELECT thread_id, '' AS item_type, item_json FROM thread_items{order_sql}"

            secret_counter = [0]
            for row in conn.execute(sql).fetchall():
                sid = str(row["thread_id"] or "").strip()
                if sid not in overview:
                    continue
                try:
                    obj = json.loads(row["item_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                itype = str(row["item_type"] or obj.get("type") or "").strip()
                if itype != "userMessage":
                    continue
                raw_text = _extract_message_text(obj.get("content"))
                preview = _preview_line(raw_text, _PROMPT_PREVIEW_LIMIT, secret_counter)
                if not preview:
                    continue
                entry = overview[sid]
                entry["turn_count"] += 1
                if not entry["first_prompt"]:
                    entry["first_prompt"] = preview
                entry["last_prompt"] = preview
        except sqlite3.Error as exc:
            _add_unique(warnings, f"codex_history_db_error:{exc}")
        finally:
            conn.close()
        return overview, history_db

    def _scan_rollout_summary(
        self,
        path: Path,
        warnings: list[str],
        *,
        archived: bool,
    ) -> dict[str, Any]:
        file_warnings: list[str] = []
        info: dict[str, Any] = {
            "session_id": _extract_session_id_from_name(path),
            "source_path": str(path.resolve()),
            "cwd": "",
            "git_branch": "",
            "created_at": "",
            "updated_at": "",
            "first_prompt": "",
            "last_prompt": "",
            "turn_count": 0,
            "archived": archived,
            "warnings": file_warnings,
        }
        secret_counter = [0]
        interesting_tokens = (
            '"session_meta"',
            '"turn_context"',
            '"compacted"',
            '"message"',
            '"user_message"',
            '"thread_rolled_back"',
        )

        def _scan_lines(lines_iter: Any) -> None:
            response_prompts: list[str] = []
            legacy_prompts: list[str] = []
            saw_response_msg = False
            malformed = 0
            last_non_empty = ""

            for line in lines_iter:
                stripped = line.strip()
                if not stripped:
                    continue
                last_non_empty = stripped
                if not stripped.startswith("{"):
                    malformed += 1
                    continue
                if info["created_at"] and not any(tok in stripped for tok in interesting_tokens):
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(obj, dict):
                    malformed += 1
                    continue
                rec_type = obj.get("type")
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                ts = normalize_timestamp(obj.get("timestamp") or payload.get("timestamp"))
                if ts:
                    if not info["created_at"]:
                        info["created_at"] = ts
                    info["updated_at"] = ts

                if rec_type == "session_meta":
                    sid = str(payload.get("id") or payload.get("session_id") or "").strip()
                    if sid:
                        info["session_id"] = sid
                    if payload.get("cwd") and not info["cwd"]:
                        info["cwd"] = normalize_path(str(payload["cwd"]))
                    git_obj = payload.get("git")
                    branch = git_obj.get("branch") if isinstance(git_obj, dict) else payload.get("git_branch")
                    if isinstance(branch, str) and branch.strip() and not info["git_branch"]:
                        info["git_branch"] = branch.strip()
                elif rec_type == "turn_context":
                    if payload.get("cwd") and not info["cwd"]:
                        info["cwd"] = normalize_path(str(payload["cwd"]))
                elif rec_type == "compacted":
                    rep = payload.get("replacement_history")
                    if isinstance(rep, list):
                        response_prompts.clear()
                        legacy_prompts.clear()
                        saw_response_msg = False
                        for item in rep:
                            if not isinstance(item, dict):
                                continue
                            item_payload = (
                                item["payload"]
                                if item.get("type") == "response_item" and isinstance(item.get("payload"), dict)
                                else item
                            )
                            role = str(item_payload.get("role") or "").lower()
                            if role not in {"user", "assistant"}:
                                continue
                            txt = _extract_message_text(
                                item_payload.get("content"),
                                item_payload.get("internal_chat_message_metadata_passthrough"),
                            )
                            prev = _preview_line(txt, _PROMPT_PREVIEW_LIMIT, secret_counter)
                            if prev:
                                saw_response_msg = True
                                if role == "user":
                                    response_prompts.append(prev)
                elif rec_type == "response_item" and payload.get("type") == "message":
                    role = str(payload.get("role") or "").lower()
                    if role in {"user", "assistant"}:
                        txt = _extract_message_text(
                            payload.get("content"),
                            payload.get("internal_chat_message_metadata_passthrough"),
                        )
                        prev = _preview_line(txt, _PROMPT_PREVIEW_LIMIT, secret_counter)
                        if prev:
                            saw_response_msg = True
                            if role == "user":
                                response_prompts.append(prev)
                elif rec_type == "event_msg":
                    etype = str(payload.get("type") or "")
                    if etype == "user_message":
                        raw = payload.get("message") or payload.get("text") or ""
                        prev = _preview_line(raw, _PROMPT_PREVIEW_LIMIT, secret_counter)
                        if prev:
                            legacy_prompts.append(prev)
                    elif etype == "thread_rolled_back":
                        num = int(payload.get("num_turns") or payload.get("numTurns") or 0)
                        if num > 0:
                            del response_prompts[-num:]
                            del legacy_prompts[-num:]

            if last_non_empty.startswith("{"):
                try:
                    last_obj = json.loads(last_non_empty)
                    if isinstance(last_obj, dict):
                        last_ts = normalize_timestamp(last_obj.get("timestamp"))
                        if last_ts:
                            info["updated_at"] = last_ts
                except json.JSONDecodeError:
                    pass

            if malformed:
                msg = f"codex_malformed_jsonl_lines:{malformed}"
                _add_unique(file_warnings, msg)
                _add_unique(warnings, msg)
            prompts = response_prompts if saw_response_msg else legacy_prompts
            info["turn_count"] = len(prompts)
            if prompts:
                info["first_prompt"] = prompts[0]
                info["last_prompt"] = prompts[-1]

        if path.name.lower().endswith(".jsonl.zst"):
            lines = _iter_rollout_lines(path, file_warnings)
            for fw in file_warnings:
                _add_unique(warnings, fw)
            if lines is not None:
                _scan_lines(lines)
        else:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    _scan_lines(handle)
            except OSError as exc:
                msg = f"codex_rollout_read_error:{path}:{exc}"
                _add_unique(file_warnings, msg)
                _add_unique(warnings, msg)
        return info

    def _discover_rollout_files(self, *, include_archived: bool) -> list[tuple[Path, bool]]:
        found: list[tuple[Path, bool]] = []
        dirs: list[tuple[Path, bool]] = [(self.root / "sessions", False)]
        if include_archived:
            dirs.append((self.root / "archived_sessions", True))
        for base_dir, is_archived in dirs:
            if not base_dir.is_dir():
                continue
            try:
                for entry in sorted(base_dir.rglob("*")):
                    if entry.is_file() and _is_rollout_filename(entry):
                        found.append((entry, is_archived))
            except OSError:
                continue
        return found

    def _summary_from_state_row(
        self,
        sid: str,
        row: dict[str, Any],
        *,
        rollout_file: Path | None,
        rollout_scan: dict[str, Any] | None,
        hist_info: dict[str, Any] | None,
        state_db: Path | None,
        history_db: Path | None,
        index_entry: dict[str, str],
        archived_flag: bool,
        secret_counter: list[int],
        history_checked: bool = True,
    ) -> SessionSummary:
        cwd = normalize_path(
            str(row.get("cwd") or (rollout_scan["cwd"] if rollout_scan else "") or "")
        )
        branch = str(
            row.get("git_branch") or (rollout_scan["git_branch"] if rollout_scan else "") or ""
        ).strip()
        created_at = (
            normalize_timestamp(row.get("created_at"))
            or normalize_timestamp(row.get("created_at_ms"))
            or (rollout_scan["created_at"] if rollout_scan else "")
        )
        updated_at = (
            normalize_timestamp(row.get("updated_at"))
            or normalize_timestamp(row.get("updated_at_ms"))
            or normalize_timestamp(row.get("recency_at"))
            or normalize_timestamp(row.get("recency_at_ms"))
            or (rollout_scan["updated_at"] if rollout_scan else "")
        )

        item_warnings: list[str] = list(rollout_scan["warnings"]) if rollout_scan else []
        if rollout_file is not None:
            source_kind = "rollout_jsonl"
            source_path = str(rollout_file.resolve())
        elif hist_info:
            source_kind = "sqlite_history"
            source_path = str(history_db.resolve()) if history_db else ""
        else:
            source_kind = "metadata_only"
            source_path = str(state_db.resolve()) if state_db else ""
            if history_checked:
                _add_unique(item_warnings, "metadata_only_no_transcript")

        raw_first = str(row.get("first_user_message") or row.get("preview") or "")
        first_prompt = (
            (rollout_scan["first_prompt"] if rollout_scan and rollout_scan["first_prompt"] else "")
            or (hist_info["first_prompt"] if hist_info and hist_info["first_prompt"] else "")
            or _preview_line(raw_first, _PROMPT_PREVIEW_LIMIT, secret_counter)
        )
        last_prompt = (
            (rollout_scan["last_prompt"] if rollout_scan and rollout_scan["last_prompt"] else "")
            or (hist_info["last_prompt"] if hist_info and hist_info["last_prompt"] else "")
            or first_prompt
        )
        turn_count = (
            rollout_scan["turn_count"]
            if rollout_scan is not None
            else (hist_info["turn_count"] if hist_info else (1 if first_prompt else 0))
        )

        raw_title = str(row.get("name") or "").strip() or str(row.get("title") or "").strip()
        title, updated_at = _merge_index_metadata(
            raw_title, updated_at, first_prompt, index_entry, secret_counter
        )

        return SessionSummary(
            provider=self.name,
            session_id=sid,
            source_kind=source_kind,
            source_path=source_path,
            cwd=cwd,
            repo_name=derive_repo_name(cwd),
            git_branch=branch,
            title=title,
            first_user_prompt=first_prompt,
            last_user_prompt=last_prompt,
            created_at=created_at,
            updated_at=updated_at,
            turn_count=turn_count,
            archived=archived_flag,
            warnings=item_warnings,
        )

    def _build_inventory(
        self,
        *,
        include_archived: bool,
        warnings: list[str],
    ) -> dict[str, dict[str, Any]]:
        state_rows, state_db = self._read_state_threads(warnings)
        history_overview, history_db = self._read_history_overview(warnings)
        index_entries = self._read_session_index(warnings)
        rollout_files = self._discover_rollout_files(include_archived=include_archived)

        disk_by_sid: dict[str, tuple[Path, bool]] = {}
        extra_scans: list[dict[str, Any]] = []

        for file_path, is_arch in rollout_files:
            sid_hint = _extract_session_id_from_name(file_path)
            if sid_hint and sid_hint in state_rows:
                disk_by_sid[sid_hint] = (file_path, is_arch)
            else:
                scanned = self._scan_rollout_summary(file_path, warnings, archived=is_arch)
                sid = scanned["session_id"] or str(file_path.resolve())
                scanned["session_id"] = sid
                extra_scans.append(scanned)
                disk_by_sid[sid] = (file_path, is_arch)

        records: dict[str, dict[str, Any]] = {}
        secret_counter = [0]

        for sid, row in state_rows.items():
            archived_flag = bool(row.get("archived"))
            rollout_file = _resolve_existing_path(str(row.get("rollout_path") or ""), self.root)
            if rollout_file is None and sid in disk_by_sid:
                rollout_file, disk_arch = disk_by_sid[sid]
                archived_flag = archived_flag or disk_arch

            rollout_scan: dict[str, Any] | None = None
            if rollout_file is not None and (not archived_flag or include_archived):
                rollout_scan = self._scan_rollout_summary(rollout_file, warnings, archived=archived_flag)

            summary = self._summary_from_state_row(
                sid,
                row,
                rollout_file=rollout_file,
                rollout_scan=rollout_scan,
                hist_info=history_overview.get(sid),
                state_db=state_db,
                history_db=history_db,
                index_entry=index_entries.get(sid, {}),
                archived_flag=archived_flag,
                secret_counter=secret_counter,
            )
            records[sid] = {"summary": summary, "rollout_path": rollout_file}

        for scanned in extra_scans:
            sid = scanned["session_id"]
            if sid in records:
                continue
            title, updated_at = _merge_index_metadata(
                "",
                scanned["updated_at"],
                scanned["first_prompt"],
                index_entries.get(sid, {}),
                secret_counter,
            )
            cwd = scanned["cwd"]
            summary = SessionSummary(
                provider=self.name,
                session_id=sid,
                source_kind="rollout_jsonl",
                source_path=scanned["source_path"],
                cwd=cwd,
                repo_name=derive_repo_name(cwd),
                git_branch=scanned["git_branch"],
                title=title,
                first_user_prompt=scanned["first_prompt"],
                last_user_prompt=scanned["last_prompt"],
                created_at=scanned["created_at"],
                updated_at=updated_at,
                turn_count=scanned["turn_count"],
                archived=scanned["archived"],
                warnings=list(scanned.get("warnings", [])),
            )
            records[sid] = {"summary": summary, "rollout_path": Path(scanned["source_path"])}

        return records

    def list_sessions(
        self, *, include_archived: bool = False
    ) -> tuple[list[SessionSummary], list[str]]:
        warnings: list[str] = []
        inventory = self._build_inventory(include_archived=include_archived, warnings=warnings)
        summaries = [
            entry["summary"]
            for entry in inventory.values()
            if include_archived or not entry["summary"].archived
        ]
        summaries.sort(
            key=lambda s: (timestamp_to_epoch_ms(s.updated_at), timestamp_to_epoch_ms(s.created_at), s.session_id),
            reverse=True,
        )
        return summaries, warnings

    def _slice_has_response_messages(
        self,
        seed_items: list[dict[str, Any]],
        active_records: list[dict[str, Any]],
        secret_counter: list[int],
    ) -> bool:
        def _has_real_msg(payload: Any) -> bool:
            if not isinstance(payload, dict):
                return False
            if str(payload.get("role") or "").lower() not in {"user", "assistant"}:
                return False
            raw = _extract_message_text(
                payload.get("content"),
                payload.get("internal_chat_message_metadata_passthrough"),
            )
            return bool(_clean_text_counted(raw, secret_counter, strip_tags=True))

        for item in seed_items:
            payload = (
                item["payload"]
                if item.get("type") == "response_item" and isinstance(item.get("payload"), dict)
                else item
            )
            if _has_real_msg(payload):
                return True
        for rec in active_records:
            if rec.get("type") != "response_item":
                continue
            payload = rec.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "message" and _has_real_msg(payload):
                return True
        return False

    def _parse_rollout_detail(
        self,
        path: Path,
        base_summary: SessionSummary | None,
        *,
        max_tool_chars: int,
    ) -> tuple[SessionDetail, bool]:
        warnings: list[str] = list(base_summary.warnings) if base_summary else []
        lines = _iter_rollout_lines(path, warnings)
        if lines is None:
            fallback_summary = base_summary or self._empty_summary(path, source_kind="rollout_jsonl", warnings=warnings)
            return SessionDetail(summary=fallback_summary, turns=[], warnings=list(warnings)), False

        records, malformed = _parse_rollout_json_lines(lines, warnings)
        if not records:
            if malformed > 0:
                _add_unique(warnings, f"codex_corrupt_rollout:{path}")
            else:
                _add_unique(warnings, f"codex_empty_rollout:{path}")
            fallback_summary = base_summary or self._empty_summary(path, source_kind="rollout_jsonl", warnings=warnings)
            return SessionDetail(summary=fallback_summary, turns=[], warnings=list(warnings)), False

        meta_session_id = _extract_session_id_from_name(path)
        meta_cwd = ""
        meta_branch = ""
        created_at = ""
        updated_at = ""
        compaction_count = 0
        last_compacted_idx: int | None = None

        for idx, rec in enumerate(records):
            rec_type = rec.get("type")
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            ts = normalize_timestamp(rec.get("timestamp") or payload.get("timestamp"))
            if ts:
                if not created_at:
                    created_at = ts
                updated_at = ts
            if rec_type == "session_meta":
                # Explicitly ignore payload["base_instructions"] per untrusted-history boundary.
                sid = str(payload.get("id") or payload.get("session_id") or "").strip()
                if sid:
                    meta_session_id = sid
                if payload.get("cwd"):
                    meta_cwd = normalize_path(str(payload["cwd"]))
                git_obj = payload.get("git")
                branch = git_obj.get("branch") if isinstance(git_obj, dict) else payload.get("git_branch")
                if isinstance(branch, str) and branch.strip():
                    meta_branch = branch.strip()
            elif rec_type == "turn_context":
                if payload.get("cwd") and not meta_cwd:
                    meta_cwd = normalize_path(str(payload["cwd"]))
            elif rec_type == "compacted":
                compaction_count += 1
                _add_unique(warnings, "compaction_gap")
                if isinstance(payload.get("replacement_history"), list):
                    last_compacted_idx = idx

        seed_items: list[dict[str, Any]] = []
        if last_compacted_idx is not None:
            rep = records[last_compacted_idx].get("payload", {}).get("replacement_history")
            if isinstance(rep, list):
                seed_items = [item for item in rep if isinstance(item, dict)]
            active_records = records[last_compacted_idx + 1 :]
        else:
            active_records = records

        secret_counter = [0]
        has_response_messages = self._slice_has_response_messages(seed_items, active_records, [0])
        acc = _TurnAccumulator()
        calls_by_id: dict[str, ToolEvent] = {}
        active_turn_id = ""

        def _attach_tool_call(
            *,
            call_id: str,
            tool_name: str,
            raw_args: Any,
            ts: str,
            turn_id: str,
            is_patch: bool = False,
        ) -> None:
            event, cmd = _summarize_tool_call(
                call_id=call_id,
                tool_name=tool_name,
                raw_args=raw_args,
                max_tool_chars=max_tool_chars,
                secret_counter=secret_counter,
                is_patch=is_patch,
            )
            turn = acc.active_turn(ts, turn_id)
            turn.tool_events.append(event)
            if call_id:
                calls_by_id[call_id] = event
            acc.add_command(cmd)

        def _handle_response_payload(payload: dict[str, Any], ts: str) -> None:
            ptype = str(payload.get("type") or "")
            if ptype in _SKIP_RESPONSE_TYPES:
                return
            passthrough = payload.get("internal_chat_message_metadata_passthrough")
            msg_turn_id = (
                str(passthrough.get("turn_id") or "") if isinstance(passthrough, dict) else ""
            ) or active_turn_id

            if ptype == "message" or ("role" in payload and not ptype):
                role = str(payload.get("role") or "").lower()
                if role in {"developer", "system"}:
                    return
                raw_text = _extract_message_text(
                    payload.get("content"),
                    passthrough if isinstance(passthrough, dict) else None,
                )
                cleaned = _clean_text_counted(raw_text, secret_counter, strip_tags=True)
                if role == "user":
                    acc.add_user_message(cleaned, ts, msg_turn_id)
                elif role == "assistant":
                    acc.add_assistant_message(cleaned, ts, msg_turn_id)
                return

            if ptype == "function_call":
                _attach_tool_call(
                    call_id=str(payload.get("call_id") or payload.get("id") or ""),
                    tool_name=_qualify_tool_name(
                        str(payload.get("name") or "tool"), str(payload.get("namespace") or "")
                    ),
                    raw_args=payload.get("arguments", ""),
                    ts=ts,
                    turn_id=msg_turn_id,
                )
                return

            if ptype == "custom_tool_call":
                name = str(payload.get("name") or "custom_tool")
                _attach_tool_call(
                    call_id=str(payload.get("call_id") or payload.get("id") or ""),
                    tool_name=name,
                    raw_args=payload.get("input", ""),
                    ts=ts,
                    turn_id=msg_turn_id,
                    is_patch=(name == "apply_patch"),
                )
                return

            if ptype == "local_shell_call":
                action = payload.get("action")
                _attach_tool_call(
                    call_id=str(payload.get("call_id") or payload.get("id") or ""),
                    tool_name=str(payload.get("name") or "local_shell"),
                    raw_args=action if action is not None else payload,
                    ts=ts,
                    turn_id=msg_turn_id,
                )
                return

            if ptype in {"function_call_output", "custom_tool_call_output"}:
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                raw_out, detected_status = _normalize_tool_output(payload.get("output"))
                status = _status_from_mapping(payload) or detected_status or "ok"
                out_summary, out_truncated = _format_output_summary(
                    raw_out, max_tool_chars, secret_counter
                )
                existing = calls_by_id.get(call_id) if call_id else None
                if existing is not None:
                    existing.output_summary = out_summary
                    existing.status = status
                    existing.truncated = existing.truncated or out_truncated
                elif acc.turns:
                    qualified = _qualify_tool_name(
                        str(payload.get("name") or "tool_output"),
                        str(payload.get("namespace") or ""),
                    )
                    acc.turns[-1].tool_events.append(
                        ToolEvent(
                            call_id=call_id,
                            tool_name=qualified,
                            arguments_summary="",
                            output_summary=out_summary,
                            status=status,
                            truncated=out_truncated,
                        )
                    )

        for item in seed_items:
            item_payload = (
                item["payload"]
                if item.get("type") == "response_item" and isinstance(item.get("payload"), dict)
                else item
            )
            _handle_response_payload(item_payload, created_at)

        if last_compacted_idx is not None:
            acc.pending_compaction = True

        for rec in active_records:
            rec_type = str(rec.get("type") or "")
            if rec_type in _SKIP_TOP_TYPES:
                continue
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            ts = normalize_timestamp(rec.get("timestamp") or payload.get("timestamp"))

            if rec_type == "turn_context":
                if payload.get("turn_id"):
                    active_turn_id = str(payload["turn_id"])
            elif rec_type == "compacted":
                acc.pending_compaction = True
            elif rec_type == "response_item":
                _handle_response_payload(payload, ts)
            elif rec_type == "event_msg":
                etype = str(payload.get("type") or "")
                if etype in _SKIP_EVENT_TYPES:
                    continue
                if etype == "task_started":
                    if payload.get("turn_id"):
                        active_turn_id = str(payload["turn_id"])
                elif etype in {"user_message", "agent_message"} and not has_response_messages:
                    raw = payload.get("message") or payload.get("text") or payload.get("content")
                    txt = _extract_message_text(raw) if isinstance(raw, list) else str(raw or "")
                    cleaned = _clean_text_counted(txt, secret_counter, strip_tags=True)
                    tid = str(payload.get("turn_id") or active_turn_id)
                    if etype == "user_message":
                        acc.add_user_message(cleaned, ts, tid)
                    else:
                        acc.add_assistant_message(cleaned, ts, tid)
                elif etype == "turn_aborted":
                    target_turn = acc.find_turn(str(payload.get("turn_id") or ""))
                    if target_turn is not None:
                        for te in target_turn.tool_events:
                            if te.status == "unknown":
                                te.status = "aborted"
                        reason = _preview_line(payload.get("reason") or "", 120, secret_counter)
                        _add_unique(target_turn.warnings, f"turn_aborted:{reason}" if reason else "turn_aborted")
                    _add_unique(warnings, "turn_aborted")
                elif etype == "thread_rolled_back":
                    num = int(payload.get("num_turns") or payload.get("numTurns") or 0)
                    if acc.rollback(num):
                        _add_unique(warnings, "thread_rolled_back")

        detail = self._finalize_detail(
            turns=acc.turns,
            turn_commands=acc.turn_commands,
            compaction_count=compaction_count,
            secret_counter=secret_counter,
            warnings=warnings,
            base_summary=base_summary,
            session_id=meta_session_id or (base_summary.session_id if base_summary else str(path.resolve())),
            source_kind="rollout_jsonl",
            source_path=str(path.resolve()),
            cwd=meta_cwd or (base_summary.cwd if base_summary else ""),
            git_branch=meta_branch or (base_summary.git_branch if base_summary else ""),
            created_at=created_at or (base_summary.created_at if base_summary else ""),
            updated_at=updated_at or (base_summary.updated_at if base_summary else ""),
        )
        return detail, True

    def _load_sqlite_history_detail(
        self,
        thread_id: str,
        base_summary: SessionSummary,
        *,
        max_tool_chars: int,
        warnings: list[str],
    ) -> SessionDetail | None:
        history_db = self._find_history_db()
        if history_db is None:
            return None
        try:
            conn = open_sqlite_readonly(history_db)
        except ProviderReadError as exc:
            _add_unique(warnings, f"codex_history_db_error:{exc}")
            return None

        try:
            conn.row_factory = sqlite3.Row
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "thread_items" not in tables:
                return None

            turn_meta: dict[str, dict[str, Any]] = {}
            if "thread_turns" in tables:
                for row in conn.execute(
                    "SELECT * FROM thread_turns WHERE thread_id = ? ORDER BY rollout_ordinal",
                    (thread_id,),
                ).fetchall():
                    d = dict(row)
                    tid = str(d.get("turn_id") or "")
                    if tid:
                        turn_meta[tid] = d

            item_cols = {r[1] for r in conn.execute("PRAGMA table_info(thread_items)").fetchall()}
            order_cols = [c for c in ("rollout_ordinal", "created_at_ms") if c in item_cols]
            order_sql = f" ORDER BY {', '.join(order_cols)}" if order_cols else ""
            item_rows = conn.execute(
                f"SELECT * FROM thread_items WHERE thread_id = ?{order_sql}",
                (thread_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            _add_unique(warnings, f"codex_history_db_error:{exc}")
            return None
        finally:
            conn.close()

        if not item_rows:
            return None

        secret_counter = [0]
        compaction_count = 0
        malformed = 0

        def _apply_turn_meta(turn: NormalizedTurn, tid: str) -> None:
            meta = turn_meta.get(tid)
            if not meta:
                return
            status_val = str(meta.get("status") or "").lower()
            if status_val in {"aborted", "interrupted"}:
                _add_unique(turn.warnings, f"turn_{status_val}")
            if meta.get("error_json"):
                _add_unique(turn.warnings, "turn_error")

        acc = _TurnAccumulator(on_new_turn=_apply_turn_meta)

        for row in item_rows:
            data = dict(row)
            tid = str(data.get("turn_id") or "")
            item_id = str(data.get("item_id") or "")
            ts = normalize_timestamp(turn_meta.get(tid, {}).get("started_at") or data.get("created_at_ms"))
            try:
                obj = json.loads(data.get("item_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                malformed += 1
                continue
            if not isinstance(obj, dict):
                malformed += 1
                continue

            itype = str(data.get("item_type") or obj.get("type") or "").strip()
            if itype in {"reasoning", "sleep"}:
                continue

            if itype == "contextCompaction":
                compaction_count += 1
                acc.pending_compaction = True
                _add_unique(warnings, "compaction_gap")
            elif itype == "userMessage":
                cleaned = _clean_text_counted(_extract_message_text(obj.get("content")), secret_counter, strip_tags=True)
                acc.add_user_message(cleaned, ts, tid)
            elif itype == "agentMessage":
                cleaned = _clean_text_counted(obj.get("text") or "", secret_counter, strip_tags=True)
                acc.add_assistant_message(cleaned, ts, tid)
            elif itype == "commandExecution":
                cmd = _command_to_string(obj.get("command"))
                bounded_cmd = _preview_line(cmd, _CMD_PREVIEW_LIMIT, secret_counter)
                cwd_val = str(obj.get("cwd") or "")
                arg_text = f"{cmd} (cwd={cwd_val})" if cmd and cwd_val else cmd
                arg_summary, arg_trunc = _format_args_summary(arg_text, max_tool_chars, secret_counter)
                out_raw = obj.get("aggregatedOutput") if "aggregatedOutput" in obj else obj.get("output")
                out_summary, out_trunc = _format_output_summary(str(out_raw or ""), max_tool_chars, secret_counter)
                status_raw = str(obj.get("status") or "").lower()
                exit_code = obj.get("exitCode") if "exitCode" in obj else obj.get("exit_code")
                if status_raw == "completed" and exit_code in (None, 0):
                    status = "ok"
                elif status_raw in {"aborted", "cancelled", "interrupted"}:
                    status = "aborted"
                else:
                    status = "error"
                acc.active_turn(ts, tid).tool_events.append(
                    ToolEvent(
                        call_id=item_id,
                        tool_name="shell",
                        arguments_summary=arg_summary,
                        output_summary=out_summary,
                        status=status,
                        truncated=arg_trunc or out_trunc,
                    )
                )
                acc.add_command(bounded_cmd)
            elif itype == "fileChange":
                changes = obj.get("changes") if isinstance(obj.get("changes"), list) else []
                touched: list[str] = []
                summaries: list[str] = []
                diffs: list[str] = []
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    cpath = str(change.get("path") or "").strip()
                    if cpath:
                        _add_unique(touched, cpath)
                    kind_obj = change.get("kind")
                    kind_name = kind_obj.get("type") if isinstance(kind_obj, dict) else str(kind_obj or "change")
                    summaries.append(f"{kind_name}:{cpath}" if cpath else str(kind_name))
                    if isinstance(change.get("diff"), str) and change["diff"]:
                        diffs.append(change["diff"])
                arg_summary, arg_trunc = _format_args_summary(", ".join(summaries), max_tool_chars, secret_counter)
                status_raw = str(obj.get("status") or "").lower()
                out_summary, out_trunc = _format_output_summary(
                    "\n".join(diffs) if diffs else status_raw, max_tool_chars, secret_counter
                )
                status = (
                    "error"
                    if status_raw in {"failed", "error"}
                    else ("ok" if status_raw in {"completed", "applied", "success", ""} else "unknown")
                )
                acc.active_turn(ts, tid).tool_events.append(
                    ToolEvent(
                        call_id=item_id,
                        tool_name="file_change",
                        arguments_summary=arg_summary,
                        output_summary=out_summary,
                        status=status,
                        files_touched=touched,
                        truncated=arg_trunc or out_trunc,
                    )
                )
            elif itype == "mcpToolCall":
                server = str(obj.get("server") or "").strip()
                tool = str(obj.get("tool") or "").strip()
                tool_name = f"mcp:{server}.{tool}" if server and tool else f"mcp:{server or tool or 'unknown'}"
                args_val = obj.get("arguments")
                files_touched = _extract_files_from_mapping(args_val) if isinstance(args_val, dict) else []
                arg_summary, arg_trunc = _format_args_summary(args_val, max_tool_chars, secret_counter)
                err_val = obj.get("error")
                res_val = obj.get("result")
                status_raw = str(obj.get("status") or "").lower()
                status = "error" if status_raw == "failed" or bool(err_val) else ("ok" if status_raw == "completed" else "unknown")
                raw_out, _ = _normalize_tool_output(err_val if err_val else res_val)
                out_summary, out_trunc = _format_output_summary(raw_out, max_tool_chars, secret_counter)
                acc.active_turn(ts, tid).tool_events.append(
                    ToolEvent(
                        call_id=item_id,
                        tool_name=tool_name,
                        arguments_summary=arg_summary,
                        output_summary=out_summary,
                        status=status,
                        files_touched=files_touched,
                        truncated=arg_trunc or out_trunc,
                    )
                )
            elif itype == "webSearch":
                arg_summary, arg_trunc = _format_args_summary(str(obj.get("query") or ""), max_tool_chars, secret_counter)
                results = obj.get("results")
                res_text = ""
                if isinstance(results, list) and results:
                    titles = [str(r.get("title") or r.get("url") or "") for r in results[:5] if isinstance(r, dict)]
                    res_text = f"{len(results)} results: " + "; ".join(t for t in titles if t)
                out_summary, out_trunc = _format_output_summary(res_text, max_tool_chars, secret_counter)
                acc.active_turn(ts, tid).tool_events.append(
                    ToolEvent(
                        call_id=item_id,
                        tool_name="web_search",
                        arguments_summary=arg_summary,
                        output_summary=out_summary,
                        status="ok",
                        truncated=arg_trunc or out_trunc,
                    )
                )
            elif itype == "collabAgentToolCall":
                ctool = str(obj.get("tool") or "collab")
                cstatus = str(obj.get("status") or "unknown")
                receivers = obj.get("receiverThreadIds")
                count = len(receivers) if isinstance(receivers, list) else 0
                note = _preview_line(f"collab:{ctool} status={cstatus} targets={count}", _PROMPT_PREVIEW_LIMIT, secret_counter)
                _add_unique(acc.active_turn(ts, tid).subagent_notes, note)
            elif itype == "subAgentActivity":
                kind = str(obj.get("kind") or "activity")
                target = str(obj.get("agentPath") or obj.get("agentThreadId") or "")
                note = _preview_line(f"subagent:{kind} {target}".strip(), _PROMPT_PREVIEW_LIMIT, secret_counter)
                _add_unique(acc.active_turn(ts, tid).subagent_notes, note)
            elif itype == "functionCallOutput":
                qualified = _qualify_tool_name(
                    str(obj.get("name") or "tool_output"), str(obj.get("namespace") or "")
                )
                raw_out, detected = _normalize_tool_output(obj.get("output"))
                out_summary, out_trunc = _format_output_summary(raw_out, max_tool_chars, secret_counter)
                acc.active_turn(ts, tid).tool_events.append(
                    ToolEvent(
                        call_id=item_id,
                        tool_name=qualified,
                        arguments_summary="",
                        output_summary=out_summary,
                        status=detected or "ok",
                        truncated=out_trunc,
                    )
                )
            elif itype in {"imageView", "imageGeneration"}:
                _add_unique(warnings, "media_omitted")
                acc.active_turn(ts, tid).tool_events.append(
                    ToolEvent(
                        call_id=item_id,
                        tool_name=itype,
                        arguments_summary="[media omitted]",
                        output_summary="[media omitted]",
                        status="ok",
                    )
                )

        if malformed:
            _add_unique(warnings, f"codex_sqlite_malformed_items:{malformed}")

        return self._finalize_detail(
            turns=acc.turns,
            turn_commands=acc.turn_commands,
            compaction_count=compaction_count,
            secret_counter=secret_counter,
            warnings=warnings,
            base_summary=base_summary,
            session_id=base_summary.session_id,
            source_kind="sqlite_history",
            source_path=str(history_db.resolve()),
            cwd=base_summary.cwd,
            git_branch=base_summary.git_branch,
            created_at=base_summary.created_at,
            updated_at=base_summary.updated_at,
        )

    def _load_metadata_only_detail(
        self,
        base_summary: SessionSummary,
        *,
        warnings: list[str],
    ) -> SessionDetail:
        _add_unique(warnings, "metadata_only_no_transcript")
        prompt = base_summary.first_user_prompt or base_summary.title
        turns: list[NormalizedTurn] = []
        if prompt:
            turns.append(
                NormalizedTurn(
                    turn_index=1,
                    timestamp=base_summary.created_at or base_summary.updated_at,
                    user_text=prompt,
                    assistant_text="",
                    warnings=["metadata_only_no_transcript"],
                )
            )
        return self._finalize_detail(
            turns=turns,
            turn_commands=[[] for _ in turns],
            compaction_count=0,
            secret_counter=[0],
            warnings=warnings,
            base_summary=base_summary,
            session_id=base_summary.session_id,
            source_kind="metadata_only",
            source_path=base_summary.source_path,
            cwd=base_summary.cwd,
            git_branch=base_summary.git_branch,
            created_at=base_summary.created_at,
            updated_at=base_summary.updated_at,
        )

    def _empty_summary(
        self,
        path: Path,
        *,
        source_kind: str,
        warnings: list[str],
    ) -> SessionSummary:
        sid = _extract_session_id_from_name(path) or str(path.resolve())
        return SessionSummary(
            provider=self.name,
            session_id=sid,
            source_kind=source_kind,
            source_path=str(path.resolve()),
            cwd="",
            repo_name="",
            git_branch="",
            title="",
            first_user_prompt="",
            last_user_prompt="",
            created_at="",
            updated_at="",
            turn_count=0,
            archived=False,
            warnings=list(warnings),
        )

    def _finalize_detail(
        self,
        *,
        turns: list[NormalizedTurn],
        turn_commands: list[list[str]],
        compaction_count: int,
        secret_counter: list[int],
        warnings: list[str],
        base_summary: SessionSummary | None,
        session_id: str,
        source_kind: str,
        source_path: str,
        cwd: str,
        git_branch: str,
        created_at: str,
        updated_at: str,
    ) -> SessionDetail:
        files_mentioned: list[str] = []
        commands_run: list[str] = []
        total_chars = 0
        user_prompts: list[str] = []

        for idx, turn in enumerate(turns, start=1):
            turn.turn_index = idx
            total_chars += len(turn.user_text) + len(turn.assistant_text)
            if turn.user_text:
                prev, _ = truncate_text(turn.user_text, _PROMPT_PREVIEW_LIMIT, single_line=True)
                if prev:
                    user_prompts.append(prev)
            for te in turn.tool_events:
                total_chars += len(te.arguments_summary) + len(te.output_summary)
                for fpath in te.files_touched:
                    _add_unique(files_mentioned, fpath)
            if idx - 1 < len(turn_commands):
                for cmd in turn_commands[idx - 1]:
                    _add_unique(commands_run, cmd)

        if secret_counter[0] > 0:
            _add_unique(warnings, f"secrets_redacted:{secret_counter[0]}")

        first_prompt = (
            user_prompts[0]
            if user_prompts
            else (base_summary.first_user_prompt if base_summary else "")
        )
        last_prompt = (
            user_prompts[-1]
            if user_prompts
            else (base_summary.last_user_prompt if base_summary else first_prompt)
        )
        title = (
            (base_summary.title if base_summary and base_summary.title else "")
            or truncate_text(first_prompt, _TITLE_PREVIEW_LIMIT, single_line=True)[0]
        )
        summary = SessionSummary(
            provider=self.name,
            session_id=session_id,
            source_kind=source_kind,
            source_path=source_path,
            cwd=cwd,
            repo_name=derive_repo_name(cwd),
            git_branch=git_branch,
            title=title,
            first_user_prompt=first_prompt,
            last_user_prompt=last_prompt,
            created_at=created_at,
            updated_at=updated_at,
            turn_count=len(turns),
            archived=base_summary.archived if base_summary else False,
            warnings=list(warnings),
        )
        return SessionDetail(
            summary=summary,
            turns=turns,
            files_mentioned=files_mentioned,
            commands_run=commands_run,
            compaction_count=compaction_count,
            total_chars=total_chars,
            warnings=list(warnings),
        )

    def _resolve_fast_by_id(
        self,
        ref: str,
        warnings: list[str],
    ) -> tuple[SessionSummary | None, Path | None]:
        """Resolve an ID or unique prefix via state DB and filename UUIDs without full rollout scans."""
        ref_lower = ref.strip().lower()
        if not ref_lower:
            return None, None

        state_rows, state_db = self._read_state_threads(warnings)
        index_entries = self._read_session_index(warnings)
        rollout_files = self._discover_rollout_files(include_archived=True)

        disk_by_sid: dict[str, tuple[Path, bool]] = {}
        unindexed_candidates: list[tuple[Path, bool]] = []
        for file_path, is_arch in rollout_files:
            sid_hint = _extract_session_id_from_name(file_path)
            if sid_hint:
                disk_by_sid[sid_hint] = (file_path, is_arch)
                if sid_hint not in state_rows:
                    unindexed_candidates.append((file_path, is_arch))
            else:
                # Filename lacks a UUID; fall back to full inventory scan so session_meta.id is checked.
                return None, None

        seen_sids: dict[str, None] = {sid: None for sid in state_rows}
        for file_path, _ in unindexed_candidates:
            seen_sids.setdefault(_extract_session_id_from_name(file_path), None)

        matching_sids: list[str] = []
        for sid in seen_sids:
            if sid.lower() == ref_lower:
                matching_sids = [sid]
                break
            if sid.lower().startswith(ref_lower):
                matching_sids.append(sid)

        if len(matching_sids) > 1:
            raise ProviderReadError(f"Ambiguous Codex session prefix: {ref}")
        if not matching_sids:
            return None, None

        sid = matching_sids[0]
        if sid in state_rows:
            row = state_rows[sid]
            archived_flag = bool(row.get("archived"))
            rollout_file = _resolve_existing_path(str(row.get("rollout_path") or ""), self.root)
            if rollout_file is None and sid in disk_by_sid:
                rollout_file, disk_arch = disk_by_sid[sid]
                archived_flag = archived_flag or disk_arch
            summary = self._summary_from_state_row(
                sid,
                row,
                rollout_file=rollout_file,
                rollout_scan=None,
                hist_info=None,
                state_db=state_db,
                history_db=self._find_history_db(),
                index_entry=index_entries.get(sid, {}),
                archived_flag=archived_flag,
                secret_counter=[0],
                history_checked=False,
            )
            return summary, rollout_file

        file_path, is_arch = disk_by_sid[sid]
        scanned = self._scan_rollout_summary(file_path, warnings, archived=is_arch)
        title, updated_at = _merge_index_metadata(
            "",
            scanned["updated_at"],
            scanned["first_prompt"],
            index_entries.get(sid, {}),
            [0],
        )
        cwd = scanned["cwd"]
        summary = SessionSummary(
            provider=self.name,
            session_id=sid,
            source_kind="rollout_jsonl",
            source_path=scanned["source_path"],
            cwd=cwd,
            repo_name=derive_repo_name(cwd),
            git_branch=scanned["git_branch"],
            title=title,
            first_user_prompt=scanned["first_prompt"],
            last_user_prompt=scanned["last_prompt"],
            created_at=scanned["created_at"],
            updated_at=updated_at,
            turn_count=scanned["turn_count"],
            archived=is_arch,
            warnings=list(scanned.get("warnings", [])),
        )
        return summary, file_path

    def load_session(self, ref: str, *, max_tool_chars: int = 600) -> SessionDetail:
        target = Path(ref).expanduser()
        if target.is_file():
            warnings: list[str] = []
            sid_hint = _extract_session_id_from_name(target)
            state_rows, _ = self._read_state_threads(warnings)
            index_entries = self._read_session_index(warnings)
            base_summary: SessionSummary | None = None
            if sid_hint and sid_hint in state_rows:
                row = state_rows[sid_hint]
                base_summary = self._summary_from_state_row(
                    sid_hint,
                    row,
                    rollout_file=target,
                    rollout_scan=None,
                    hist_info=None,
                    state_db=self._find_state_db(),
                    history_db=self._find_history_db(),
                    index_entry=index_entries.get(sid_hint, {}),
                    archived_flag=bool(row.get("archived")),
                    secret_counter=[0],
                    history_checked=False,
                )
            elif sid_hint and sid_hint in index_entries:
                base_summary = self._empty_summary(target, source_kind="rollout_jsonl", warnings=[])
                base_summary.title = index_entries[sid_hint].get("title", "")

            detail, ok = self._parse_rollout_detail(target, base_summary, max_tool_chars=max_tool_chars)
            if base_summary is None and detail.summary.session_id in index_entries and not detail.summary.title:
                detail.summary.title = index_entries[detail.summary.session_id].get("title", "")
            if ok:
                return detail
            lookup_sid = detail.summary.session_id or sid_hint
            if lookup_sid:
                merged_warnings = list(detail.warnings)
                sql_detail = self._load_sqlite_history_detail(
                    lookup_sid,
                    base_summary or detail.summary,
                    max_tool_chars=max_tool_chars,
                    warnings=merged_warnings,
                )
                if sql_detail is not None:
                    return sql_detail
            return detail

        warnings = []
        summary, rollout_path = self._resolve_fast_by_id(ref, warnings)
        if summary is None:
            inventory = self._build_inventory(include_archived=True, warnings=warnings)
            ref_lower = ref.strip().lower()
            exact = None
            prefix_matches: list[dict[str, Any]] = []
            for sid, entry in inventory.items():
                if sid.lower() == ref_lower:
                    exact = entry
                    break
                if ref_lower and sid.lower().startswith(ref_lower):
                    prefix_matches.append(entry)
            chosen = exact
            if chosen is None:
                if len(prefix_matches) == 1:
                    chosen = prefix_matches[0]
                elif len(prefix_matches) > 1:
                    raise ProviderReadError(f"Ambiguous Codex session prefix: {ref}")
                else:
                    raise ProviderReadError(f"Codex session not found: {ref}")
            summary = chosen["summary"]
            rollout_path = chosen.get("rollout_path")

        merged_warnings = list(warnings)
        for w in summary.warnings:
            _add_unique(merged_warnings, w)

        if rollout_path is not None and rollout_path.is_file():
            detail, ok = self._parse_rollout_detail(rollout_path, summary, max_tool_chars=max_tool_chars)
            for w in merged_warnings:
                _add_unique(detail.warnings, w)
                _add_unique(detail.summary.warnings, w)
            if ok:
                return detail
            merged_warnings = list(detail.warnings)

        sql_detail = self._load_sqlite_history_detail(
            summary.session_id,
            summary,
            max_tool_chars=max_tool_chars,
            warnings=merged_warnings,
        )
        if sql_detail is not None:
            return sql_detail

        return self._load_metadata_only_detail(summary, warnings=merged_warnings)

    def extract_searchable_text(
        self, summary: SessionSummary, *, max_bytes: int = 262_144
    ) -> str:
        if max_bytes <= 0:
            return ""
        secret_counter = [0]
        chunks: list[str] = []
        used = 0

        def _append_chunk(raw: str) -> bool:
            nonlocal used
            cleaned = _clean_text_counted(raw, secret_counter, strip_tags=True)
            if not cleaned:
                return used < max_bytes
            remaining = max_bytes - used
            if remaining <= 0:
                return False
            piece = cleaned[:remaining]
            chunks.append(piece)
            used += len(piece) + 1
            return used < max_bytes

        if summary.source_kind == "rollout_jsonl" and summary.source_path:
            path = Path(summary.source_path)
            if path.is_file():
                if path.name.lower().endswith(".jsonl.zst"):
                    warnings: list[str] = []
                    lines = _iter_rollout_lines(path, warnings) or []
                    return self._extract_text_from_jsonl_lines(lines, max_bytes, secret_counter)
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        return self._extract_text_from_jsonl_lines(handle, max_bytes, secret_counter)
                except OSError:
                    return ""

        if summary.source_kind == "sqlite_history":
            history_db = self._find_history_db()
            if history_db is not None:
                try:
                    conn = open_sqlite_readonly(history_db)
                except ProviderReadError:
                    return ""
                try:
                    conn.row_factory = sqlite3.Row
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(thread_items)").fetchall()}
                    order_cols = [c for c in ("rollout_ordinal", "created_at_ms") if c in cols]
                    order_sql = f" ORDER BY {', '.join(order_cols)}" if order_cols else ""
                    for row in conn.execute(
                        f"SELECT item_type, item_json FROM thread_items WHERE thread_id = ?{order_sql}",
                        (summary.session_id,),
                    ):
                        try:
                            obj = json.loads(row["item_json"])
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if not isinstance(obj, dict):
                            continue
                        itype = str(row["item_type"] or obj.get("type") or "")
                        if itype == "userMessage":
                            keep = _append_chunk(_extract_message_text(obj.get("content")))
                        elif itype == "agentMessage":
                            keep = _append_chunk(str(obj.get("text") or ""))
                        else:
                            keep = True
                        if not keep:
                            break
                except sqlite3.Error:
                    pass
                finally:
                    conn.close()
                return "\n".join(chunks)

        for field_val in (summary.title, summary.first_user_prompt, summary.last_user_prompt):
            if field_val and not _append_chunk(field_val):
                break
        return "\n".join(chunks)

    def _extract_text_from_jsonl_lines(
        self,
        lines: Any,
        max_bytes: int,
        secret_counter: list[int],
    ) -> str:
        response_chunks: list[str] = []
        legacy_chunks: list[str] = []
        saw_response_msg = False
        bytes_seen = 0

        for line in lines:
            bytes_seen += len(line.encode("utf-8", errors="replace"))
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            rec_type = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if rec_type == "response_item" and payload.get("type") == "message":
                role = str(payload.get("role") or "").lower()
                if role in {"user", "assistant"}:
                    txt = _extract_message_text(
                        payload.get("content"),
                        payload.get("internal_chat_message_metadata_passthrough"),
                    )
                    cleaned = _clean_text_counted(txt, secret_counter, strip_tags=True)
                    if cleaned:
                        saw_response_msg = True
                        response_chunks.append(cleaned)
            elif rec_type == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
                raw = payload.get("message") or payload.get("text") or ""
                cleaned = _clean_text_counted(raw, secret_counter, strip_tags=True)
                if cleaned:
                    legacy_chunks.append(cleaned)
            if bytes_seen >= max_bytes:
                break

        selected = response_chunks if saw_response_msg else legacy_chunks
        return "\n".join(selected)[:max_bytes]
