"""Claude Code session provider supporting project JSONL DAG reconstruction and history enrichment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
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
    normalize_path,
)
from importer.safety import clean_text, truncate_text

_PROMPT_PREVIEW_LIMIT = 240
_TITLE_PREVIEW_LIMIT = 120
_CMD_PREVIEW_LIMIT = 240

_SKIP_RECORD_TYPES = frozenset(
    {
        "api-request",
        "api-request-shape",
        "api-request-blob",
        "atis-latch",
        "cost-state",
        "queue-operation",
        "file-history-snapshot",
        "file-history-delta",
        "tools-used",
        "mode",
        "progress",
        "attachment",
        "custom-title",
        "ai-title",
        "summary",
        "last-prompt",
        "content-replacement",
    }
)
_CLAUDE_SIGNATURE_TYPES = _SKIP_RECORD_TYPES - {"progress", "attachment"}
_SHELL_TOOL_NAMES = frozenset({"bash", "shell", "exec", "run_command", "terminal", "exec_command"})
_SUBAGENT_TOOL_NAMES = frozenset({"agent", "task", "dispatch_agent"})
_FILE_ARG_KEYS = ("file_path", "filepath", "notebook_path", "target_file", "path", "file")

_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (?:Update File|Add File|Delete File|Move to):\s*(.+?)\s*$",
    re.MULTILINE,
)
_EXIT_CODE_RE = re.compile(
    r"(?im)(?:^|\b)(?:exit[ _]?code|exited with code|return[ _]?code)\s*[:=]?\s*(-?[1-9]\d*)\b"
)
_TOOL_RESULTS_PATH_RE = re.compile(r"tool-results/[^\s\"'<>]+\.(?:txt|json|log)", re.IGNORECASE)
_PERSISTED_OUTPUT_RE = re.compile(r"<persisted-output\b", re.IGNORECASE)
_CLEARED_RESULT_MARKER = "[Old tool result content cleared]"
_EXTERNALIZED_STUB = "[externalized/persisted tool result omitted]"
_IMAGE_STUB = "[image omitted]"


def _add_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean_text_counted(
    value: Any,
    secret_counter: list[int],
    *,
    strip_tags: bool = True,
) -> str:
    cleaned, count = clean_text(value, strip_tags=strip_tags)
    secret_counter[0] += count
    return cleaned


def _preview_line(value: Any, limit: int, secret_counter: list[int]) -> str:
    cleaned = _clean_text_counted(value, secret_counter, strip_tags=True)
    text, _ = truncate_text(cleaned, limit, single_line=True)
    return text


def _decode_project_slug(slug: str) -> str:
    """Best-effort lossy decode of a Claude project folder slug (e.g. '-Users-mac-repo')."""
    if not slug or not slug.startswith("-"):
        return ""
    parts = slug[1:].split("-")
    if not parts:
        return ""
    current = Path("/")
    idx = 0
    while idx < len(parts):
        matched_end: int | None = None
        for end in range(len(parts), idx, -1):
            candidate = "-".join(parts[idx:end])
            if not candidate:
                continue
            try:
                if (current / candidate).exists():
                    matched_end = end
                    current = current / candidate
                    idx = end
                    break
            except OSError:
                break
        if matched_end is None:
            if parts[idx]:
                current = current / parts[idx]
            idx += 1
    return str(current)


def _get_message_dict(record: dict[str, Any]) -> dict[str, Any]:
    msg = record.get("message")
    return msg if isinstance(msg, dict) else {}


def _get_message_content(record: dict[str, Any]) -> Any:
    msg = _get_message_dict(record)
    return msg.get("content") if msg else record.get("content")


def _get_message_id(record: dict[str, Any]) -> str:
    return _clean_str(_get_message_dict(record).get("id"))


def _raw_claude_parent(record: dict[str, Any]) -> str:
    parent = _clean_str(record.get("parentUuid"))
    if parent:
        return parent
    return _clean_str(record.get("logicalParentUuid"))


def _is_media_block(block: dict[str, Any]) -> bool:
    btype = str(block.get("type") or "").lower()
    return (
        "image" in btype
        or "image_url" in block
        or btype in {"document", "binary"}
        or (isinstance(block.get("source"), dict) and "text" not in block)
    )


def _extract_content_blocks(
    content: Any,
    *,
    collect_tool_uses: bool = False,
    collect_tool_results: bool = False,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract joined text/image stubs plus optional tool_use and tool_result blocks."""
    if isinstance(content, str):
        return content, [], []
    if not isinstance(content, list):
        return "", [], []

    text_parts: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            if block:
                text_parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").lower()
        if btype in {"thinking", "redacted_thinking"}:
            continue
        if btype == "tool_use":
            if collect_tool_uses:
                tool_uses.append(block)
            continue
        if btype == "tool_result":
            if collect_tool_results:
                tool_results.append(block)
            continue
        if _is_media_block(block):
            text_parts.append(_IMAGE_STUB)
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            text_parts.append(text)
    return "\n".join(text_parts), tool_uses, tool_results


def _is_filtered_record(record: dict[str, Any], snipped_uuids: set[str] | None = None) -> bool:
    rec_type = str(record.get("type") or "")
    if rec_type in _SKIP_RECORD_TYPES or rec_type == "system":
        return True
    if (
        record.get("isSidechain") is True
        or record.get("isMeta") is True
        or record.get("isVirtual") is True
        or record.get("isVisibleInTranscriptOnly") is True
        or record.get("isCompactSummary") is True
    ):
        return True
    if snipped_uuids:
        uid = _clean_str(record.get("uuid"))
        if uid and uid in snipped_uuids:
            return True
    return False


def _is_tool_result_only_user(record: dict[str, Any]) -> bool:
    if record.get("type") != "user" or _is_filtered_record(record):
        return False
    raw_text, _, tool_results = _extract_content_blocks(
        _get_message_content(record), collect_tool_results=True
    )
    if not tool_results:
        return False
    cleaned, _ = clean_text(raw_text, strip_tags=True)
    return not cleaned


