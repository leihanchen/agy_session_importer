#!/usr/bin/env python3
"""Pure-stdlib stdio MCP JSON-RPC 2.0 server and MCP App tool suite."""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, BinaryIO, Callable

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.agy_bridge import (
    build_import_seed_prompt,
    resolve_agy_project,
    spawn_agy_conversation_async,
)
from importer.models import ImportedSessionRecord, SessionDetail
from importer.providers.registry import ProviderRegistry, create_default_registry
from importer.render import format_show_text, render_show_envelope
from importer.safety import TRUST_BOUNDARY, truncate_text
from importer.search import ResolutionResult, list_sessions, resolve_session
from importer.store import ImportedSessionStore, make_import_id

APP_RESOURCE_URI = "ui://session-importer/app.html"
APP_MIME_TYPE = "text/html;profile=mcp-app"

_DEFAULT_PROTOCOL_VERSION = "2025-03-26"
_SERVER_NAME = "agy-session-importer"
_SERVER_VERSION = "0.2.0"
_MAX_OPEN_TURNS = 25
_MAX_STRUCTURED_BYTES = 512_000
_DEFAULT_LIST_LIMIT = 30
_DEFAULT_DETAIL_TURN_END = 50
_DEFAULT_MAX_TOOL_CHARS = 2000


