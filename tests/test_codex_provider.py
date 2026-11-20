"""Unit tests for the Codex session provider (rollout JSONL, SQLite, and metadata fallback)."""

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "skills" / "resume-session" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.models import normalize_timestamp
from importer.providers.codex import CodexProvider
from importer.providers.registry import create_default_registry


def _write_jsonl(path: Path, records: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for rec in records:
        if isinstance(rec, str):
            lines.append(rec)
        else:
            lines.append(json.dumps(rec))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _create_state_db(path: Path, rows: list[dict], *, wal_uncheckpointed_rows: list[dict] | None = None) -> sqlite3.Connection | None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            cwd TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            git_branch TEXT,
            first_user_message TEXT NOT NULL DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0,
            name TEXT,
            preview TEXT NOT NULL DEFAULT '',
            recency_at INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER,
            updated_at_ms INTEGER,
            recency_at_ms INTEGER DEFAULT 0
        )
        """
    )
    insert_sql = """
        INSERT INTO threads (
            id, rollout_path, created_at, updated_at, cwd, title, git_branch,
            first_user_message, archived, name, preview, recency_at,
            created_at_ms, updated_at_ms, recency_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def _params(r: dict) -> tuple:
        return (
            r["id"],
            r.get("rollout_path", ""),
            r.get("created_at", 0),
            r.get("updated_at", 0),
            r.get("cwd", ""),
            r.get("title", ""),
            r.get("git_branch"),
            r.get("first_user_message", ""),
            r.get("archived", 0),
            r.get("name"),
            r.get("preview", ""),
            r.get("recency_at", 0),
            r.get("created_at_ms"),
            r.get("updated_at_ms"),
            r.get("recency_at_ms", 0),
        )

    for row in rows:
        conn.execute(insert_sql, _params(row))
    conn.commit()

    if wal_uncheckpointed_rows:
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        for row in wal_uncheckpointed_rows:
            conn.execute(insert_sql, _params(row))
        conn.commit()
        return conn

    conn.close()
    return None


def _create_history_db(
    path: Path,
    turns: list[tuple[str, str, int, str, int | None, int | None]],
    items: list[tuple[str, str, str, int, int, str, dict]],
) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        """
        CREATE TABLE thread_turns (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            rollout_ordinal INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_json TEXT,
            started_at INTEGER,
            completed_at INTEGER,
            PRIMARY KEY (thread_id, turn_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE thread_items (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            rollout_ordinal INTEGER NOT NULL,
            created_at_ms INTEGER NOT NULL,
            item_type TEXT NOT NULL DEFAULT '',
            item_json TEXT NOT NULL,
            PRIMARY KEY (thread_id, turn_id, item_id)
        )
        """
    )
    for turn in turns:
        conn.execute(
            "INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, status, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
            turn,
        )
    for thread_id, turn_id, item_id, ordinal, created_ms, item_type, item_obj in items:
        conn.execute(
            "INSERT INTO thread_items (thread_id, turn_id, item_id, rollout_ordinal, created_at_ms, item_type, item_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (thread_id, turn_id, item_id, ordinal, created_ms, item_type, json.dumps(item_obj)),
        )
    conn.commit()
    conn.close()