def _extract_files_from_input(tool_input: Any) -> list[str]:
    files: list[str] = []
    if isinstance(tool_input, dict):
        for key in _FILE_ARG_KEYS:
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                _add_unique(files, val.strip())
        paths_val = tool_input.get("paths")
        if isinstance(paths_val, list):
            for entry in paths_val:
                if isinstance(entry, str) and entry.strip():
                    _add_unique(files, entry.strip())
        for patch_key in ("patch", "diff", "input"):
            patch_val = tool_input.get(patch_key)
            if isinstance(patch_val, str) and "*** " in patch_val:
                for match in _PATCH_FILE_RE.findall(patch_val):
                    cleaned = match.strip()
                    if cleaned and cleaned != "/dev/null":
                        _add_unique(files, cleaned)
    elif isinstance(tool_input, str) and "*** " in tool_input:
        for match in _PATCH_FILE_RE.findall(tool_input):
            cleaned = match.strip()
            if cleaned and cleaned != "/dev/null":
                _add_unique(files, cleaned)
    return files


def _extract_command(tool_name: str, tool_input: Any) -> str:
    base = tool_name.rsplit(".", 1)[-1].rsplit(":", 1)[-1].strip().lower()
    if base not in _SHELL_TOOL_NAMES:
        return ""
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    elif isinstance(tool_input, str) and tool_input.strip():
        return tool_input.strip()
    return ""


def _tool_result_content_to_text(content: Any, tool_use_result: Any) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append(item)
            elif isinstance(item, dict):
                if _is_media_block(item):
                    parts.append(_IMAGE_STUB)
                elif isinstance(item.get("text"), str) and item["text"]:
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str) and item["content"]:
                    parts.append(item["content"])
        text = "\n".join(parts)
    elif isinstance(content, dict):
        text = str(
            content.get("text")
            or content.get("output")
            or content.get("stdout")
            or content.get("stderr")
            or content.get("error")
            or json.dumps(content, ensure_ascii=False)
        )
    else:
        text = ""

    if text:
        return text
    if isinstance(tool_use_result, str):
        return tool_use_result
    if isinstance(tool_use_result, dict):
        pieces: list[str] = []
        for key in ("stdout", "stderr", "content", "output", "error", "message", "filePath"):
            val = tool_use_result.get(key)
            if isinstance(val, str) and val:
                pieces.append(val)
        return "\n".join(pieces) if pieces else json.dumps(tool_use_result, ensure_ascii=False)
    return ""


def _is_externalized_tool_result(
    tool_use_id: str,
    raw_text: str,
    tool_use_result: Any,
    replaced_ids: set[str],
) -> bool:
    if tool_use_id and tool_use_id in replaced_ids:
        return True
    candidates: list[str] = [raw_text] if raw_text else []
    if isinstance(tool_use_result, str) and tool_use_result:
        candidates.append(tool_use_result)
    elif isinstance(tool_use_result, dict):
        for key in (
            "persistedOutputPath",
            "persisted_output_path",
            "outputFile",
            "filePath",
            "content",
            "stdout",
            "stderr",
        ):
            val = tool_use_result.get(key)
            if isinstance(val, str) and val:
                candidates.append(val)
    for item in candidates:
        if (
            _PERSISTED_OUTPUT_RE.search(item)
            or _CLEARED_RESULT_MARKER in item
            or _TOOL_RESULTS_PATH_RE.search(item)
        ):
            return True
    return False


def _resolve_tool_result_status(
    block: dict[str, Any],
    tool_use_result: Any,
    raw_text: str,
    tool_name: str,
) -> str:
    if isinstance(tool_use_result, dict) and tool_use_result.get("interrupted") is True:
        return "aborted"
    if block.get("is_error") is True:
        return "error"
    if isinstance(tool_use_result, dict):
        if tool_use_result.get("is_error") is True or tool_use_result.get("error"):
            return "error"
    base_name = tool_name.rsplit(".", 1)[-1].rsplit(":", 1)[-1].strip().lower()
    if base_name in _SHELL_TOOL_NAMES and _EXIT_CODE_RE.search(raw_text):
        return "error"
    return "ok"


def _collect_replaced_ids(record: dict[str, Any], replaced_ids: set[str]) -> None:
    for key in ("toolUseId", "tool_use_id"):
        direct = _clean_str(record.get(key))
        if direct:
            replaced_ids.add(direct)
    replacements = record.get("replacements")
    if isinstance(replacements, list):
        for item in replacements:
            if isinstance(item, dict):
                for key in ("toolUseId", "tool_use_id"):
                    tid = _clean_str(item.get(key))
                    if tid:
                        replaced_ids.add(tid)


def _resolve_title(
    *,
    custom_title: str,
    ai_title: str,
    summaries: list[tuple[str, str]],
    active_leaf_uuid: str,
    first_prompt: str,
    history_display: str,
    secret_counter: list[int],
) -> str:
    if custom_title:
        cleaned = _preview_line(custom_title, _TITLE_PREVIEW_LIMIT, secret_counter)
        if cleaned:
            return cleaned
    if ai_title:
        cleaned = _preview_line(ai_title, _TITLE_PREVIEW_LIMIT, secret_counter)
        if cleaned:
            return cleaned
    if summaries:
        chosen = ""
        if active_leaf_uuid:
            for text, leaf in reversed(summaries):
                if leaf == active_leaf_uuid and text.strip():
                    chosen = text
                    break
        if not chosen:
            for text, _ in reversed(summaries):
                if text.strip():
                    chosen = text
                    break
        if chosen:
            cleaned = _preview_line(chosen, _TITLE_PREVIEW_LIMIT, secret_counter)
            if cleaned:
                return cleaned
    if first_prompt:
        return truncate_text(first_prompt, _TITLE_PREVIEW_LIMIT, single_line=True)[0]
    if history_display:
        return _preview_line(history_display, _TITLE_PREVIEW_LIMIT, secret_counter)
    return ""


def _drop_answered_tool_result_blocks(
    content: list[Any], answered_tool_use_ids: set[str]
) -> list[Any]:
    return [
        block
        for block in content
        if not (
            isinstance(block, dict)
            and str(block.get("type") or "").lower() == "tool_result"
            and _clean_str(block.get("tool_use_id")) in answered_tool_use_ids
        )
    ]