def _merge_warnings(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def _resolve_ui_html_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path = os.environ.get("AGY_SESSION_IMPORTER_UI_HTML", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path(__file__).resolve().parents[1] / "ui" / "app.html"


def _fallback_ui_html(ui_path: Path) -> str:
    escaped = html.escape(str(ui_path))
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head><meta charset=\"utf-8\"><title>Session Importer</title></head>\n"
        "<body>\n"
        "<main>\n"
        "  <h1>Session Importer UI</h1>\n"
        f"  <p>UI bundle not found at <code>{escaped}</code>.</p>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _json_byte_len(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _bound_turn_dicts(turns: list[dict[str, Any]], max_tool_chars: int) -> list[dict[str, Any]]:
    if max_tool_chars < 0:
        return turns
    bounded: list[dict[str, Any]] = []
    for turn in turns:
        turn_copy = dict(turn)
        raw_events = turn_copy.get("tool_events") or []
        new_events: list[dict[str, Any]] = []
        for ev in raw_events:
            ev_copy = dict(ev)
            args_sum, args_trunc = truncate_text(str(ev_copy.get("arguments_summary") or ""), max_tool_chars)
            out_sum, out_trunc = truncate_text(str(ev_copy.get("output_summary") or ""), max_tool_chars)
            ev_copy["arguments_summary"] = args_sum
            ev_copy["output_summary"] = out_sum
            ev_copy["truncated"] = bool(ev_copy.get("truncated")) or args_trunc or out_trunc
            new_events.append(ev_copy)
        turn_copy["tool_events"] = new_events
        bounded.append(turn_copy)
    return bounded


def _shrink_turns_tool_summaries(turns: list[dict[str, Any]], limit: int) -> None:
    for turn in turns:
        for ev in turn.get("tool_events") or []:
            args_sum, args_trunc = truncate_text(str(ev.get("arguments_summary") or ""), limit)
            out_sum, out_trunc = truncate_text(str(ev.get("output_summary") or ""), limit)
            ev["arguments_summary"] = args_sum
            ev["output_summary"] = out_sum
            ev["truncated"] = bool(ev.get("truncated")) or args_trunc or out_trunc


def _shrink_list_for_budget(items: list[str], target_max_items: int, item_char_limit: int) -> list[str]:
    trimmed: list[str] = []
    for entry in items[:target_max_items]:
        text, _ = truncate_text(str(entry), item_char_limit)
        trimmed.append(text)
    return trimmed


def _enforce_open_payload_budget(
    payload: dict[str, Any],
    *,
    max_bytes: int = _MAX_STRUCTURED_BYTES,
) -> dict[str, Any]:
    if _json_byte_len(payload) < max_bytes:
        return payload

    selected = payload.get("selected_session")
    if isinstance(selected, dict):
        turns = selected.get("turns")
        if isinstance(turns, list) and turns:
            for tool_limit in (300, 120):
                _shrink_turns_tool_summaries(turns, tool_limit)
                if _json_byte_len(payload) < max_bytes:
                    return payload

        if _json_byte_len(payload) >= max_bytes:
            trimmed_metadata = False
            for list_key in ("files_mentioned", "commands_run"):
                raw_list = selected.get(list_key)
                if isinstance(raw_list, list) and raw_list:
                    shrunk = _shrink_list_for_budget(raw_list, 50, 200)
                    if shrunk != raw_list:
                        selected[list_key] = shrunk
                        trimmed_metadata = True
            if trimmed_metadata:
                selected["warnings"] = _merge_warnings(
                    list(selected.get("warnings") or []),
                    ["metadata_trimmed_for_payload_limit"],
                )
                payload["warnings"] = _merge_warnings(
                    list(payload.get("warnings") or []),
                    ["metadata_trimmed_for_payload_limit"],
                )
            if _json_byte_len(payload) < max_bytes:
                return payload

        if isinstance(turns, list) and turns:
            while len(turns) > 1 and _json_byte_len(payload) >= max_bytes:
                turns.pop()
                last_idx = turns[-1].get("turn_index")
                selected["turn_end"] = (
                    int(last_idx)
                    if isinstance(last_idx, int) and last_idx >= 1
                    else int(selected.get("turn_start", 1)) + len(turns) - 1
                )
                selected["has_more"] = True
                selected["has_more_turns"] = True
                payload["has_more_turns"] = True

        if _json_byte_len(payload) >= max_bytes:
            drained_metadata = False
            for list_key in ("files_mentioned", "commands_run"):
                raw_list = selected.get(list_key)
                if isinstance(raw_list, list) and raw_list:
                    while selected[list_key] and _json_byte_len(payload) >= max_bytes:
                        half = len(selected[list_key]) // 2
                        selected[list_key] = selected[list_key][:half]
                        drained_metadata = True
            if drained_metadata:
                selected["warnings"] = _merge_warnings(
                    list(selected.get("warnings") or []),
                    ["metadata_trimmed_for_payload_limit"],
                )
                payload["warnings"] = _merge_warnings(
                    list(payload.get("warnings") or []),
                    ["metadata_trimmed_for_payload_limit"],
                )
            if _json_byte_len(payload) < max_bytes:
                return payload

        if isinstance(turns, list) and len(turns) == 1 and _json_byte_len(payload) >= max_bytes:
                single = turns[0]
                text_limit = 32_000
                while text_limit >= 128 and _json_byte_len(payload) >= max_bytes:
                    user_t, _ = truncate_text(str(single.get("user_text") or ""), text_limit)
                    asst_t, _ = truncate_text(str(single.get("assistant_text") or ""), text_limit)
                    single["user_text"] = user_t
                    single["assistant_text"] = asst_t
                    _shrink_turns_tool_summaries(turns, min(120, text_limit))
                    text_limit //= 2
                if _json_byte_len(payload) >= max_bytes:
                    single["tool_events"] = []
                    single["user_text"], _ = truncate_text(str(single.get("user_text") or ""), 64)
                    single["assistant_text"], _ = truncate_text(str(single.get("assistant_text") or ""), 64)
                selected["warnings"] = _merge_warnings(
                    list(selected.get("warnings") or []),
                    ["truncated_for_payload_limit"],
                )
                payload["warnings"] = _merge_warnings(
                    list(payload.get("warnings") or []),
                    ["truncated_for_payload_limit"],
                )

        if _json_byte_len(payload) >= max_bytes and isinstance(selected.get("summary"), dict):
            summary = selected["summary"]
            for field_key in ("first_user_prompt", "last_user_prompt", "title", "cwd", "source_path"):
                val = summary.get(field_key)
                if isinstance(val, str) and len(val) > 256:
                    summary[field_key], _ = truncate_text(val, 256)
            if isinstance(summary.get("warnings"), list) and len(summary["warnings"]) > 10:
                summary["warnings"] = summary["warnings"][:10]

    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        while candidates and _json_byte_len(payload) >= max_bytes:
            candidates.pop()
            payload["warnings"] = _merge_warnings(
                list(payload.get("warnings") or []),
                ["candidates_trimmed_for_payload_limit"],
            )

    imports = payload.get("imports")
    if isinstance(imports, list):
        while imports and _json_byte_len(payload) >= max_bytes:
            imports.pop()
            payload["warnings"] = _merge_warnings(
                list(payload.get("warnings") or []),
                ["imports_trimmed_for_payload_limit"],
            )

    if _json_byte_len(payload) >= max_bytes and isinstance(selected, dict):
        had_turns = bool(selected.get("turns")) or int(payload.get("total_turns") or 0) > 0
        if selected.get("turns"):
            selected["turns"] = []
            selected["turn_end"] = 0
            selected["has_more"] = had_turns
            selected["has_more_turns"] = had_turns
            payload["has_more_turns"] = had_turns
            selected["warnings"] = _merge_warnings(
                list(selected.get("warnings") or []),
                ["turns_dropped_for_payload_limit"],
            )
            payload["warnings"] = _merge_warnings(
                list(payload.get("warnings") or []),
                ["turns_dropped_for_payload_limit"],
            )
        if _json_byte_len(payload) >= max_bytes:
            payload["selected_session"] = None
            payload["has_more_turns"] = had_turns
            payload["warnings"] = _merge_warnings(
                list(payload.get("warnings") or []),
                ["selected_session_dropped_for_payload_limit"],
            )

    if _json_byte_len(payload) >= max_bytes and isinstance(payload.get("warnings"), list):
        payload["warnings"] = ["truncated_for_payload_limit"]

    return payload


def _jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _tool_response(text: str, structured: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    resp: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
    }
    if is_error:
        resp["isError"] = True
    return resp


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class SessionImporterMCPServer:
    """Pure-stdlib stdio MCP JSON-RPC 2.0 server for the Session Importer UI and tools."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry | None = None,
        store: ImportedSessionStore | None = None,
        ui_html_path: Path | None = None,
        projects_dir: Path | None = None,
        agy_bin: str | None = None,
        spawn_runner: Callable[..., Any] | None = None,
    ) -> None:
        self.registry = create_default_registry() if registry is None else registry
        self.store = ImportedSessionStore() if store is None else store
        self.ui_html_path = _resolve_ui_html_path(ui_html_path)
        self.projects_dir = Path(projects_dir).expanduser() if projects_dir is not None else None
        self.agy_bin = agy_bin
        self.spawn_runner = spawn_runner
        self._write_lock = threading.Lock()

    def _provider_display_name(self, provider_name: str) -> str:
        try:
            return self.registry.get(provider_name).display_name
        except KeyError:
            return provider_name

    def _provider_hint(self) -> str:
        names = self.registry.names()
        if not names:
            return "'all'"
        return "'all', " + ", ".join(f"'{n}'" for n in names)

    def _tool_definitions(self) -> list[dict[str, Any]]:
        provider_hint = self._provider_hint()
        return [
            {
                "name": "open_session_importer",
                "description": (
                    "Open the Session Importer interactive UI to browse imported conversations, "
                    "discover prior external agent sessions, or inspect full turn history."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "provider": {
                            "type": "string",
                            "default": "all",
                            "description": f"Provider selector ({provider_hint}, or alias).",
                        },
                        "query": {
                            "type": "string",
                            "default": "",
                            "description": "Optional search keywords to pre-filter candidates.",
                        },
                        "session_ref": {
                            "type": "string",
                            "default": "",
                            "description": "Optional import_id, session_id, path, or query to open in History Viewer.",
                        },
                        "tab": {
                            "type": "string",
                            "default": "",
                            "description": "Initial tab ('imported', 'discover', 'history', or '' for automatic).",
                        },
                        "cwd": {
                            "type": "string",
                            "default": "",
                            "description": "Optional working directory filter.",
                        },
                        "include_archived": {
                            "type": "boolean",
                            "default": False,
                            "description": "Include archived sessions when supported by a provider.",
                        },
                    },
                    "required": [],
                },
                "_meta": {
                    "ui": {
                        "resourceUri": APP_RESOURCE_URI,
                        "visibility": ["model", "app"],
                    },
                    "com.google.antigravity/ui": {
                        "entrypoints": [
                            {"type": "aux_pane", "title": "Session Importer"},
                            {"type": "global", "title": "Session Importer"},
                        ]
                    },
                },
            },
            {
                "name": "render_imported_conversation",
                "description": (
                    "Embed an interactive Turn 1..N conversation history viewer directly inline "
                    "inside the current Antigravity chat message for an imported session."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "import_id": {
                            "type": "string",
                            "default": "",
                            "description": "Import ID from the import store.",
                        },
                        "session_ref": {
                            "type": "string",
                            "default": "",
                            "description": "Optional fallback session_id or transcript path if import_id is omitted.",
                        },
                        "provider": {
                            "type": "string",
                            "default": "all",
                            "description": f"Provider selector ({provider_hint}, or alias).",
                        },
                    },
                    "required": [],
                },
                "_meta": {
                    "ui": {
                        "resourceUri": APP_RESOURCE_URI,
                        "visibility": ["model", "app"],
                    },
                },
            },
            {
                "name": "import_session_context",
                "description": (
                    "Resolve a prior external agent session, save a durable 0600 snapshot in the "
                    "import store, and return bounded untrusted-history handoff context."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "default": "latest",
                            "description": "Session ID/prefix, file path, 'latest', or search keywords.",
                        },
                        "provider": {
                            "type": "string",
                            "default": "all",
                            "description": f"Provider selector ({provider_hint}, or alias).",
                        },
                        "mode": {
                            "type": "string",
                            "default": "full",
                            "enum": ["full", "compact"],
                            "description": "Rendered context mode ('full' or 'compact').",
                        },
                        "cwd": {
                            "type": "string",
                            "default": "",
                            "description": "Target workspace directory (defaults to current working directory).",
                        },
                        "allow_cross_repo": {
                            "type": "boolean",
                            "default": False,
                            "description": "Allow loading a matched session from another repository.",
                        },
                        "conversation_id": {
                            "type": "string",
                            "default": "",
                            "description": "Optional active Antigravity conversation ID to associate.",
                        },
                    },
                    "required": [],
                },
                "_meta": {
                    "ui": {
                        "visibility": ["model", "app"],
                    }
                },
            },
            {
                "name": "app_list_sessions",
                "description": "List and search external agent sessions for the MCP App UI.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "provider": {
                            "type": "string",
                            "default": "all",
                            "description": f"Provider selector ({provider_hint}, or alias).",
                        },
                        "query": {
                            "type": "string",
                            "default": "",
                            "description": "Optional search keywords.",
                        },
                        "cwd": {
                            "type": "string",
                            "default": "",
                            "description": "Target working directory when all_cwds is false.",
                        },
                        "all_cwds": {
                            "type": "boolean",
                            "default": True,
                            "description": "Search across all repositories when true.",
                        },
                        "include_archived": {
                            "type": "boolean",
                            "default": False,
                            "description": "Include archived sessions when supported.",
                        },
                        "limit": {
                            "type": "integer",
                            "default": _DEFAULT_LIST_LIMIT,
                            "description": "Maximum candidate sessions to return.",
                        },
                    },
                    "required": [],
                },
                "_meta": {
                    "ui": {
                        "visibility": ["app"],
                    }
                },
            },
            {
                "name": "app_get_session_detail",
                "description": "Load paginated session turns from snapshot or live provider storage for the UI.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "default": "",
                            "description": "Session ID, prefix, file path, or query.",
                        },
                        "provider": {
                            "type": "string",
                            "default": "all",
                            "description": f"Provider selector ({provider_hint}, or alias).",
                        },
                        "import_id": {
                            "type": "string",
                            "default": "",
                            "description": "Optional persisted import_id.",
                        },
                        "from_snapshot": {
                            "type": "boolean",
                            "default": True,
                            "description": "Prefer persisted local snapshot when available.",
                        },
                        "turn_start": {
                            "type": "integer",
                            "default": 1,
                            "description": "1-based inclusive start turn index.",
                        },
                        "turn_end": {
                            "type": "integer",
                            "default": _DEFAULT_DETAIL_TURN_END,
                            "description": "1-based inclusive end turn index.",
                        },
                        "max_tool_chars": {
                            "type": "integer",
                            "default": _DEFAULT_MAX_TOOL_CHARS,
                            "description": "Maximum characters per tool argument/output summary.",
                        },
                    },
                    "required": [],
                },
                "_meta": {
                    "ui": {
                        "visibility": ["app"],
                    }
                },
            },
            {
                "name": "app_list_imports",
                "description": "List persisted imported session records for the UI.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                "_meta": {
                    "ui": {
                        "visibility": ["app"],
                    }
                },
            },
            {
                "name": "app_start_import",
                "description": "Start importing a selected session into a new Antigravity conversation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": "Import ID, session ID/prefix, file path, or query.",
                        },
                        "provider": {
                            "type": "string",
                            "default": "all",
                            "description": f"Provider selector ({provider_hint}, or alias).",
                        },
                        "mode": {
                            "type": "string",
                            "default": "full",
                            "enum": ["full", "compact"],
                            "description": "Seed prompt rendering mode.",
                        },
                        "spawn_via": {
                            "type": "string",
                            "default": "cli_background",
                            "enum": ["client_send_message", "cli_background"],
                            "description": "Conversation creation strategy.",
                        },
                    },
                    "required": ["ref"],
                },
                "_meta": {
                    "ui": {
                        "visibility": ["app"],
                    }
                },
            },
            {
                "name": "app_complete_import",
                "description": "Finalize an in-progress UI import or trigger background CLI fallback.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "import_id": {
                            "type": "string",
                            "description": "Persisted import ID.",
                        },
                        "agy_conversation_id": {
                            "type": "string",
                            "default": "",
                            "description": "Created Antigravity conversation ID.",
                        },
                        "fallback_to_cli": {
                            "type": "boolean",
                            "default": False,
                            "description": "Launch background CLI conversation creation from saved snapshot.",
                        },
                        "error": {
                            "type": "string",
                            "default": "",
                            "description": "Optional error message if client message dispatch failed.",
                        },
                    },
                    "required": ["import_id"],
                },
                "_meta": {
                    "ui": {
                        "visibility": ["app"],
                    }
                },
            },
            {
                "name": "app_delete_import",
                "description": "Delete a persisted import record and its local snapshot from the store.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "import_id": {
                            "type": "string",
                            "description": "Persisted import ID to delete.",
                        },
                    },
                    "required": ["import_id"],
                },
                "_meta": {
                    "ui": {
                        "visibility": ["app"],
                    }
                },
            },
        ]

    def _records_by_id(self) -> tuple[list[ImportedSessionRecord], dict[str, dict[str, Any]]]:
        records = self.store.list_records()
        mapping = {rec.import_id: rec.to_dict() for rec in records}
        return records, mapping

    def _attach_import_records(
        self,
        summaries: list[Any],
        records_by_id: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for summary in summaries:
            cand = summary.to_dict()
            imp_id = make_import_id(summary.provider, summary.session_id, summary.source_path)
            cand["imported_record"] = records_by_id.get(imp_id)
            candidates.append(cand)
        return candidates

    def _build_detail_payload(
        self,
        detail: SessionDetail,
        *,
        source: str,
        import_record: ImportedSessionRecord | None,
        turn_start: int,
        turn_end: int,
        max_tool_chars: int,
        extra_warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        total_turns = len(detail.turns)
        if total_turns == 0:
            actual_end = 0
            sliced_turns: list[dict[str, Any]] = []
            has_more = False
        elif turn_start > total_turns:
            actual_end = total_turns
            sliced_turns = []
            has_more = False
        else:
            actual_end = min(turn_end, total_turns)
            selected = detail.turns[turn_start - 1 : actual_end]
            sliced_turns = _bound_turn_dicts([t.to_dict() for t in selected], max_tool_chars)
            has_more = actual_end < total_turns

        warnings = _merge_warnings(
            list(detail.summary.warnings),
            list(detail.warnings),
            list(extra_warnings or []),
        )
        return {
            "schema_version": 1,
            "trust_boundary": TRUST_BOUNDARY,
            "summary": detail.summary.to_dict(),
            "turns": sliced_turns,
            "total_turns": total_turns,
            "turn_start": turn_start,
            "turn_end": actual_end,
            "has_more": has_more,
            "has_more_turns": has_more,
            "files_mentioned": list(detail.files_mentioned),
            "commands_run": list(detail.commands_run),
            "compaction_count": detail.compaction_count,
            "source": source,
            "import_record": import_record.to_dict() if import_record is not None else None,
            "warnings": warnings,
        }

    def _get_store_record_for_provider(
        self,
        ref: str,
        *,
        provider: str = "all",
    ) -> ImportedSessionRecord | None:
        resolved = self.registry.resolve(provider)
        all_names = self.registry.names()
        if len(resolved) == len(all_names):
            return self.store.get_record(ref, provider="all")
        if len(resolved) == 1:
            return self.store.get_record(ref, provider=resolved[0].name)

        matches: list[ImportedSessionRecord] = []
        seen_ids: set[str] = set()
        for prov in resolved:
            rec = self.store.get_record(ref, provider=prov.name)
            if rec is not None and rec.import_id not in seen_ids:
                seen_ids.add(rec.import_id)
                matches.append(rec)
        return matches[0] if len(matches) == 1 else None

    def _load_detail_for_ui(
        self,
        *,
        ref: str = "",
        provider: str = "all",
        import_id: str = "",
        from_snapshot: bool = True,
        max_tool_chars: int = _DEFAULT_MAX_TOOL_CHARS,
        cwd: str | None = None,
    ) -> tuple[SessionDetail | None, str, ImportedSessionRecord | None, list[str]]:
        ref_clean = (ref or "").strip()
        import_id_clean = (import_id or "").strip()
        effective_cwd = (cwd or "").strip() or os.getcwd()
        warnings: list[str] = []

        record: ImportedSessionRecord | None = None
        if import_id_clean:
            record = self.store.get_record(import_id_clean)
        elif ref_clean:
            record = self._get_store_record_for_provider(ref_clean, provider=provider)

        prefer_snapshot = from_snapshot and (
            bool(import_id_clean) or (record is not None and ref_clean == record.import_id)
        )
        if prefer_snapshot and record is not None:
            try:
                detail = self.store.load_snapshot(record.import_id)
                return detail, "snapshot", record, warnings
            except (FileNotFoundError, ValueError, OSError) as exc:
                warnings.append(f"snapshot_load_fallback:{exc}")

        lookup_ref = ref_clean or (record.session_id if record else "") or (record.source_path if record else "")
        lookup_provider = (
            provider
            if (provider and provider.strip().lower() != "all")
            else (record.provider if record else "all")
        )

        if lookup_ref:
            res = resolve_session(
                self.registry,
                lookup_ref,
                provider=lookup_provider,
                cwd=effective_cwd,
                allow_cross_repo=True,
                max_tool_chars=max_tool_chars,
                include_archived=True,
            )
            warnings = _merge_warnings(warnings, list(res.warnings))
            if res.status == "matched" and res.detail is not None:
                matched_rec = record or self.store.get_record(
                    make_import_id(
                        res.detail.summary.provider,
                        res.detail.summary.session_id,
                        res.detail.summary.source_path,
                    )
                )
                return res.detail, "live", matched_rec, warnings

        if from_snapshot and record is not None:
            try:
                detail = self.store.load_snapshot(record.import_id)
                return detail, "snapshot", record, warnings
            except (FileNotFoundError, ValueError, OSError) as exc:
                warnings.append(f"snapshot_load_failed:{exc}")

        return None, "live", record, warnings

    def _tool_open_session_importer(self, args: dict[str, Any]) -> dict[str, Any]:
        provider = str(args.get("provider") or "all")
        query = str(args.get("query") or "")
        session_ref = str(args.get("session_ref") or "").strip()
        tab_arg = str(args.get("tab") or "").strip()
        cwd_arg = str(args.get("cwd") or "").strip()
        include_archived = bool(args.get("include_archived", False))

        self.registry.resolve(provider)
        providers = self.registry.describe()
        records, records_by_id = self._records_by_id()
        imports = [rec.to_dict() for rec in records]

        summaries, warnings = list_sessions(
            self.registry,
            provider=provider,
            cwd=cwd_arg or None,
            all_cwds=not bool(cwd_arg),
            query=query,
            limit=_DEFAULT_LIST_LIMIT,
            include_archived=include_archived,
        )
        candidates = self._attach_import_records(summaries, records_by_id)

        selected_session: dict[str, Any] | None = None
        if session_ref:
            detail, source, matched_rec, detail_warnings = self._load_detail_for_ui(
                ref=session_ref,
                provider=provider,
                from_snapshot=True,
                max_tool_chars=_DEFAULT_MAX_TOOL_CHARS,
                cwd=cwd_arg or None,
            )
            warnings = _merge_warnings(warnings, detail_warnings)
            if detail is not None:
                selected_session = self._build_detail_payload(
                    detail,
                    source=source,
                    import_record=matched_rec,
                    turn_start=1,
                    turn_end=_MAX_OPEN_TURNS,
                    max_tool_chars=_DEFAULT_MAX_TOOL_CHARS,
                )

        if selected_session is not None:
            total_turns = int(selected_session["total_turns"])
            has_more_turns = bool(selected_session["has_more_turns"])
        else:
            total_turns = 0
            has_more_turns = False

        if tab_arg in ("imported", "discover", "history"):
            resolved_tab = tab_arg
        elif selected_session is not None:
            resolved_tab = "history"
        elif imports:
            resolved_tab = "imported"
        else:
            resolved_tab = "discover"

        structured: dict[str, Any] = {
            "schema_version": 1,
            "trust_boundary": TRUST_BOUNDARY,
            "tab": resolved_tab,
            "cwd": cwd_arg,
            "all_cwds": not bool(cwd_arg),
            "providers": providers,
            "imports": imports,
            "candidates": candidates,
            "selected_session": selected_session,
            "has_more_turns": has_more_turns,
            "total_turns": total_turns,
            "warnings": warnings,
        }
        _enforce_open_payload_budget(structured, max_bytes=_MAX_STRUCTURED_BYTES)
        summary_text = (
            f"Session Importer ready (tab={structured['tab']}, "
            f"imports={len(structured['imports'])}, candidates={len(structured['candidates'])}"
            + (f", selected_turns={structured['total_turns']}" if structured["selected_session"] else "")
            + ")."
        )
        return _tool_response(summary_text, structured)

    def _tool_render_imported_conversation(self, args: dict[str, Any]) -> dict[str, Any]:
        import_id = str(args.get("import_id") or "").strip()
        session_ref = str(args.get("session_ref") or "").strip()
        provider = str(args.get("provider") or "all")

        self.registry.resolve(provider)
        target_ref = import_id or session_ref or "latest"
        detail, source, matched_rec, warnings = self._load_detail_for_ui(
            ref=target_ref,
            provider=provider,
            from_snapshot=True,
            max_tool_chars=_DEFAULT_MAX_TOOL_CHARS,
            cwd=os.getcwd(),
        )
        if detail is None:
            raise FileNotFoundError(f"Session not found for inline conversation view: {target_ref!r}")

        selected_session = self._build_detail_payload(
            detail,
            source=source,
            import_record=matched_rec,
            turn_start=1,
            turn_end=_MAX_OPEN_TURNS,
            max_tool_chars=_DEFAULT_MAX_TOOL_CHARS,
        )
        structured: dict[str, Any] = {
            "schema_version": 1,
            "trust_boundary": TRUST_BOUNDARY,
            "inline_mode": True,
            "tab": "history",
            "cwd": detail.summary.cwd,
            "all_cwds": True,
            "providers": self.registry.describe(),
            "imports": [matched_rec.to_dict()] if matched_rec is not None else [],
            "candidates": [],
            "selected_session": selected_session,
            "has_more_turns": bool(selected_session["has_more_turns"]),
            "total_turns": int(selected_session["total_turns"]),
            "warnings": warnings,
        }
        _enforce_open_payload_budget(structured, max_bytes=_MAX_STRUCTURED_BYTES)
        summary_text = (
            f"Embedded inline Turn 1..{structured['total_turns']} conversation history "
            f"for {detail.summary.provider}:{detail.summary.session_id or target_ref}."
        )
        return _tool_response(summary_text, structured)

    def _tool_import_session_context(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("ref") or "latest")
        provider = str(args.get("provider") or "all")
        mode = str(args.get("mode") or "full")
        cwd_arg = str(args.get("cwd") or "").strip()
        allow_cross_repo = bool(args.get("allow_cross_repo", False))
        conversation_id = str(args.get("conversation_id") or "").strip()

        if mode not in ("full", "compact"):
            raise ValueError(f"Unsupported render mode: {mode!r} (expected 'full' or 'compact')")
        self.registry.resolve(provider)

        result = resolve_session(
            self.registry,
            ref,
            provider=provider,
            cwd=cwd_arg or None,
            allow_cross_repo=allow_cross_repo,
        )
        show_envelope = render_show_envelope(result, mode=mode)

        import_record_dict: dict[str, Any] | None = None
        warnings = list(show_envelope.get("warnings") or [])
        if result.status == "matched" and result.detail is not None:
            detail = result.detail
            provider_display = self._provider_display_name(detail.summary.provider)
            matched_project = resolve_agy_project(detail.summary.cwd, projects_dir=self.projects_dir)
            record = self.store.save_import(
                detail,
                provider_display_name=provider_display,
                mode=mode,
                status="ready" if conversation_id else "in_session",
                agy_conversation_id=conversation_id,
                agy_project_id=matched_project.project_id if matched_project else "",
                agy_project_name=matched_project.project_name if matched_project else "",
            )
            import_record_dict = record.to_dict()
            warnings = _merge_warnings(warnings, list(record.warnings))

        structured: dict[str, Any] = {
            "schema_version": 1,
            "status": result.status,
            "trust_boundary": TRUST_BOUNDARY,
            "cross_repo": result.cross_repo,
            "mode": mode,
            "provider": str(show_envelope.get("provider") or ""),
            "import_record": import_record_dict,
            "session": show_envelope.get("session"),
            "rendered": str(show_envelope.get("rendered") or ""),
            "candidates": list(show_envelope.get("candidates") or []),
            "warnings": warnings,
        }
        text = structured["rendered"] or format_show_text(show_envelope)
        return _tool_response(text, structured)

    def _tool_app_list_sessions(self, args: dict[str, Any]) -> dict[str, Any]:
        provider = str(args.get("provider") or "all")
        query = str(args.get("query") or "")
        cwd_arg = str(args.get("cwd") or "").strip()
        all_cwds = bool(args.get("all_cwds", True))
        include_archived = bool(args.get("include_archived", False))
        limit_raw = args.get("limit", _DEFAULT_LIST_LIMIT)
        limit = int(limit_raw) if limit_raw is not None else _DEFAULT_LIST_LIMIT
        if limit < 0:
            raise ValueError(f"limit must be >= 0, got {limit}")

        self.registry.resolve(provider)
        summaries, warnings = list_sessions(
            self.registry,
            provider=provider,
            cwd=cwd_arg or None,
            all_cwds=all_cwds,
            query=query,
            limit=limit,
            include_archived=include_archived,
        )
        _, records_by_id = self._records_by_id()
        candidates = self._attach_import_records(summaries, records_by_id)
        structured = {
            "schema_version": 1,
            "providers": self.registry.describe(),
            "candidates": candidates,
            "warnings": warnings,
        }
        return _tool_response(f"Listed {len(candidates)} session(s).", structured)

    def _tool_app_get_session_detail(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("ref") or "")
        provider = str(args.get("provider") or "all")
        import_id = str(args.get("import_id") or "")
        from_snapshot = bool(args.get("from_snapshot", True))
        turn_start = int(args.get("turn_start") if args.get("turn_start") is not None else 1)
        turn_end = int(
            args.get("turn_end") if args.get("turn_end") is not None else _DEFAULT_DETAIL_TURN_END
        )
        max_tool_chars = int(
            args.get("max_tool_chars")
            if args.get("max_tool_chars") is not None
            else _DEFAULT_MAX_TOOL_CHARS
        )

        if turn_start < 1:
            raise ValueError(f"turn_start must be >= 1, got {turn_start}")
        if turn_end < turn_start:
            raise ValueError(f"turn_end ({turn_end}) must be >= turn_start ({turn_start})")
        if max_tool_chars < 0:
            raise ValueError(f"max_tool_chars must be >= 0, got {max_tool_chars}")
        self.registry.resolve(provider)

        detail, source, matched_rec, warnings = self._load_detail_for_ui(
            ref=ref,
            provider=provider,
            import_id=import_id,
            from_snapshot=from_snapshot,
            max_tool_chars=max_tool_chars,
        )
        if detail is None:
            raise ValueError(
                f"Session not found (ref={ref!r}, import_id={import_id!r}, provider={provider!r})"
            )

        structured = self._build_detail_payload(
            detail,
            source=source,
            import_record=matched_rec,
            turn_start=turn_start,
            turn_end=turn_end,
            max_tool_chars=max_tool_chars,
            extra_warnings=warnings,
        )
        text = (
            f"Loaded turns {structured['turn_start']}..{structured['turn_end']} "
            f"of {structured['total_turns']} ({source})."
        )
        return _tool_response(text, structured)

    def _tool_app_list_imports(self, _args: dict[str, Any]) -> dict[str, Any]:
        records = self.store.list_records()
        structured = {
            "schema_version": 1,
            "imports": [rec.to_dict() for rec in records],
        }
        return _tool_response(f"Listed {len(records)} imported session(s).", structured)

    def _tool_app_start_import(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("ref") or "").strip()
        provider = str(args.get("provider") or "all")
        mode = str(args.get("mode") or "full")
        spawn_via = str(args.get("spawn_via") or "cli_background")

        if not ref:
            return _tool_response(
                "Missing required 'ref' parameter.",
                {
                    "schema_version": 1,
                    "status": "error",
                    "import_record": None,
                    "seed_prompt": "",
                    "spawn_via": spawn_via,
                    "candidates": [],
                    "warnings": ["missing_ref"],
                },
                is_error=True,
            )
        if mode not in ("full", "compact"):
            return _tool_response(
                f"Unsupported mode: {mode!r}",
                {
                    "schema_version": 1,
                    "status": "error",
                    "import_record": None,
                    "seed_prompt": "",
                    "spawn_via": spawn_via,
                    "candidates": [],
                    "warnings": [f"unsupported_mode:{mode}"],
                },
                is_error=True,
            )
        if spawn_via not in ("client_send_message", "cli_background"):
            return _tool_response(
                f"Unsupported spawn_via: {spawn_via!r}",
                {
                    "schema_version": 1,
                    "status": "error",
                    "import_record": None,
                    "seed_prompt": "",
                    "spawn_via": spawn_via,
                    "candidates": [],
                    "warnings": [f"unsupported_spawn_via:{spawn_via}"],
                },
                is_error=True,
            )

        self.registry.resolve(provider)
        existing_rec = self._get_store_record_for_provider(ref, provider=provider)
        detail: SessionDetail | None = None
        warnings: list[str] = []
        resolution: ResolutionResult | None = None

        if existing_rec is not None and ref == existing_rec.import_id:
            try:
                detail = self.store.load_snapshot(existing_rec.import_id)
            except (FileNotFoundError, ValueError, OSError):
                detail = None

        if detail is None:
            lookup_ref = (
                (existing_rec.session_id or existing_rec.source_path)
                if (existing_rec is not None and ref == existing_rec.import_id)
                else ref
            )
            lookup_provider = (
                existing_rec.provider
                if (existing_rec is not None and ref == existing_rec.import_id)
                else provider
            )
            resolution = resolve_session(
                self.registry,
                lookup_ref,
                provider=lookup_provider,
                cwd=os.getcwd(),
                allow_cross_repo=True,
                include_archived=True,
            )
            warnings = _merge_warnings(warnings, list(resolution.warnings))
            if resolution.status == "matched" and resolution.detail is not None:
                detail = resolution.detail
            elif existing_rec is not None:
                try:
                    detail = self.store.load_snapshot(existing_rec.import_id)
                    warnings = _merge_warnings(warnings, ["loaded_from_snapshot"])
                except (FileNotFoundError, ValueError, OSError):
                    detail = None

        if detail is None:
            status = resolution.status if resolution is not None else "no_match"
            candidates = [c.to_dict() for c in resolution.candidates] if resolution is not None else []
            structured = {
                "schema_version": 1,
                "status": status,
                "import_record": None,
                "seed_prompt": "",
                "spawn_via": spawn_via,
                "candidates": candidates,
                "warnings": warnings,
            }
            return _tool_response(f"Import resolution status: {status}.", structured)

        provider_display = self._provider_display_name(detail.summary.provider)
        if spawn_via == "client_send_message":
            matched_project = resolve_agy_project(detail.summary.cwd, projects_dir=self.projects_dir)
            record = self.store.save_import(
                detail,
                provider_display_name=provider_display,
                mode=mode,
                status="creating",
                agy_project_id=matched_project.project_id if matched_project else "",
                agy_project_name=matched_project.project_name if matched_project else "",
            )
            seed_prompt = build_import_seed_prompt(
                detail,
                provider_display_name=provider_display,
                mode=mode,
                import_id=record.import_id,
            )
            structured = {
                "schema_version": 1,
                "status": "creating",
                "import_record": record.to_dict(),
                "seed_prompt": seed_prompt,
                "spawn_via": spawn_via,
                "candidates": [],
                "warnings": _merge_warnings(warnings, list(record.warnings)),
            }
            return _tool_response(f"Prepared seed prompt for import {record.import_id}.", structured)

        record, _thread = spawn_agy_conversation_async(
            detail,
            store=self.store,
            provider_display_name=provider_display,
            mode=mode,
            projects_dir=self.projects_dir,
            agy_bin=self.agy_bin,
            runner=self.spawn_runner,
        )
        structured = {
            "schema_version": 1,
            "status": record.status,
            "import_record": record.to_dict(),
            "seed_prompt": "",
            "spawn_via": spawn_via,
            "candidates": [],
            "warnings": _merge_warnings(warnings, list(record.warnings)),
        }
        return _tool_response(
            f"Import {record.import_id} started (status={record.status}).",
            structured,
            is_error=(record.status == "error"),
        )

    def _tool_app_complete_import(self, args: dict[str, Any]) -> dict[str, Any]:
        import_id = str(args.get("import_id") or "").strip()
        agy_conversation_id = str(args.get("agy_conversation_id") or "").strip()
        fallback_to_cli = bool(args.get("fallback_to_cli", False))
        error = str(args.get("error") or "").strip()

        if not import_id:
            raise ValueError("import_id is required")

        if fallback_to_cli:
            record = self.store.get_record(import_id)
            if record is None or record.import_id != import_id:
                raise KeyError(f"Unknown import_id: {import_id}")
            detail = self.store.load_snapshot(record.import_id)
            provider_display = record.provider_display_name or self._provider_display_name(record.provider)
            updated, _thread = spawn_agy_conversation_async(
                detail,
                store=self.store,
                provider_display_name=provider_display,
                mode=record.mode or "full",
                projects_dir=self.projects_dir,
                agy_bin=self.agy_bin,
                runner=self.spawn_runner,
            )
        elif error:
            updated = self.store.update_status(
                import_id,
                status="error",
                error_message=error,
            )
        else:
            updated = self.store.update_status(
                import_id,
                status="ready",
                agy_conversation_id=agy_conversation_id,
                error_message="",
            )

        structured = {
            "schema_version": 1,
            "status": updated.status,
            "import_record": updated.to_dict(),
        }
        return _tool_response(
            f"Import {updated.import_id} updated (status={updated.status}).",
            structured,
        )

    def _tool_app_delete_import(self, args: dict[str, Any]) -> dict[str, Any]:
        import_id = str(args.get("import_id") or "").strip()
        if not import_id:
            raise ValueError("import_id is required")
        deleted = self.store.delete_record(import_id)
        structured = {
            "schema_version": 1,
            "deleted": deleted,
            "import_id": import_id,
        }
        text = f"Deleted import {import_id}." if deleted else f"Import {import_id} not found."
        return _tool_response(text, structured)

    def _handle_tools_call(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            return _jsonrpc_error(req_id, -32602, "tools/call requires a non-empty 'name'")

        raw_args = params.get("arguments")
        if raw_args is None:
            args: dict[str, Any] = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            return _jsonrpc_error(req_id, -32602, "tools/call 'arguments' must be an object")

        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "open_session_importer": self._tool_open_session_importer,
            "render_imported_conversation": self._tool_render_imported_conversation,
            "import_session_context": self._tool_import_session_context,
            "app_list_sessions": self._tool_app_list_sessions,
            "app_get_session_detail": self._tool_app_get_session_detail,
            "app_list_imports": self._tool_app_list_imports,
            "app_start_import": self._tool_app_start_import,
            "app_complete_import": self._tool_app_complete_import,
            "app_delete_import": self._tool_app_delete_import,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return _jsonrpc_error(req_id, -32601, f"Unknown tool: {tool_name}")

        try:
            result = handler(args)
        except (ValueError, KeyError, FileNotFoundError) as exc:
            return _jsonrpc_result(
                req_id,
                _tool_response(str(exc), {"error": str(exc)}, is_error=True),
            )
        except Exception as exc:
            return _jsonrpc_result(
                req_id,
                _tool_response(
                    str(exc) or exc.__class__.__name__,
                    {"error": str(exc) or exc.__class__.__name__},
                    is_error=True,
                ),
            )
        return _jsonrpc_result(req_id, result)

    def _handle_resources_read(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            return _jsonrpc_error(req_id, -32602, "resources/read requires a non-empty 'uri'")
        if uri != APP_RESOURCE_URI:
            return _jsonrpc_error(req_id, -32002, f"Resource not found: {uri}")

        if self.ui_html_path.is_file():
            text = self.ui_html_path.read_text(encoding="utf-8")
        else:
            text = _fallback_ui_html(self.ui_html_path)

        return _jsonrpc_result(
            req_id,
            {
                "contents": [
                    {
                        "uri": APP_RESOURCE_URI,
                        "mimeType": APP_MIME_TYPE,
                        "text": text,
                        "_meta": {"ui": {"prefersBorder": False}},
                    }
                ]
            },
        )

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a single JSON-RPC 2.0 request/notification dict; return None for notifications."""
        if not isinstance(msg, dict):
            return _jsonrpc_error(None, -32600, "Invalid Request: expected JSON object")

        if "id" not in msg:
            return None

        req_id = msg["id"]
        method = msg.get("method")
        if not isinstance(method, str) or not method:
            return _jsonrpc_error(req_id, -32600, "Invalid Request: missing 'method'")

        raw_params = msg.get("params")
        if raw_params is None:
            params: dict[str, Any] = {}
        elif isinstance(raw_params, dict):
            params = raw_params
        else:
            return _jsonrpc_error(req_id, -32602, "Invalid params: expected object")

        if method == "initialize":
            client_proto = params.get("protocolVersion")
            proto = client_proto if isinstance(client_proto, str) and client_proto.strip() else _DEFAULT_PROTOCOL_VERSION
            return _jsonrpc_result(
                req_id,
                {
                    "protocolVersion": proto,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {
                        "name": _SERVER_NAME,
                        "version": _SERVER_VERSION,
                    },
                },
            )

        if method == "ping":
            return _jsonrpc_result(req_id, {})

        if method == "tools/list":
            return _jsonrpc_result(req_id, {"tools": self._tool_definitions()})

        if method == "tools/call":
            return self._handle_tools_call(req_id, params)

        if method == "resources/list":
            return _jsonrpc_result(
                req_id,
                {
                    "resources": [
                        {
                            "uri": APP_RESOURCE_URI,
                            "name": "Session Importer UI",
                            "description": "Interactive MCP App UI for discovering, inspecting, and importing external agent sessions.",
                            "mimeType": APP_MIME_TYPE,
                            "_meta": {"ui": {"prefersBorder": False}},
                        }
                    ]
                },
            )

        if method == "resources/templates/list":
            return _jsonrpc_result(req_id, {"resourceTemplates": []})

        if method == "resources/read":
            return self._handle_resources_read(req_id, params)

        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    def _write_frame(self, stdout: BinaryIO, payload: Any, *, content_length: bool) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if content_length:
            frame = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded
        else:
            frame = encoded + b"\n"
        with self._write_lock:
            stdout.write(frame)
            stdout.flush()

    def serve_stdio(
        self,
        stdin: BinaryIO = sys.stdin.buffer,
        stdout: BinaryIO = sys.stdout.buffer,
    ) -> None:
        """Serve JSON-RPC 2.0 MCP messages over stdio supporting newline and Content-Length framing."""
        while True:
            line = stdin.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue

            content_length_mode = stripped.lower().startswith(b"content-length:")
            if content_length_mode:
                try:
                    length = int(stripped.split(b":", 1)[1].strip())
                    if length < 0:
                        raise ValueError("negative Content-Length")
                except ValueError:
                    while True:
                        hdr_line = stdin.readline()
                        if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                            break
                    self._write_frame(
                        stdout,
                        _jsonrpc_error(None, -32700, "Invalid Content-Length header"),
                        content_length=True,
                    )
                    continue
                while True:
                    hdr_line = stdin.readline()
                    if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                        break
                raw_bytes = _read_exact(stdin, length)
                if len(raw_bytes) < length:
                    break
            else:
                raw_bytes = stripped

            try:
                decoded = raw_bytes.decode("utf-8")
                parsed = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._write_frame(
                    stdout,
                    _jsonrpc_error(None, -32700, f"Parse error: {exc}"),
                    content_length=content_length_mode,
                )
                continue

            if isinstance(parsed, list):
                batch_responses = [
                    resp for item in parsed if (resp := self.handle_message(item)) is not None
                ]
                if batch_responses:
                    self._write_frame(stdout, batch_responses, content_length=content_length_mode)
            else:
                response = self.handle_message(parsed)
                if response is not None:
                    self._write_frame(stdout, response, content_length=content_length_mode)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for launching the stdio MCP server."""
    parser = argparse.ArgumentParser(
        prog="mcp_server.py",
        description="Stdio MCP JSON-RPC 2.0 server for Antigravity Session Importer.",
    )
    parser.add_argument("--data-dir", default=None, help="Persistent import store directory.")
    parser.add_argument("--ui-html", default=None, help="Path to built single-file app.html UI bundle.")
    parser.add_argument("--projects-dir", default=None, help="Antigravity projects directory override.")
    args = parser.parse_args(argv)

    store = ImportedSessionStore(data_dir=Path(args.data_dir) if args.data_dir else None)
    server = SessionImporterMCPServer(
        store=store,
        ui_html_path=Path(args.ui_html) if args.ui_html else None,
        projects_dir=Path(args.projects_dir) if args.projects_dir else None,
    )
    server.serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