class TestCodexProvider(unittest.TestCase):
    """Tests for CodexProvider rollout JSONL, SQLite history, and metadata fallback."""

    def test_codex_rollout_jsonl_full_parsing_and_filtering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex_home"
            repo_dir = Path(tmp) / "workspace"
            repo_dir.mkdir(parents=True)
            session_id = "019a0000-1111-7222-8333-444455556666"
            rollout_path = root / "sessions" / "2026" / "11" / "20" / f"rollout-2026-11-20T10-00-00-{session_id}.jsonl"

            records = [
                {
                    "timestamp": "2026-11-20T10:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "cwd": str(repo_dir),
                        "timestamp": "2026-11-20T10:00:00Z",
                        "git": {"branch": "feat/auth"},
                        "base_instructions": "SECRET_SYSTEM_PROMPT_NEVER_SERIALIZE",
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn-1", "cwd": str(repo_dir), "model": "gpt-5"},
                },
                {
                    "timestamp": "2026-11-20T10:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "DEVELOPER_INTERNAL_PROMPT"}],
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "system",
                        "content": [{"type": "input_text", "text": "SYSTEM_INTERNAL_PROMPT"}],
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": "turn-1",
                            "content_item_kinds": ["agents_md.instructions", "environments.environment_context"],
                            "content_item_metadata": [{"provenance": {"type": "agents_md"}}, {"provenance": {"type": "harness"}}],
                        },
                        "content": [
                            {"type": "input_text", "text": "# AGENTS.md instructions\n\n<INSTRUCTIONS>ignore me</INSTRUCTIONS>"},
                            {"type": "input_text", "text": "<environment_context><cwd>/x</cwd></environment_context>"},
                        ],
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "<environment_context><cwd>/x</cwd></environment_context>Fix auth bug with key sk-proj-secret1234567890",
                            }
                        ],
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:04Z",
                    "type": "world_state",
                    "payload": {"state": {"secret_world": "drop_me"}},
                },
                {
                    "timestamp": "2026-11-20T10:00:04Z",
                    "type": "token_usage_record",
                    "payload": {"usage": {"total_tokens": 999}},
                },
                {
                    "timestamp": "2026-11-20T10:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "PRIVATE_REASONING_THOUGHT"}],
                        "encrypted_content": "ENCRYPTED_BLOB_XYZ",
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:06Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": "call_fn_1",
                        "namespace": "shell",
                        "name": "exec",
                        "arguments": json.dumps({"cmd": "pytest tests/test_auth.py"}),
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:07Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_fn_1",
                        "output": [{"type": "input_text", "text": "FAILED tests/test_auth.py\nProcess exited with code 1"}],
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:08Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "call_id": "call_patch_1",
                        "name": "apply_patch",
                        "input": (
                            "*** Begin Patch\n"
                            "*** Update File: src/auth.py\n"
                            "@@ -1 +1 @@\n-old\n+new\n"
                            "*** Add File: tests/test_auth.py\n+def test_ok(): pass\n"
                            "*** End Patch"
                        ),
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:09Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call_patch_1",
                        "output": "Success. Updated the following files:\nM src/auth.py\nA tests/test_auth.py",
                    },
                },
                {
                    "timestamp": "2026-11-20T10:00:10Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Updated auth module and added a test."}],
                    },
                },
                {
                    "timestamp": "2026-11-20T10:01:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-2"},
                },
                {
                    "timestamp": "2026-11-20T10:01:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Run the full suite"}],
                    },
                },
                {
                    "timestamp": "2026-11-20T10:01:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "local_shell_call",
                        "call_id": "call_shell_2",
                        "action": {"type": "exec", "command": ["pytest", "-q"]},
                    },
                },
                {
                    "timestamp": "2026-11-20T10:01:05Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "turn_id": "turn-2", "reason": "interrupted"},
                },
            ]
            _write_jsonl(rollout_path, records)

            provider = CodexProvider(root=root)
            self.assertTrue(provider.is_available())
            self.assertTrue(provider.can_handle_path(rollout_path))

            summaries, warnings = provider.list_sessions()
            self.assertEqual(warnings, [])
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].session_id, session_id)
            self.assertEqual(summaries[0].git_branch, "feat/auth")
            self.assertEqual(summaries[0].turn_count, 2)

            detail = provider.load_session(session_id)
            serialized = json.dumps(detail.to_dict())

            for forbidden in (
                "SECRET_SYSTEM_PROMPT_NEVER_SERIALIZE",
                "DEVELOPER_INTERNAL_PROMPT",
                "SYSTEM_INTERNAL_PROMPT",
                "PRIVATE_REASONING_THOUGHT",
                "ENCRYPTED_BLOB_XYZ",
                "secret_world",
                "sk-proj-secret1234567890",
            ):
                self.assertNotIn(forbidden, serialized)

            self.assertEqual(len(detail.turns), 2)
            turn1 = detail.turns[0]
            self.assertIn("Fix auth bug", turn1.user_text)
            self.assertIn("[REDACTED]", turn1.user_text)
            self.assertEqual(turn1.assistant_text, "Updated auth module and added a test.")
            self.assertEqual(len(turn1.tool_events), 2)

            fn_event = turn1.tool_events[0]
            self.assertEqual(fn_event.call_id, "call_fn_1")
            self.assertEqual(fn_event.tool_name, "shell.exec")
            self.assertEqual(fn_event.status, "error")
            self.assertIn("Process exited with code 1", fn_event.output_summary)

            patch_event = turn1.tool_events[1]
            self.assertEqual(patch_event.tool_name, "apply_patch")
            self.assertEqual(patch_event.status, "ok")
            self.assertEqual(patch_event.files_touched, ["src/auth.py", "tests/test_auth.py"])

            self.assertIn("src/auth.py", detail.files_mentioned)
            self.assertIn("tests/test_auth.py", detail.files_mentioned)
            self.assertIn("pytest tests/test_auth.py", detail.commands_run)
            self.assertIn("pytest -q", detail.commands_run)

            turn2 = detail.turns[1]
            self.assertEqual(turn2.tool_events[0].status, "aborted")
            self.assertTrue(any("aborted" in w for w in turn2.warnings))
            self.assertTrue(any(w.startswith("secrets_redacted:") for w in detail.warnings))

            searchable = provider.extract_searchable_text(summaries[0])
            self.assertIn("Updated auth module", searchable)
            self.assertNotIn("PRIVATE_REASONING_THOUGHT", searchable)

    def test_codex_compacted_replacement_history_and_thread_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex_home"
            sid1 = "019a0000-2222-7222-8333-444455556666"
            rollout1 = root / "sessions" / f"rollout-{sid1}.jsonl"

            _write_jsonl(
                rollout1,
                [
                    {
                        "timestamp": "2026-11-20T11:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": sid1, "cwd": tmp, "timestamp": "2026-11-20T11:00:00Z"},
                    },
                    {
                        "timestamp": "2026-11-20T11:00:01Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Stale pre-compaction prompt"}]},
                    },
                    {
                        "timestamp": "2026-11-20T11:05:00Z",
                        "type": "compacted",
                        "payload": {
                            "replacement_history": [
                                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "DROP_COMPACT_DEV"}]},
                                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Compacted Turn 1 goal"}]},
                                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Compacted Turn 1 summary"}]},
                            ]
                        },
                    },
                    {
                        "timestamp": "2026-11-20T11:06:00Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Post-compaction turn 2"}]},
                    },
                    {
                        "timestamp": "2026-11-20T11:06:05Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Kept response"}]},
                    },
                    {
                        "timestamp": "2026-11-20T11:07:00Z",
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Rolled back mistake"}]},
                    },
                    {
                        "timestamp": "2026-11-20T11:07:05Z",
                        "type": "event_msg",
                        "payload": {"type": "thread_rolled_back", "num_turns": 1},
                    },
                    # Also ensure event_msg duplicates are suppressed when response_item:message exists.
                    {
                        "timestamp": "2026-11-20T11:06:00Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "Duplicate legacy prompt"},
                    },
                ],
            )

            # Second session using legacy event_msg only (no response_item:message).
            sid2 = "019a0000-3333-7222-8333-444455556666"
            rollout2 = root / "sessions" / f"rollout-{sid2}.jsonl"
            _write_jsonl(
                rollout2,
                [
                    {
                        "timestamp": "2026-11-20T12:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": sid2, "cwd": tmp, "timestamp": "2026-11-20T12:00:00Z"},
                    },
                    {
                        "timestamp": "2026-11-20T12:00:01Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "Legacy user prompt"},
                    },
                    {
                        "timestamp": "2026-11-20T12:00:02Z",
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "Legacy agent reply"},
                    },
                ],
            )

            provider = CodexProvider(root=root)
            detail1 = provider.load_session(sid1)
            self.assertGreaterEqual(detail1.compaction_count, 1)
            self.assertIn("compaction_gap", detail1.warnings)
            self.assertEqual(len(detail1.turns), 2)
            self.assertEqual(detail1.turns[0].user_text, "Compacted Turn 1 goal")
            self.assertFalse(detail1.turns[0].compaction_before)
            self.assertEqual(detail1.turns[1].user_text, "Post-compaction turn 2")
            self.assertTrue(detail1.turns[1].compaction_before)
            serialized1 = json.dumps(detail1.to_dict())
            self.assertNotIn("Stale pre-compaction prompt", serialized1)
            self.assertNotIn("DROP_COMPACT_DEV", serialized1)
            self.assertNotIn("Rolled back mistake", serialized1)
            self.assertNotIn("Duplicate legacy prompt", serialized1)

            detail2 = provider.load_session(sid2)
            self.assertEqual(len(detail2.turns), 1)
            self.assertEqual(detail2.turns[0].user_text, "Legacy user prompt")
            self.assertEqual(detail2.turns[0].assistant_text, "Legacy agent reply")

    def test_codex_numeric_suffix_and_wal_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex_home"
            root.mkdir(parents=True)

            old_id = "019a0000-0005-7000-8000-000000000005"
            new_id = "019a0000-0010-7000-8000-000000000010"
            wal_id = "019a0000-0011-7000-8000-000000000011"

            _create_state_db(
                root / "state_5.sqlite",
                [
                    {
                        "id": old_id,
                        "created_at": 1776916275,
                        "updated_at": 1776916275,
                        "cwd": tmp,
                        "title": "Old state_5 thread",
                    }
                ],
            )
            writer = _create_state_db(
                root / "state_10.sqlite",
                [
                    {
                        "id": new_id,
                        "created_at": 1776916275,
                        "updated_at": 1776916275,
                        "cwd": tmp,
                        "title": "State 10 main thread",
                    }
                ],
                wal_uncheckpointed_rows=[
                    {
                        "id": wal_id,
                        "created_at": 1776916275000,
                        "updated_at": 1776916275000,
                        "cwd": tmp,
                        "title": "Uncheckpointed WAL thread",
                    }
                ],
            )
            try:
                _create_history_db(
                    root / "thread_history_1.sqlite",
                    turns=[
                        (wal_id, "turn-1", 0, "completed", 1776916275, 1776916280),
                    ],
                    items=[
                        (wal_id, "turn-1", "item-1", 0, 1776916275000, "userMessage", {"type": "userMessage", "content": [{"type": "text", "text": "Check WAL visibility"}]}),
                        (wal_id, "turn-1", "item-2", 1, 1776916276000, "agentMessage", {"type": "agentMessage", "text": "WAL read succeeded"}),
                    ],
                )

                provider = CodexProvider(root=root)
                summaries, warnings = provider.list_sessions()
                self.assertEqual(warnings, [])
                by_id = {s.session_id: s for s in summaries}
                self.assertNotIn(old_id, by_id)
                self.assertIn(new_id, by_id)
                self.assertIn(wal_id, by_id)

                # Unix seconds vs Unix milliseconds normalize identically.
                self.assertEqual(by_id[new_id].created_at, normalize_timestamp(1776916275))
                self.assertEqual(by_id[wal_id].created_at, normalize_timestamp(1776916275000))
                self.assertEqual(by_id[new_id].created_at, by_id[wal_id].created_at)

                detail = provider.load_session(wal_id)
                self.assertEqual(detail.summary.source_kind, "sqlite_history")
                self.assertEqual(len(detail.turns), 1)
                self.assertEqual(detail.turns[0].user_text, "Check WAL visibility")
                self.assertEqual(detail.turns[0].assistant_text, "WAL read succeeded")
            finally:
                if writer is not None:
                    writer.close()

    def test_codex_corrupt_rollout_falls_back_to_sqlite_or_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex_home"
            root.mkdir(parents=True)

            sqlite_id = "019a0000-4444-7000-8000-000000000001"
            meta_id = "019a0000-4444-7000-8000-000000000002"
            corrupt_id = "019a0000-4444-7000-8000-000000000003"
            corrupt_rollout = root / "sessions" / f"rollout-{corrupt_id}.jsonl"
            corrupt_rollout.parent.mkdir(parents=True, exist_ok=True)
            corrupt_rollout.write_text("{not valid json\n", encoding="utf-8")

            _create_state_db(
                root / "state_1.sqlite",
                [
                    {
                        "id": sqlite_id,
                        "rollout_path": str(root / "sessions" / "missing-rollout.jsonl"),
                        "created_at": 1776916275,
                        "updated_at": 1776916300,
                        "cwd": tmp,
                        "title": "SQLite fallback thread",
                    },
                    {
                        "id": meta_id,
                        "rollout_path": str(root / "sessions" / "also-missing.jsonl"),
                        "created_at": 1776916200,
                        "updated_at": 1776916250,
                        "cwd": tmp,
                        "title": "Metadata only thread",
                        "first_user_message": "Initial metadata prompt",
                    },
                    {
                        "id": corrupt_id,
                        "rollout_path": str(corrupt_rollout),
                        "created_at": 1776916210,
                        "updated_at": 1776916260,
                        "cwd": tmp,
                        "title": "Corrupt rollout fallback thread",
                    },
                ],
            )
            _create_history_db(
                root / "thread_history_1.sqlite",
                turns=[
                    (sqlite_id, "t1", 0, "completed", 1776916275, 1776916285),
                    (sqlite_id, "t2", 1, "completed", 1776916290, 1776916300),
                    (corrupt_id, "ct1", 0, "completed", 1776916210, 1776916220),
                ],
                items=[
                    (sqlite_id, "t1", "i1", 0, 1776916275000, "userMessage", {"type": "userMessage", "content": [{"type": "text", "text": "Inspect repo"}, {"type": "image", "url": "data:image/png;base64,AAA"}]}),
                    (sqlite_id, "t1", "i2", 1, 1776916276000, "reasoning", {"type": "reasoning", "summary": ["HIDDEN_SQLITE_REASONING"], "content": ["SECRET_COT"]}),
                    (sqlite_id, "t1", "i3", 2, 1776916277000, "commandExecution", {"type": "commandExecution", "command": "git status", "cwd": tmp, "status": "completed", "aggregatedOutput": "clean", "exitCode": 0}),
                    (sqlite_id, "t1", "i4", 3, 1776916278000, "fileChange", {"type": "fileChange", "changes": [{"path": "app.py", "kind": {"type": "update"}, "diff": "+x"}], "status": "completed"}),
                    (sqlite_id, "t1", "i5", 4, 1776916279000, "mcpToolCall", {"type": "mcpToolCall", "server": "linear", "tool": "get_issue", "status": "failed", "arguments": {"id": "1"}, "result": None, "error": "not found"}),
                    (sqlite_id, "t1", "i6", 5, 1776916280000, "webSearch", {"type": "webSearch", "query": "python sqlite ro mode", "action": {}, "results": [{"title": "SQLite URI"}]}),
                    (sqlite_id, "t1", "i7", 6, 1776916281000, "collabAgentToolCall", {"type": "collabAgentToolCall", "tool": "spawn", "status": "completed", "senderThreadId": sqlite_id, "receiverThreadIds": ["child-1"], "prompt": "check", "agentsStates": {}}),
                    (sqlite_id, "t1", "i8", 7, 1776916282000, "subAgentActivity", {"type": "subAgentActivity", "kind": "finished", "agentThreadId": "child-1", "agentPath": "reviewer"}),
                    (sqlite_id, "t1", "i9", 8, 1776916283000, "imageView", {"type": "imageView", "path": "/tmp/screen.png"}),
                    (sqlite_id, "t1", "i10", 9, 1776916284000, "agentMessage", {"type": "agentMessage", "text": "Checked repo and files."}),
                    (sqlite_id, "t2", "i11", 10, 1776916288000, "contextCompaction", {"type": "contextCompaction", "id": "cmp-1"}),
                    (sqlite_id, "t2", "i12", 11, 1776916290000, "userMessage", {"type": "userMessage", "content": [{"type": "input_text", "text": "Continue after compaction"}]}),
                    (sqlite_id, "t2", "i13", 12, 1776916295000, "agentMessage", {"type": "agentMessage", "text": "Continuing now."}),
                    (corrupt_id, "ct1", "ci1", 0, 1776916210000, "userMessage", {"type": "userMessage", "content": [{"type": "text", "text": "Recovered from corrupt rollout"}]}),
                    (corrupt_id, "ct1", "ci2", 1, 1776916215000, "agentMessage", {"type": "agentMessage", "text": "SQLite fallback worked"}),
                ],
            )

            provider = CodexProvider(root=root)

            # Missing rollout path falls back cleanly to thread_history_1.sqlite
            sql_detail = provider.load_session(sqlite_id)
            self.assertEqual(sql_detail.summary.source_kind, "sqlite_history")
            self.assertNotIn("metadata_only_no_transcript", sql_detail.warnings)
            self.assertEqual(sql_detail.compaction_count, 1)
            self.assertIn("compaction_gap", sql_detail.warnings)
            self.assertEqual(len(sql_detail.turns), 2)
            self.assertIn("Checked repo and files.", provider.extract_searchable_text(sql_detail.summary))

            t1, t2 = sql_detail.turns
            self.assertIn("Inspect repo", t1.user_text)
            self.assertIn("[image omitted]", t1.user_text)
            self.assertEqual(t1.assistant_text, "Checked repo and files.")
            tool_names = [te.tool_name for te in t1.tool_events]
            self.assertEqual(tool_names, ["shell", "file_change", "mcp:linear.get_issue", "web_search", "imageView"])
            self.assertEqual(t1.tool_events[0].status, "ok")
            self.assertEqual(t1.tool_events[1].files_touched, ["app.py"])
            self.assertEqual(t1.tool_events[2].status, "error")
            self.assertEqual(t1.tool_events[3].arguments_summary, "python sqlite ro mode")
            self.assertEqual(t1.tool_events[4].output_summary, "[media omitted]")
            self.assertTrue(any("collab" in note for note in t1.subagent_notes))
            self.assertTrue(any("subagent" in note for note in t1.subagent_notes))
            self.assertIn("git status", sql_detail.commands_run)
            self.assertIn("app.py", sql_detail.files_mentioned)
            self.assertTrue(t2.compaction_before)

            sql_json = json.dumps(sql_detail.to_dict())
            self.assertNotIn("HIDDEN_SQLITE_REASONING", sql_json)
            self.assertNotIn("SECRET_COT", sql_json)

            # Corrupt rollout file emits warning and falls back to SQLite history
            corrupt_detail = provider.load_session(str(corrupt_rollout))
            self.assertEqual(corrupt_detail.summary.source_kind, "sqlite_history")
            self.assertEqual(corrupt_detail.turns[0].user_text, "Recovered from corrupt rollout")
            self.assertTrue(any("malformed" in w or "corrupt" in w for w in corrupt_detail.warnings))

            # Standalone corrupt rollout without SQLite emits warning without crashing
            standalone_corrupt = root / "sessions" / "rollout-standalone-bad.jsonl"
            standalone_corrupt.write_text("{corrupt\n", encoding="utf-8")
            standalone_detail = provider.load_session(str(standalone_corrupt))
            self.assertEqual(standalone_detail.turns, [])
            self.assertTrue(any("corrupt" in w for w in standalone_detail.warnings))

            # Second thread with neither rollout nor thread_items -> metadata_only
            meta_detail = provider.load_session(meta_id)
            self.assertEqual(meta_detail.summary.source_kind, "metadata_only")
            self.assertIn("metadata_only_no_transcript", meta_detail.warnings)
            self.assertIn("metadata_only_no_transcript", meta_detail.summary.warnings)
            self.assertEqual(meta_detail.turns[0].user_text, "Initial metadata prompt")
            self.assertIn("Initial metadata prompt", provider.extract_searchable_text(meta_detail.summary))

            # Corrupt state SQLite emits warning and does not crash list_sessions
            (root / "state_2.sqlite").write_text("not a sqlite db", encoding="utf-8")
            summaries_after_bad_db, db_warnings = provider.list_sessions()
            self.assertTrue(any("codex_state_db_error" in w for w in db_warnings))
            self.assertIsInstance(summaries_after_bad_db, list)
            (root / "state_2.sqlite").unlink()

            # .jsonl.zst warning when zstd binary is unavailable
            zst_path = root / "sessions" / "rollout-zst-test.jsonl.zst"
            zst_path.write_bytes(b"fake-zst")
            with mock.patch.object(shutil, "which", return_value=None):
                zst_detail = provider.load_session(str(zst_path))
            self.assertTrue(any("zstd" in w for w in zst_detail.warnings))

            # If system zstd exists, verify real .jsonl.zst decompression path too
            if shutil.which("zstd"):
                real_zst = root / "sessions" / "rollout-real-019a0000-7777-7000-8000-000000000007.jsonl.zst"
                raw_jsonl = json.dumps({"timestamp": "2026-11-20T13:00:00Z", "type": "session_meta", "payload": {"id": "019a0000-7777-7000-8000-000000000007", "cwd": tmp}}) + "\n" + json.dumps({"timestamp": "2026-11-20T13:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello from zstd"}]}}) + "\n"
                proc = subprocess.run(["zstd", "-q", "-c"], input=raw_jsonl.encode("utf-8"), capture_output=True, check=True)
                real_zst.write_bytes(proc.stdout)
                loaded_zst = provider.load_session(str(real_zst))
                self.assertEqual(len(loaded_zst.turns), 1)
                self.assertEqual(loaded_zst.turns[0].user_text, "Hello from zstd")

    def test_codex_archived_filter_and_session_index_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "codex_home"
            active_id = "019a0000-5555-7000-8000-000000000001"
            arch_id = "019a0000-5555-7000-8000-000000000002"

            active_rollout = root / "sessions" / f"rollout-{active_id}.jsonl"
            arch_rollout = root / "archived_sessions" / f"rollout-{arch_id}.jsonl"

            _write_jsonl(
                active_rollout,
                [
                    {"timestamp": "2026-11-20T09:00:00Z", "type": "session_meta", "payload": {"id": active_id, "cwd": tmp}},
                    {"timestamp": "2026-11-20T09:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Build feature"}]}},
                ],
            )
            _write_jsonl(
                arch_rollout,
                [
                    {"timestamp": "2026-11-19T09:00:00Z", "type": "session_meta", "payload": {"id": arch_id, "cwd": tmp}},
                    {"timestamp": "2026-11-19T09:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Old archived task"}]}},
                ],
            )
            _create_state_db(
                root / "state_1.sqlite",
                [
                    {
                        "id": active_id,
                        "rollout_path": str(active_rollout),
                        "created_at": 1776916000,
                        "updated_at": 1776916010,
                        "cwd": tmp,
                        "title": "",
                        "archived": 0,
                    },
                    {
                        "id": arch_id,
                        "rollout_path": str(arch_rollout),
                        "created_at": 1776816000,
                        "updated_at": 1776816010,
                        "cwd": tmp,
                        "title": "Archived title",
                        "archived": 1,
                    },
                ],
            )
            _write_jsonl(
                root / "session_index.jsonl",
                [
                    {"id": active_id, "thread_name": "Enriched Index Title", "updated_at": "2026-11-20T09:05:00Z"},
                ],
            )

            registry = create_default_registry({"codex": root})
            provider = registry.get("codex")
            self.assertIsInstance(provider, CodexProvider)
            self.assertIs(registry.get("openai-codex"), provider)
            self.assertIs(registry.get("codex-cli"), provider)

            active_only, warnings = provider.list_sessions(include_archived=False)
            self.assertEqual(warnings, [])
            self.assertEqual([s.session_id for s in active_only], [active_id])
            self.assertEqual(active_only[0].title, "Enriched Index Title")
            self.assertFalse(active_only[0].archived)

            with_archived, _ = provider.list_sessions(include_archived=True)
            by_id = {s.session_id: s for s in with_archived}
            self.assertEqual(set(by_id.keys()), {active_id, arch_id})
            self.assertTrue(by_id[arch_id].archived)

            # Non-Codex JSONL files outside Codex root must not be claimed by CodexProvider
            claude_like = Path(tmp) / "claude" / "projects" / "demo" / "abc.jsonl"
            _write_jsonl(
                claude_like,
                [{"type": "user", "sessionId": "abc", "uuid": "u1", "message": {"role": "user", "content": "hi"}}],
            )
            self.assertFalse(provider.can_handle_path(claude_like))


if __name__ == "__main__":
    unittest.main()