def _filter_sibling_tool_results(
    record: dict[str, Any],
    answered_tool_use_ids: set[str],
) -> dict[str, Any]:
    """Return a copy of a sibling record excluding tool_results already answered on chain."""
    if not answered_tool_use_ids:
        return record
    msg = record.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        filtered = _drop_answered_tool_result_blocks(msg["content"], answered_tool_use_ids)
        if len(filtered) == len(msg["content"]):
            return record
        cloned_msg = dict(msg)
        cloned_msg["content"] = filtered
        cloned_rec = dict(record)
        cloned_rec["message"] = cloned_msg
        return cloned_rec
    content = record.get("content")
    if isinstance(content, list):
        filtered = _drop_answered_tool_result_blocks(content, answered_tool_use_ids)
        if len(filtered) == len(content):
            return record
        cloned_rec = dict(record)
        cloned_rec["content"] = filtered
        return cloned_rec
    return record


def _update_ts_bounds(
    ts_raw: Any,
    created_at: str,
    created_ms: int,
    updated_at: str,
    updated_ms: int,
) -> tuple[str, int, str, int]:
    ts_iso = normalize_timestamp(ts_raw)
    if not ts_iso:
        return created_at, created_ms, updated_at, updated_ms
    ts_ms = timestamp_to_epoch_ms(ts_raw)
    if not created_at or ts_ms < created_ms:
        created_at = ts_iso
        created_ms = ts_ms
    if not updated_at or ts_ms >= updated_ms:
        updated_at = ts_iso
        updated_ms = ts_ms
    return created_at, created_ms, updated_at, updated_ms


class ClaudeProvider(SessionProvider):
    """Read-only provider for Claude Code project JSONL transcripts and history.jsonl."""

    name = "claude"
    display_name = "Claude Code"
    aliases = ("claude-code", "cc")

    def __init__(self, root: Path | None = None) -> None:
        if root is not None:
            self.root = Path(root).expanduser()
        else:
            env_root = (
                os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
                or os.environ.get("CLAUDE_HOME", "").strip()
            )
            self.root = Path(env_root).expanduser() if env_root else Path.home() / ".claude"

    def is_available(self) -> bool:
        try:
            if not self.root.is_dir():
                return False
            return (self.root / "projects").is_dir() or (self.root / "history.jsonl").is_file()
        except OSError:
            return False

    def can_handle_path(self, path: Path) -> bool:
        target = Path(path)
        if not target.name.lower().endswith(".jsonl"):
            return False
        if target.name in {"history.jsonl", "session_index.jsonl"}:
            return False
        try:
            if target.resolve().is_relative_to((self.root / "projects").resolve()):
                return True
        except OSError:
            pass
        try:
            if not target.is_file():
                return False
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
                        if checked >= 15:
                            break
                        continue
                    if isinstance(obj, dict):
                        rtype = str(obj.get("type") or "")
                        if rtype in {"session_meta", "turn_context", "response_item", "event_msg", "compacted"}:
                            return False
                        if rtype in _CLAUDE_SIGNATURE_TYPES:
                            return True
                        if rtype in {"user", "assistant", "system"} and (
                            "uuid" in obj
                            or "parentUuid" in obj
                            or "logicalParentUuid" in obj
                            or "sessionId" in obj
                            or isinstance(obj.get("message"), dict)
                        ):
                            return True
                    if checked >= 15:
                        break
        except OSError:
            return False
        return False

    def _discover_project_files(self) -> list[Path]:
        projects_dir = self.root / "projects"
        if not projects_dir.is_dir():
            return []
        found: list[Path] = []
        try:
            project_dirs = sorted(projects_dir.iterdir())
        except OSError:
            return []
        for proj_dir in project_dirs:
            try:
                if not proj_dir.is_dir() or proj_dir.name == "subagents":
                    continue
                for entry in sorted(proj_dir.iterdir()):
                    if entry.is_file() and entry.name.lower().endswith(".jsonl"):
                        found.append(entry)
            except OSError:
                continue
        return found

    def _read_history_index(self, warnings: list[str] | None = None) -> dict[str, dict[str, Any]]:
        history_path = self.root / "history.jsonl"
        entries: dict[str, dict[str, Any]] = {}
        if not history_path.is_file():
            return entries

        malformed = 0
        try:
            with history_path.open("r", encoding="utf-8", errors="replace") as handle:
                for idx, line in enumerate(handle):
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
                    sid = _clean_str(obj.get("sessionId"))
                    if not sid:
                        continue
                    ts_iso = normalize_timestamp(obj.get("timestamp"))
                    ts_ms = timestamp_to_epoch_ms(obj.get("timestamp"))
                    display = obj.get("display")
                    display_str = display.strip() if isinstance(display, str) else ""
                    project = _clean_str(obj.get("project"))

                    slot = entries.setdefault(
                        sid,
                        {
                            "first_display": "",
                            "last_display": "",
                            "first_ts": "",
                            "last_ts": "",
                            "first_key": None,
                            "last_key": None,
                            "project": "",
                        },
                    )
                    if project:
                        slot["project"] = project
                    key = (ts_ms, idx)
                    if display_str:
                        if slot["first_key"] is None or key < slot["first_key"]:
                            slot["first_key"] = key
                            slot["first_display"] = display_str
                        if slot["last_key"] is None or key >= slot["last_key"]:
                            slot["last_key"] = key
                            slot["last_display"] = display_str
                    if ts_iso:
                        if not slot["first_ts"] or ts_ms < timestamp_to_epoch_ms(slot["first_ts"]):
                            slot["first_ts"] = ts_iso
                        if not slot["last_ts"] or ts_ms >= timestamp_to_epoch_ms(slot["last_ts"]):
                            slot["last_ts"] = ts_iso
        except OSError as exc:
            if warnings is not None:
                _add_unique(warnings, f"claude_history_read_error:{exc}")
            return entries

        if malformed and warnings is not None:
            _add_unique(warnings, f"claude_malformed_history_lines:{malformed}")
        return entries

    def _resolve_cwd(self, path: Path, raw_cwd: str, hist: dict[str, Any]) -> str:
        cwd = raw_cwd or hist.get("project", "")
        if not cwd:
            try:
                if path.parent.parent == self.root / "projects":
                    cwd = _decode_project_slug(path.parent.name)
            except OSError:
                pass
        return normalize_path(cwd)

    def _build_session_summary(
        self,
        *,
        path: Path,
        session_id: str,
        raw_cwd: str,
        git_branch: str,
        custom_title: str,
        ai_title: str,
        summaries: list[tuple[str, str]],
        active_leaf_uuid: str,
        last_prompt_text: str,
        first_prompt: str,
        last_prompt: str,
        created_at: str,
        updated_at: str,
        turn_count: int,
        hist: dict[str, Any],
        warnings: list[str],
        secret_counter: list[int],
    ) -> SessionSummary:
        norm_cwd = self._resolve_cwd(path, raw_cwd, hist)
        resolved_first = first_prompt or _preview_line(
            hist.get("first_display", ""), _PROMPT_PREVIEW_LIMIT, secret_counter
        )
        if last_prompt:
            resolved_last = last_prompt
        else:
            resolved_last = _preview_line(last_prompt_text, _PROMPT_PREVIEW_LIMIT, secret_counter)
            if not resolved_last:
                raw_hist_last = hist.get("last_display", "")
                if raw_hist_last and raw_hist_last == hist.get("first_display", "") and not first_prompt:
                    resolved_last = resolved_first
                else:
                    resolved_last = (
                        _preview_line(raw_hist_last, _PROMPT_PREVIEW_LIMIT, secret_counter)
                        or resolved_first
                    )
        resolved_created = created_at or hist.get("first_ts", "")
        resolved_updated = updated_at or hist.get("last_ts", "")
        title = _resolve_title(
            custom_title=custom_title,
            ai_title=ai_title,
            summaries=summaries,
            active_leaf_uuid=active_leaf_uuid,
            first_prompt=resolved_first,
            history_display="",
            secret_counter=secret_counter,
        )
        return SessionSummary(
            provider=self.name,
            session_id=session_id,
            source_kind="project_jsonl",
            source_path=str(path.resolve()),
            cwd=norm_cwd,
            repo_name=derive_repo_name(norm_cwd),
            git_branch=git_branch,
            title=title,
            first_user_prompt=resolved_first,
            last_user_prompt=resolved_last,
            created_at=resolved_created,
            updated_at=resolved_updated,
            turn_count=turn_count,
            archived=False,
            warnings=list(warnings),
        )

    def _scan_session_summary(
        self,
        path: Path,
        history_by_sid: dict[str, dict[str, Any]],
        warnings: list[str] | None,
        secret_counter: list[int],
    ) -> SessionSummary:
        file_warnings: list[str] = []
        sid = ""
        cwd = ""
        git_branch = ""
        custom_title = ""
        ai_title = ""
        summaries: list[tuple[str, str]] = []
        last_prompt_text = ""
        last_prompt_leaf = ""
        latest_ua_uuid = ""
        first_prompt = ""
        last_prompt = ""
        turn_count = 0
        created_at = ""
        updated_at = ""
        created_ms = 0
        updated_ms = 0
        malformed = 0
        sidechain_skipped = 0

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
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

                    rec_sid = _clean_str(obj.get("sessionId"))
                    if rec_sid and not sid:
                        sid = rec_sid

                    rec_type = str(obj.get("type") or "")
                    if rec_type == "custom-title":
                        val = _clean_str(obj.get("customTitle"))
                        if val:
                            custom_title = val
                        continue
                    if rec_type == "ai-title":
                        val = _clean_str(obj.get("aiTitle"))
                        if val:
                            ai_title = val
                        continue
                    if rec_type == "summary":
                        stext = obj.get("summary")
                        if isinstance(stext, str) and stext.strip():
                            summaries.append((stext.strip(), _clean_str(obj.get("leafUuid"))))
                        continue
                    if rec_type == "last-prompt":
                        if isinstance(obj.get("lastPrompt"), str):
                            last_prompt_text = obj["lastPrompt"]
                        leaf = _clean_str(obj.get("leafUuid"))
                        if leaf:
                            last_prompt_leaf = leaf
                        continue

                    if obj.get("isSidechain") is True:
                        sidechain_skipped += 1
                        continue

                    rec_cwd = _clean_str(obj.get("cwd"))
                    if rec_cwd:
                        cwd = rec_cwd
                    rec_branch = _clean_str(obj.get("gitBranch"))
                    if rec_branch:
                        git_branch = rec_branch

                    created_at, created_ms, updated_at, updated_ms = _update_ts_bounds(
                        obj.get("timestamp"), created_at, created_ms, updated_at, updated_ms
                    )

                    if rec_type in {"user", "assistant"}:
                        uid = _clean_str(obj.get("uuid"))
                        if uid:
                            latest_ua_uuid = uid

                    if rec_type == "user" and not _is_filtered_record(obj):
                        raw_text, _, _ = _extract_content_blocks(_get_message_content(obj))
                        prev = _preview_line(raw_text, _PROMPT_PREVIEW_LIMIT, secret_counter)
                        if prev:
                            turn_count += 1
                            if not first_prompt:
                                first_prompt = prev
                            last_prompt = prev
        except OSError as exc:
            msg = f"claude_session_read_error:{path}:{exc}"
            _add_unique(file_warnings, msg)
            if warnings is not None:
                _add_unique(warnings, msg)

        if malformed:
            msg = f"claude_malformed_jsonl_lines:{malformed}"
            _add_unique(file_warnings, msg)
            if warnings is not None:
                _add_unique(warnings, msg)
        if sidechain_skipped:
            msg = f"claude_sidechain_skipped:{sidechain_skipped}"
            _add_unique(file_warnings, msg)
            if warnings is not None:
                _add_unique(warnings, msg)

        resolved_sid = sid or path.stem
        scan_last = _preview_line(last_prompt_text, _PROMPT_PREVIEW_LIMIT, secret_counter) or last_prompt
        return self._build_session_summary(
            path=path,
            session_id=resolved_sid,
            raw_cwd=cwd,
            git_branch=git_branch,
            custom_title=custom_title,
            ai_title=ai_title,
            summaries=summaries,
            active_leaf_uuid=last_prompt_leaf or latest_ua_uuid,
            last_prompt_text=last_prompt_text,
            first_prompt=first_prompt,
            last_prompt=scan_last,
            created_at=created_at,
            updated_at=updated_at,
            turn_count=turn_count,
            hist=history_by_sid.get(resolved_sid, {}),
            warnings=file_warnings,
            secret_counter=secret_counter,
        )

    def list_sessions(
        self, *, include_archived: bool = False
    ) -> tuple[list[SessionSummary], list[str]]:
        _ = include_archived
        warnings: list[str] = []
        history_by_sid = self._read_history_index(warnings)
        secret_counter = [0]
        summaries: list[SessionSummary] = []
        for path in self._discover_project_files():
            summaries.append(self._scan_session_summary(path, history_by_sid, warnings, secret_counter))
        summaries.sort(
            key=lambda s: (timestamp_to_epoch_ms(s.updated_at), timestamp_to_epoch_ms(s.created_at), s.session_id),
            reverse=True,
        )
        return summaries, warnings

    def _resolve_session_path(self, ref: str) -> Path:
        ref_clean = ref.strip()
        if not ref_clean:
            raise ProviderReadError("Empty Claude session reference")
        ref_lower = ref_clean.lower()
        candidates = self._discover_project_files()

        # Fast-path: match filename stems first so unrelated corrupt files are not read.
        exact_stem = [p for p in candidates if p.stem.lower() == ref_lower]
        if len(exact_stem) == 1:
            return exact_stem[0]
        prefix_stems = [p for p in candidates if p.stem.lower().startswith(ref_lower)]
        if len(prefix_stems) == 1:
            return prefix_stems[0]
        if len(prefix_stems) > 1:
            raise ProviderReadError(f"Ambiguous Claude session prefix: {ref}")

        # Slow-path: scan files silently (no cross-session warning bleed) for internal sessionId.
        history_by_sid: dict[str, dict[str, Any]] = {}
        secret_counter = [0]
        exact_matches: list[Path] = []
        prefix_matches: list[Path] = []
        for path in candidates:
            summary = self._scan_session_summary(path, history_by_sid, None, secret_counter)
            sid_lower = summary.session_id.lower()
            if sid_lower == ref_lower:
                exact_matches.append(path)
            elif sid_lower.startswith(ref_lower):
                prefix_matches.append(path)

        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1 or len(prefix_matches) > 1:
            raise ProviderReadError(f"Ambiguous Claude session prefix: {ref}")
        if len(prefix_matches) == 1:
            return prefix_matches[0]
        raise ProviderReadError(f"Claude session not found: {ref}")

    def _reconstruct_active_branch(
        self,
        all_records: list[tuple[int, dict[str, Any]]],
        last_prompt_leaf_uuid: str,
        warnings: list[str],
    ) -> tuple[list[dict[str, Any]], set[str], str]:
        non_sidechain: list[tuple[int, dict[str, Any]]] = []
        nodes_by_uuid: dict[str, tuple[int, dict[str, Any]]] = {}
        snipped_uuids: set[str] = set()
        snip_bridges: dict[str, str] = {}
        preserved_links: list[tuple[int, str, str, str, str]] = []
        sidechain_skipped = 0

        for file_idx, rec in all_records:
            if rec.get("isSidechain") is True:
                sidechain_skipped += 1
                continue
            non_sidechain.append((file_idx, rec))
            uid = _clean_str(rec.get("uuid"))
            if uid:
                nodes_by_uuid[uid] = (file_idx, rec)

            snip_meta = rec.get("snipMetadata") if isinstance(rec.get("snipMetadata"), dict) else rec.get("snip_metadata")
            if isinstance(snip_meta, dict):
                removed = snip_meta.get("removedUuids") or snip_meta.get("removed_uuids")
                anchor = _clean_str(snip_meta.get("anchorUuid") or snip_meta.get("anchor_uuid")) or uid or _raw_claude_parent(rec)
                if isinstance(removed, list):
                    for rem in removed:
                        rem_id = _clean_str(rem)
                        if rem_id:
                            snipped_uuids.add(rem_id)
                            if anchor and anchor != rem_id:
                                snip_bridges[rem_id] = anchor

            compact_meta = (
                rec.get("compactMetadata")
                if isinstance(rec.get("compactMetadata"), dict)
                else rec.get("compact_metadata")
            )
            if isinstance(compact_meta, dict):
                pres = (
                    compact_meta.get("preservedSegment")
                    if isinstance(compact_meta.get("preservedSegment"), dict)
                    else compact_meta.get("preserved_segment")
                )
                if isinstance(pres, dict):
                    head_u = _clean_str(pres.get("headUuid") or pres.get("head_uuid"))
                    anchor_u = _clean_str(pres.get("anchorUuid") or pres.get("anchor_uuid"))
                    tail_u = _clean_str(pres.get("tailUuid") or pres.get("tail_uuid"))
                    if uid:
                        preserved_links.append((file_idx, uid, head_u, anchor_u, tail_u))

        preserved_overrides: dict[str, str] = {}
        for file_idx, uid, head_u, anchor_u, tail_u in preserved_links:
            _, boundary_rec = nodes_by_uuid[uid]
            if not _raw_claude_parent(boundary_rec):
                if anchor_u and anchor_u != uid and anchor_u in nodes_by_uuid:
                    preserved_overrides.setdefault(uid, anchor_u)
                elif tail_u and tail_u != uid and tail_u in nodes_by_uuid and nodes_by_uuid[tail_u][0] < file_idx:
                    preserved_overrides.setdefault(uid, tail_u)
            if head_u and head_u != uid and head_u in nodes_by_uuid:
                if nodes_by_uuid[head_u][0] > file_idx:
                    preserved_overrides.setdefault(head_u, uid)
                elif (
                    anchor_u
                    and anchor_u != uid
                    and anchor_u != head_u
                    and anchor_u in nodes_by_uuid
                    and nodes_by_uuid[anchor_u][0] < nodes_by_uuid[head_u][0]
                ):
                    preserved_overrides.setdefault(head_u, anchor_u)

        if sidechain_skipped:
            _add_unique(warnings, f"claude_sidechain_skipped:{sidechain_skipped}")

        def _resolve_parent(uid: str, rec: dict[str, Any]) -> str:
            raw_parent = _raw_claude_parent(rec)
            if raw_parent:
                if raw_parent in nodes_by_uuid:
                    return raw_parent
                if uid in preserved_overrides and preserved_overrides[uid] in nodes_by_uuid:
                    return preserved_overrides[uid]
                return raw_parent
            return preserved_overrides.get(uid, "")

        def _next_surviving_parent(uid: str, rec: dict[str, Any], visited_walk: set[str]) -> str:
            parent = _resolve_parent(uid, rec)
            seen_bridges: set[str] = set()
            while parent and parent not in nodes_by_uuid and parent in snip_bridges and parent not in seen_bridges:
                seen_bridges.add(parent)
                parent = snip_bridges[parent]
                if parent in visited_walk:
                    return parent
            return parent

        def _resolves_to_ua(start_uuid: str) -> bool:
            cur = start_uuid
            seen: set[str] = set()
            while cur and cur in nodes_by_uuid and cur not in seen:
                seen.add(cur)
                _, rec = nodes_by_uuid[cur]
                if rec.get("type") in {"user", "assistant"}:
                    return True
                cur = _next_surviving_parent(cur, rec, seen)
            return False

        ua_uuids = [
            uid for uid, (_, rec) in nodes_by_uuid.items() if rec.get("type") in {"user", "assistant"}
        ]
        if not ua_uuids:
            if any(rec.get("type") in {"user", "assistant"} for _, rec in non_sidechain):
                _add_unique(warnings, "linear_fallback_used")
            return [rec for _, rec in non_sidechain], snipped_uuids, ""

        if last_prompt_leaf_uuid and last_prompt_leaf_uuid in nodes_by_uuid and _resolves_to_ua(last_prompt_leaf_uuid):
            leaf_uuid = last_prompt_leaf_uuid
        else:
            strict_ua_ancestors: set[str] = set()
            for uid in ua_uuids:
                seen: set[str] = {uid}
                _, rec = nodes_by_uuid[uid]
                cur = _next_surviving_parent(uid, rec, seen)
                while cur and cur in nodes_by_uuid and cur not in seen:
                    strict_ua_ancestors.add(cur)
                    seen.add(cur)
                    _, p_rec = nodes_by_uuid[cur]
                    cur = _next_surviving_parent(cur, p_rec, seen)
            leaf_candidates = [uid for uid in ua_uuids if uid not in strict_ua_ancestors] or ua_uuids
            leaf_uuid = max(
                leaf_candidates,
                key=lambda uid: (
                    timestamp_to_epoch_ms(nodes_by_uuid[uid][1].get("timestamp")),
                    nodes_by_uuid[uid][0],
                ),
            )

        visited: set[str] = set()
        chain_reversed: list[tuple[int, dict[str, Any]]] = []
        cur = leaf_uuid
        fallback_needed = False

        while cur:
            if cur in visited or cur not in nodes_by_uuid:
                fallback_needed = True
                break
            visited.add(cur)
            file_idx, rec = nodes_by_uuid[cur]
            chain_reversed.append((file_idx, rec))
            parent = _next_surviving_parent(cur, rec, visited)
            if not parent:
                break
            if parent not in nodes_by_uuid:
                if parent in snipped_uuids:
                    _add_unique(warnings, "compaction_gap")
                    break
                fallback_needed = True
                break
            cur = parent

        if fallback_needed:
            _add_unique(warnings, "linear_fallback_used")
            return [rec for _, rec in non_sidechain], snipped_uuids, leaf_uuid

        chain = list(reversed(chain_reversed))
        return self._recover_parallel_siblings(chain, visited, non_sidechain), snipped_uuids, leaf_uuid

    def _recover_parallel_siblings(
        self,
        chain: list[tuple[int, dict[str, Any]]],
        chain_uuids: set[str],
        non_sidechain: list[tuple[int, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        chain_msg_ids = {
            mid
            for _, rec in chain
            if rec.get("type") == "assistant"
            for mid in (_get_message_id(rec),)
            if mid
        }

        recovered_assistants: list[tuple[int, dict[str, Any]]] = []
        assistant_uuids: set[str] = {
            _clean_str(rec.get("uuid")) for _, rec in chain if rec.get("type") == "assistant"
        }
        for file_idx, rec in non_sidechain:
            if rec.get("type") != "assistant":
                continue
            uid = _clean_str(rec.get("uuid"))
            if uid in chain_uuids:
                continue
            mid = _get_message_id(rec)
            if mid and mid in chain_msg_ids:
                recovered_assistants.append((file_idx, rec))
                if uid:
                    assistant_uuids.add(uid)

        chain_tool_use_ids: set[str] = set()
        for _, rec in [*chain, *recovered_assistants]:
            if rec.get("type") != "assistant":
                continue
            _, tool_uses, _ = _extract_content_blocks(
                _get_message_content(rec), collect_tool_uses=True
            )
            for block in tool_uses:
                tid = _clean_str(block.get("id"))
                if tid:
                    chain_tool_use_ids.add(tid)

        answered_tool_use_ids: set[str] = set()
        for _, rec in chain:
            if rec.get("type") != "user" or _is_filtered_record(rec):
                continue
            _, _, tr_blocks = _extract_content_blocks(
                _get_message_content(rec), collect_tool_results=True
            )
            for block in tr_blocks:
                tid = _clean_str(block.get("tool_use_id"))
                if tid:
                    answered_tool_use_ids.add(tid)

        missing_tool_use_ids = chain_tool_use_ids - answered_tool_use_ids

        recovered_tool_results: list[tuple[int, dict[str, Any]]] = []
        for file_idx, rec in non_sidechain:
            uid = _clean_str(rec.get("uuid"))
            if uid in chain_uuids or not _is_tool_result_only_user(rec):
                continue
            _, _, tr_blocks = _extract_content_blocks(
                _get_message_content(rec), collect_tool_results=True
            )
            tr_ids = {_clean_str(b.get("tool_use_id")) for b in tr_blocks} - {""}
            if not (tr_ids & missing_tool_use_ids):
                continue
            recovered_tool_results.append(
                (file_idx, _filter_sibling_tool_results(rec, answered_tool_use_ids))
            )

        if not recovered_assistants and not recovered_tool_results:
            return [rec for _, rec in chain]

        if all(chain[i][0] < chain[i + 1][0] for i in range(len(chain) - 1)):
            merged = sorted([*chain, *recovered_assistants, *recovered_tool_results], key=lambda item: item[0])
            return [rec for _, rec in merged]

        slots: list[list[tuple[int, dict[str, Any]]]] = [[entry] for entry in chain]
        msg_id_to_slot: dict[str, int] = {}
        uuid_to_slot: dict[str, int] = {}
        for idx, (_, rec) in enumerate(chain):
            uid = _clean_str(rec.get("uuid"))
            if uid:
                uuid_to_slot[uid] = idx
            if rec.get("type") == "assistant":
                mid = _get_message_id(rec)
                if mid and mid not in msg_id_to_slot:
                    msg_id_to_slot[mid] = idx
        for entry in recovered_assistants:
            mid = _get_message_id(entry[1])
            slot_idx = msg_id_to_slot.get(mid, len(slots) - 1)
            slots[slot_idx].append(entry)
            uid = _clean_str(entry[1].get("uuid"))
            if uid:
                uuid_to_slot[uid] = slot_idx
        for entry in recovered_tool_results:
            parent = _raw_claude_parent(entry[1])
            slot_idx = uuid_to_slot.get(parent, len(slots) - 1)
            slots[slot_idx].append(entry)

        ordered: list[dict[str, Any]] = []
        for slot in slots:
            slot.sort(key=lambda item: item[0])
            ordered.extend(rec for _, rec in slot)
        return ordered

    def _read_session_file(
        self, path: Path, warnings: list[str]
    ) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, Any]]:
        all_records: list[tuple[int, dict[str, Any]]] = []
        meta: dict[str, Any] = {
            "sid": "",
            "cwd": "",
            "git_branch": "",
            "custom_title": "",
            "ai_title": "",
            "summaries": [],
            "last_prompt_text": "",
            "last_prompt_leaf": "",
            "replaced_ids": set(),
        }
        malformed = 0

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_idx, line in enumerate(handle):
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

                    all_records.append((line_idx, obj))
                    rec_sid = _clean_str(obj.get("sessionId"))
                    if rec_sid and not meta["sid"]:
                        meta["sid"] = rec_sid

                    rec_type = str(obj.get("type") or "")
                    if rec_type == "custom-title":
                        val = _clean_str(obj.get("customTitle"))
                        if val:
                            meta["custom_title"] = val
                    elif rec_type == "ai-title":
                        val = _clean_str(obj.get("aiTitle"))
                        if val:
                            meta["ai_title"] = val
                    elif rec_type == "summary":
                        stext = obj.get("summary")
                        if isinstance(stext, str) and stext.strip():
                            meta["summaries"].append((stext.strip(), _clean_str(obj.get("leafUuid"))))
                    elif rec_type == "last-prompt":
                        if isinstance(obj.get("lastPrompt"), str):
                            meta["last_prompt_text"] = obj["lastPrompt"]
                        leaf = _clean_str(obj.get("leafUuid"))
                        if leaf:
                            meta["last_prompt_leaf"] = leaf
                    elif rec_type == "content-replacement":
                        _collect_replaced_ids(obj, meta["replaced_ids"])
                    elif obj.get("isSidechain") is not True:
                        rec_cwd = _clean_str(obj.get("cwd"))
                        if rec_cwd:
                            meta["cwd"] = rec_cwd
                        rec_branch = _clean_str(obj.get("gitBranch"))
                        if rec_branch:
                            meta["git_branch"] = rec_branch
        except OSError as exc:
            raise ProviderReadError(f"Unable to read Claude session '{path}': {exc}") from exc

        if malformed:
            _add_unique(warnings, f"claude_malformed_jsonl_lines:{malformed}")
        return all_records, meta

    def load_session(self, ref: str, *, max_tool_chars: int = 600) -> SessionDetail:
        target = Path(ref).expanduser()
        path = target if target.is_file() else self._resolve_session_path(ref)

        warnings: list[str] = []
        all_records, meta = self._read_session_file(path, warnings)
        resolved_sid = meta["sid"] or path.stem
        # Read history.jsonl silently so malformed history entries never pollute an explicit session load.
        hist = self._read_history_index(None).get(resolved_sid, {})

        branch_records, snipped_uuids, resolved_leaf_uuid = self._reconstruct_active_branch(
            all_records, meta["last_prompt_leaf"], warnings
        )

        secret_counter = [0]
        turns: list[NormalizedTurn] = []
        tool_events_by_id: dict[str, ToolEvent] = {}
        files_mentioned: list[str] = []
        commands_run: list[str] = []
        compaction_count = 0
        pending_compaction = False
        saw_boundary_for_pending = False
        created_at = ""
        updated_at = ""
        created_ms = 0
        updated_ms = 0

        def _ensure_active_turn(ts: str) -> NormalizedTurn:
            nonlocal pending_compaction, saw_boundary_for_pending
            if not turns or pending_compaction:
                t = NormalizedTurn(
                    turn_index=len(turns) + 1,
                    timestamp=ts,
                    user_text="",
                    assistant_text="",
                    compaction_before=pending_compaction,
                )
                pending_compaction = False
                saw_boundary_for_pending = False
                turns.append(t)
            elif not turns[-1].timestamp and ts:
                turns[-1].timestamp = ts
            return turns[-1]

        def _start_user_turn(cleaned_text: str, ts: str) -> NormalizedTurn:
            nonlocal pending_compaction, saw_boundary_for_pending
            if turns and not turns[-1].user_text and not turns[-1].assistant_text and not turns[-1].tool_events:
                turns[-1].user_text = cleaned_text
                if ts and not turns[-1].timestamp:
                    turns[-1].timestamp = ts
                if pending_compaction:
                    turns[-1].compaction_before = True
                    pending_compaction = False
                    saw_boundary_for_pending = False
                return turns[-1]
            t = NormalizedTurn(
                turn_index=len(turns) + 1,
                timestamp=ts,
                user_text=cleaned_text,
                assistant_text="",
                compaction_before=pending_compaction,
            )
            pending_compaction = False
            saw_boundary_for_pending = False
            turns.append(t)
            return t

        for rec in branch_records:
            created_at, created_ms, updated_at, updated_ms = _update_ts_bounds(
                rec.get("timestamp"), created_at, created_ms, updated_at, updated_ms
            )
            ts_iso = normalize_timestamp(rec.get("timestamp"))
            rec_type = str(rec.get("type") or "")

            if rec_type == "system":
                if rec.get("subtype") == "compact_boundary":
                    compaction_count += 1
                    pending_compaction = True
                    saw_boundary_for_pending = True
                    _add_unique(warnings, "compaction_gap")
                continue

            if rec.get("isCompactSummary") is True:
                if not saw_boundary_for_pending:
                    compaction_count += 1
                pending_compaction = True
                saw_boundary_for_pending = False
                _add_unique(warnings, "compaction_gap")
                continue

            if _is_filtered_record(rec, snipped_uuids):
                continue

            if rec_type == "user":
                raw_text, _, tool_results = _extract_content_blocks(
                    _get_message_content(rec), collect_tool_results=True
                )
                cleaned_user = _clean_text_counted(raw_text, secret_counter, strip_tags=True)
                if cleaned_user:
                    _start_user_turn(cleaned_user, ts_iso)

                tool_use_result = rec.get("toolUseResult")
                for block in tool_results:
                    tu_id = _clean_str(block.get("tool_use_id"))
                    existing = tool_events_by_id.get(tu_id) if tu_id else None
                    tname = existing.tool_name if existing else "tool_result"
                    raw_out = _tool_result_content_to_text(block.get("content"), tool_use_result)
                    if _is_externalized_tool_result(
                        tu_id, raw_out, tool_use_result, meta["replaced_ids"]
                    ):
                        out_summary = _EXTERNALIZED_STUB
                        out_trunc = False
                        _add_unique(warnings, "externalized_tool_result_omitted")
                    else:
                        cleaned_out = _clean_text_counted(raw_out, secret_counter, strip_tags=True)
                        out_summary, out_trunc = truncate_text(
                            cleaned_out, max_tool_chars, single_line=False
                        )
                    status = _resolve_tool_result_status(block, tool_use_result, raw_out, tname)
                    if existing is not None:
                        existing.output_summary = out_summary
                        existing.status = status
                        existing.truncated = existing.truncated or out_trunc
                    else:
                        turn = _ensure_active_turn(ts_iso)
                        event = ToolEvent(
                            call_id=tu_id,
                            tool_name=tname,
                            arguments_summary="",
                            output_summary=out_summary,
                            status=status,
                            truncated=out_trunc,
                        )
                        turn.tool_events.append(event)
                        if tu_id:
                            tool_events_by_id[tu_id] = event
                continue

            if rec_type == "assistant":
                raw_assistant, tool_uses, _ = _extract_content_blocks(
                    _get_message_content(rec), collect_tool_uses=True
                )
                cleaned_assistant = _clean_text_counted(raw_assistant, secret_counter, strip_tags=True)
                if cleaned_assistant:
                    turn = _ensure_active_turn(ts_iso)
                    turn.assistant_text = (
                        f"{turn.assistant_text}\n\n{cleaned_assistant}" if turn.assistant_text else cleaned_assistant
                    )

                for tu in tool_uses:
                    turn = _ensure_active_turn(ts_iso)
                    call_id = _clean_str(tu.get("id"))
                    tool_name = _clean_str(tu.get("name")) or "tool"
                    tool_input = tu.get("input")
                    touched = _extract_files_from_input(tool_input)
                    for fpath in touched:
                        _add_unique(files_mentioned, fpath)

                    cmd = _extract_command(tool_name, tool_input)
                    if cmd:
                        _add_unique(commands_run, _preview_line(cmd, _CMD_PREVIEW_LIMIT, secret_counter))

                    if tool_name.lower() in _SUBAGENT_TOOL_NAMES and isinstance(tool_input, dict):
                        sub_type = _clean_str(tool_input.get("subagent_type")) or "subagent"
                        desc = _clean_str(tool_input.get("description"))
                        note_raw = f"subagent:{tool_name} ({sub_type}) {desc}".strip()
                        _add_unique(turn.subagent_notes, _preview_line(note_raw, _PROMPT_PREVIEW_LIMIT, secret_counter))

                    raw_args = (
                        tool_input
                        if isinstance(tool_input, str)
                        else (json.dumps(tool_input, ensure_ascii=False) if tool_input is not None else "")
                    )
                    cleaned_args = _clean_text_counted(raw_args, secret_counter, strip_tags=False)
                    args_summary, arg_trunc = truncate_text(cleaned_args, max_tool_chars, single_line=True)

                    event = ToolEvent(
                        call_id=call_id,
                        tool_name=tool_name,
                        arguments_summary=args_summary,
                        output_summary="",
                        status="unknown",
                        files_touched=touched,
                        truncated=arg_trunc,
                    )
                    turn.tool_events.append(event)
                    if call_id:
                        tool_events_by_id[call_id] = event

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

        summary = self._build_session_summary(
            path=path,
            session_id=resolved_sid,
            raw_cwd=meta["cwd"],
            git_branch=meta["git_branch"],
            custom_title=meta["custom_title"],
            ai_title=meta["ai_title"],
            summaries=meta["summaries"],
            active_leaf_uuid=meta["last_prompt_leaf"] or resolved_leaf_uuid,
            last_prompt_text=meta["last_prompt_text"],
            first_prompt=user_prompts[0] if user_prompts else "",
            last_prompt=user_prompts[-1] if user_prompts else "",
            created_at=created_at,
            updated_at=updated_at,
            turn_count=len(turns),
            hist=hist,
            warnings=warnings,
            secret_counter=secret_counter,
        )
        if secret_counter[0] > 0:
            secret_warn = f"secrets_redacted:{secret_counter[0]}"
            _add_unique(warnings, secret_warn)
            _add_unique(summary.warnings, secret_warn)
        return SessionDetail(
            summary=summary,
            turns=turns,
            files_mentioned=files_mentioned,
            commands_run=commands_run,
            compaction_count=compaction_count,
            total_chars=total_chars,
            warnings=list(warnings),
        )

    def extract_searchable_text(
        self, summary: SessionSummary, *, max_bytes: int = 262_144
    ) -> str:
        if max_bytes <= 0 or not summary.source_path:
            return ""
        path = Path(summary.source_path)
        if not path.is_file():
            return ""

        secret_counter = [0]
        chunks: list[str] = []
        bytes_seen = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    bytes_seen += len(line.encode("utf-8", errors="replace"))
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict) or _is_filtered_record(obj):
                        continue
                    rtype = str(obj.get("type") or "")
                    if rtype in {"user", "assistant"}:
                        raw_text, _, _ = _extract_content_blocks(_get_message_content(obj))
                        cleaned = _clean_text_counted(raw_text, secret_counter, strip_tags=True)
                        if cleaned:
                            chunks.append(cleaned)
                    if bytes_seen >= max_bytes:
                        break
        except OSError:
            return ""
        return "\n".join(chunks)[:max_bytes]
