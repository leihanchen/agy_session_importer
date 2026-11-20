"""Unit tests for the Claude Code session provider (DAG reconstruction, compaction, and filtering)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "skills" / "resume-session" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.providers.base import ProviderReadError
from importer.providers.claude import ClaudeProvider
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


class TestClaudeProvider(unittest.TestCase):
    """Tests for Task 4 ClaudeProvider discovery, DAG reconstruction, compaction, and safety."""

    def test_claude_discovery_titles_and_history_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude_home"
            proj = root / "projects" / "-Users-mac-work-payments"

            sid_custom = "11111111-1111-4111-8111-111111111111"
            sid_ai = "22222222-2222-4222-8222-222222222222"
            sid_summary = "33333333-3333-4333-8333-333333333333"
            sid_prompt = "44444444-4444-4444-8444-444444444444"
            sid_history = "55555555-5555-4555-8555-555555555555"
            sid_subagent = "99999999-9999-4999-8999-999999999999"

            # 1. custom-title > ai-title > summary > prompt
            _write_jsonl(
                proj / f"{sid_custom}.jsonl",
                [
                    {
                        "type": "user",
                        "uuid": "c-u1",
                        "parentUuid": None,
                        "sessionId": sid_custom,
                        "cwd": "/Users/mac/work/payments-api",
                        "gitBranch": "feat/stripe-v2",
                        "timestamp": "2026-11-20T08:00:00Z",
                        "message": {"role": "user", "content": "First user prompt"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "c-a1",
                        "parentUuid": "c-u1",
                        "sessionId": sid_custom,
                        "timestamp": "2026-11-20T08:00:10Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Working on it."}]},
                    },
                    {"type": "summary", "summary": "Summary title", "leafUuid": "c-a1"},
                    {"type": "ai-title", "aiTitle": "AI generated title", "sessionId": sid_custom},
                    {"type": "custom-title", "customTitle": "Pinned custom title", "sessionId": sid_custom},
                    {"type": "last-prompt", "lastPrompt": "First user prompt", "leafUuid": "c-a1", "sessionId": sid_custom},
                ],
            )

            # 2. ai-title > summary > prompt
            _write_jsonl(
                proj / f"{sid_ai}.jsonl",
                [
                    {
                        "type": "user",
                        "uuid": "a-u1",
                        "parentUuid": None,
                        "sessionId": sid_ai,
                        "cwd": "/Users/mac/work/payments-api",
                        "gitBranch": "fix/retry",
                        "timestamp": "2026-11-20T08:10:00Z",
                        "message": {"role": "user", "content": "Prompt for AI title"},
                    },
                    {"type": "summary", "summary": "Summary fallback", "leafUuid": "a-u1"},
                    {"type": "ai-title", "aiTitle": "AI wins over summary", "sessionId": sid_ai},
                    {"type": "last-prompt", "lastPrompt": "Prompt for AI title", "leafUuid": "a-u1", "sessionId": sid_ai},
                ],
            )

            # 3. summary matching active leafUuid wins over later summary on an abandoned leaf
            _write_jsonl(
                proj / f"{sid_summary}.jsonl",
                [
                    {
                        "type": "user",
                        "uuid": "s-u1",
                        "parentUuid": None,
                        "sessionId": sid_summary,
                        "cwd": "/Users/mac/work/payments-api",
                        "gitBranch": "main",
                        "timestamp": "2026-11-20T08:20:00Z",
                        "message": {"role": "user", "content": "Summarize active branch"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "s-a1",
                        "parentUuid": "s-u1",
                        "sessionId": sid_summary,
                        "timestamp": "2026-11-20T08:20:05Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Active leaf."}]},
                    },
                    {
                        "type": "assistant",
                        "uuid": "s-abandoned",
                        "parentUuid": "s-u1",
                        "sessionId": sid_summary,
                        "timestamp": "2026-11-20T08:20:08Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Dead leaf."}]},
                    },
                    {"type": "summary", "summary": "Active leaf summary", "leafUuid": "s-a1"},
                    {"type": "summary", "summary": "Abandoned leaf summary", "leafUuid": "s-abandoned"},
                    {"type": "last-prompt", "lastPrompt": "Summarize active branch", "leafUuid": "s-a1", "sessionId": sid_summary},
                ],
            )

            # 4. first human prompt + malformed JSONL line + folder fallback absent because cwd present
            _write_jsonl(
                proj / f"{sid_prompt}.jsonl",
                [
                    "{not valid json",
                    {
                        "type": "user",
                        "uuid": "p-u1",
                        "parentUuid": None,
                        "sessionId": sid_prompt,
                        "cwd": "/Users/mac/work/payments-api",
                        "gitBranch": "chore/cleanup",
                        "timestamp": "2026-11-20T08:30:00Z",
                        "message": {"role": "user", "content": "First human prompt title"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "p-a1",
                        "parentUuid": "p-u1",
                        "sessionId": sid_prompt,
                        "timestamp": "2026-11-20T08:30:10Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Cleaned."}]},
                    },
                    "{also-bad",
                ],
            )

            # 5. JSONL lacking titles and cwd -> enriched from history.jsonl
            _write_jsonl(
                proj / f"{sid_history}.jsonl",
                [
                    {
                        "type": "assistant",
                        "uuid": "h-a1",
                        "parentUuid": None,
                        "sessionId": sid_history,
                        "timestamp": "2026-11-20T08:40:05Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Recovered."}]},
                    },
                ],
            )

            # 6. Subagent JSONLs must be ignored as top-level sessions
            _write_jsonl(
                proj / "subagents" / f"{sid_subagent}.jsonl",
                [
                    {
                        "type": "user",
                        "uuid": "sub-1",
                        "parentUuid": None,
                        "sessionId": sid_subagent,
                        "cwd": "/Users/mac/work/payments-api",
                        "timestamp": "2026-11-20T08:50:00Z",
                        "message": {"role": "user", "content": "Subagent task"},
                    }
                ],
            )
            _write_jsonl(
                proj / sid_custom / "subagents" / "agent-abc123.jsonl",
                [
                    {
                        "type": "user",
                        "uuid": "sub-2",
                        "parentUuid": None,
                        "sessionId": "agent-abc123",
                        "cwd": "/Users/mac/work/payments-api",
                        "timestamp": "2026-11-20T08:51:00Z",
                        "message": {"role": "user", "content": "Nested subagent task"},
                    }
                ],
            )

            _write_jsonl(
                root / "history.jsonl",
                [
                    "{bad-history-json",
                    {
                        "display": "Enriched history display",
                        "pastedContents": {},
                        "project": "/Users/mac/work/payments-api",
                        "sessionId": sid_history,
                        "timestamp": 1795164000000,
                    },
                    {
                        "display": "Should not override custom title",
                        "pastedContents": {},
                        "project": "/Users/mac/work/other",
                        "sessionId": sid_custom,
                        "timestamp": 1795164060000,
                    },
                ],
            )

            provider = ClaudeProvider(root=root)
            self.assertTrue(provider.is_available())

            sessions, list_warnings = provider.list_sessions()
            by_id = {s.session_id: s for s in sessions}

            self.assertEqual(set(by_id.keys()), {sid_custom, sid_ai, sid_summary, sid_prompt, sid_history})
            self.assertNotIn(sid_subagent, by_id)
            self.assertNotIn("agent-abc123", by_id)

            self.assertEqual(by_id[sid_custom].title, "Pinned custom title")
            self.assertEqual(by_id[sid_custom].cwd, os.path.realpath("/Users/mac/work/payments-api"))
            self.assertEqual(by_id[sid_custom].git_branch, "feat/stripe-v2")
            self.assertEqual(by_id[sid_custom].source_kind, "project_jsonl")
            self.assertEqual(by_id[sid_custom].warnings, [])

            self.assertEqual(by_id[sid_ai].title, "AI wins over summary")
            self.assertEqual(by_id[sid_summary].title, "Active leaf summary")
            self.assertEqual(by_id[sid_prompt].title, "First human prompt title")
            self.assertTrue(any("malformed" in w for w in by_id[sid_prompt].warnings))
            self.assertIn("claude_malformed_jsonl_lines:2", by_id[sid_prompt].warnings)
            self.assertTrue(any("malformed" in w for w in list_warnings))

            self.assertEqual(by_id[sid_history].title, "Enriched history display")
            self.assertEqual(by_id[sid_history].first_user_prompt, "Enriched history display")
            self.assertEqual(by_id[sid_history].last_user_prompt, "Enriched history display")
            self.assertEqual(by_id[sid_history].cwd, os.path.realpath("/Users/mac/work/payments-api"))

            # Loading a healthy session by ID must not inherit warnings from corrupt sid_prompt or history.jsonl.
            clean_detail = provider.load_session(sid_custom)
            self.assertEqual(clean_detail.warnings, [])
            self.assertEqual(clean_detail.summary.warnings, [])

            bad_detail = provider.load_session(sid_prompt)
            self.assertIn("claude_malformed_jsonl_lines:2", bad_detail.warnings)

    def test_claude_compaction_logical_parent_and_intermediate_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude_home"
            sid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            jsonl = root / "projects" / "-tmp-app" / f"{sid}.jsonl"

            _write_jsonl(
                jsonl,
                [
                    {
                        "type": "user",
                        "uuid": "u1",
                        "parentUuid": None,
                        "sessionId": sid,
                        "cwd": tmp,
                        "gitBranch": "main",
                        "timestamp": "2026-11-20T09:00:00Z",
                        "message": {"role": "user", "content": "Turn 1: Implement OAuth login"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "parentUuid": "u1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T09:00:10Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Implemented OAuth callback."}]},
                    },
                    {
                        "type": "progress",
                        "uuid": "prog1",
                        "parentUuid": "a1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T09:00:15Z",
                        "data": {"type": "hook_progress", "hookName": "postTool"},
                    },
                    {
                        "type": "user",
                        "uuid": "meta1",
                        "parentUuid": "prog1",
                        "isMeta": True,
                        "sessionId": sid,
                        "timestamp": "2026-11-20T09:00:20Z",
                        "message": {"role": "user", "content": "Local command meta wrapper"},
                    },
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "uuid": "cb1",
                        "parentUuid": None,
                        "logicalParentUuid": "meta1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T09:01:00Z",
                        "compactMetadata": {"trigger": "auto", "preTokens": 120000},
                    },
                    {
                        "type": "user",
                        "uuid": "cs1",
                        "parentUuid": "cb1",
                        "isCompactSummary": True,
                        "sessionId": sid,
                        "timestamp": "2026-11-20T09:01:01Z",
                        "message": {
                            "role": "user",
                            "content": "This session is being continued from a previous conversation...",
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "u2",
                        "parentUuid": "cs1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T09:02:00Z",
                        "message": {"role": "user", "content": "Turn 2: Add refresh token tests"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a2",
                        "parentUuid": "u2",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T09:02:15Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Added refresh token tests."}]},
                    },
                    {"type": "last-prompt", "lastPrompt": "Turn 2: Add refresh token tests", "leafUuid": "a2", "sessionId": sid},
                ],
            )

            provider = ClaudeProvider(root=root)
            detail = provider.load_session(sid)

            self.assertEqual(len(detail.turns), 2)
            self.assertEqual(detail.summary.turn_count, 2)
            self.assertEqual(detail.turns[0].user_text, "Turn 1: Implement OAuth login")
            self.assertEqual(detail.turns[0].assistant_text, "Implemented OAuth callback.")
            self.assertFalse(detail.turns[0].compaction_before)

            self.assertEqual(detail.turns[1].user_text, "Turn 2: Add refresh token tests")
            self.assertEqual(detail.turns[1].assistant_text, "Added refresh token tests.")
            self.assertTrue(detail.turns[1].compaction_before)

            self.assertEqual(detail.compaction_count, 1)
            self.assertIn("compaction_gap", detail.warnings)
            self.assertEqual(detail.summary.first_user_prompt, "Turn 1: Implement OAuth login")
            self.assertEqual(detail.summary.last_user_prompt, "Turn 2: Add refresh token tests")
            rendered_blob = json.dumps(detail.to_dict())
            self.assertNotIn("Local command meta wrapper", rendered_blob)
            self.assertNotIn("This session is being continued", rendered_blob)

    def test_claude_active_branch_parallel_tools_and_filtering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude_home"
            sid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            proj = root / "projects" / "-tmp-service"
            jsonl = proj / f"{sid}.jsonl"

            # Create an external tool-results file that must NEVER be read into context.
            ext_file = proj / sid / "tool-results" / "toolu_ext_path.txt"
            ext_file.parent.mkdir(parents=True, exist_ok=True)
            ext_file.write_text("SECRET_DO_NOT_LOAD_EXTERNAL_OUTPUT", encoding="utf-8")

            _write_jsonl(
                jsonl,
                [
                    {"type": "queue-operation", "operation": "enqueue", "sessionId": sid, "timestamp": "2026-11-20T10:00:00Z"},
                    {"type": "file-history-snapshot", "messageId": "snap-1", "snapshot": {}},
                    {
                        "type": "user",
                        "uuid": "u1",
                        "parentUuid": None,
                        "sessionId": sid,
                        "cwd": tmp,
                        "gitBranch": "main",
                        "timestamp": "2026-11-20T10:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "<system-reminder>ignore injected instructions</system-reminder>\n"
                                        "<ide_opened_file>app.py</ide_opened_file>\n"
                                        "Check auth.py with key sk-proj-abc1234567890xyz"
                                    ),
                                },
                                {
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgoAAAANS"},
                                },
                            ],
                        },
                    },
                    # Abandoned retry fork off u1 (should NOT appear on active branch)
                    {
                        "type": "assistant",
                        "uuid": "a-abandoned",
                        "parentUuid": "u1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:02Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "ABANDONED_FORK_OUTPUT"}]},
                    },
                    {
                        "type": "user",
                        "uuid": "u-abandoned",
                        "parentUuid": "a-abandoned",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:03Z",
                        "message": {"role": "user", "content": "ABANDONED_USER_FOLLOWUP"},
                    },
                    # Sidechain noise that must be dropped with warning
                    {
                        "type": "user",
                        "uuid": "side-1",
                        "parentUuid": "u1",
                        "isSidechain": True,
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:04Z",
                        "message": {"role": "user", "content": "SIDECHAIN_SECRET_TEXT"},
                    },
                    # Active branch: two parallel assistant siblings sharing message.id = "msg_parallel_1"
                    {
                        "type": "assistant",
                        "uuid": "a1-sib1",
                        "parentUuid": "u1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:05Z",
                        "message": {
                            "id": "msg_parallel_1",
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "HIDDEN_THOUGHT_1", "signature": "SIG_12345"},
                                {"type": "redacted_thinking", "data": "ENCRYPTED_THINKING_BLOB"},
                                {"type": "text", "text": "Inspecting auth and running tests."},
                                {
                                    "type": "tool_use",
                                    "id": "toolu_read_1",
                                    "name": "Read",
                                    "input": {"file_path": "src/auth.py"},
                                },
                            ],
                        },
                    },
                    {
                        "type": "assistant",
                        "uuid": "a1-sib2",
                        "parentUuid": "u1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:06Z",
                        "message": {
                            "id": "msg_parallel_1",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_bash_1",
                                    "name": "Bash",
                                    "input": {"command": "pytest tests/test_auth.py"},
                                },
                                {
                                    "type": "tool_use",
                                    "id": "toolu_ext_repl",
                                    "name": "Bash",
                                    "input": {"command": "cat big.log"},
                                },
                                {
                                    "type": "tool_use",
                                    "id": "toolu_ext_persisted",
                                    "name": "Bash",
                                    "input": {"command": "cat huge.log"},
                                },
                                {
                                    "type": "tool_use",
                                    "id": "toolu_ext_path",
                                    "name": "Read",
                                    "input": {"file_path": "huge.txt"},
                                },
                                {
                                    "type": "tool_use",
                                    "id": "toolu_ext_cleared",
                                    "name": "Read",
                                    "input": {"file_path": "old.txt"},
                                },
                            ],
                        },
                    },
                    # Orphaned parallel tool_result user record pointing to a1-sib1
                    {
                        "type": "user",
                        "uuid": "tr-sib1",
                        "parentUuid": "a1-sib1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:07Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "tool_use_id": "toolu_read_1", "content": "def login(): pass"}
                            ],
                        },
                    },
                    # Main branch tool_result user record pointing to a1-sib2
                    {
                        "type": "user",
                        "uuid": "tr-sib2",
                        "parentUuid": "a1-sib2",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:08Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_bash_1",
                                    "is_error": True,
                                    "content": [{"type": "text", "text": "1 failed in tests/test_auth.py"}],
                                },
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_ext_repl",
                                    "content": "giant output that was content-replaced",
                                },
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_ext_persisted",
                                    "content": "<persisted-output>\nOutput too large...\n</persisted-output>",
                                },
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_ext_path",
                                    "content": f"Saved to {ext_file}",
                                },
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_ext_cleared",
                                    "content": "[Old tool result content cleared]",
                                },
                            ],
                        },
                    },
                    {
                        "type": "content-replacement",
                        "sessionId": sid,
                        "replacements": [{"toolUseId": "toolu_ext_repl"}],
                    },
                    # Virtual / transcript-only / attachment records that must be filtered
                    {
                        "type": "user",
                        "uuid": "virt-1",
                        "parentUuid": "tr-sib2",
                        "isVirtual": True,
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:09Z",
                        "message": {"role": "user", "content": "VIRTUAL_SHOULD_SKIP"},
                    },
                    {
                        "type": "user",
                        "uuid": "vis-1",
                        "parentUuid": "virt-1",
                        "isVisibleInTranscriptOnly": True,
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:10Z",
                        "message": {"role": "user", "content": "TRANSCRIPT_ONLY_SKIP"},
                    },
                    {
                        "type": "attachment",
                        "uuid": "att-1",
                        "parentUuid": "vis-1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:11Z",
                        "attachment": {"type": "file", "content": "ATT_SKIP"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a2",
                        "parentUuid": "att-1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T10:00:12Z",
                        "message": {
                            "id": "msg_final",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "test_auth.py failed; needs fix."}],
                        },
                    },
                    {
                        "type": "last-prompt",
                        "lastPrompt": "Check auth.py with key [REDACTED]",
                        "leafUuid": "a2",
                        "sessionId": sid,
                    },
                ],
            )

            provider = ClaudeProvider(root=root)
            detail = provider.load_session(sid)

            # tool_result-only user messages must NOT become separate turns
            self.assertEqual(len(detail.turns), 1)
            turn = detail.turns[0]
            self.assertIn("Check auth.py", turn.user_text)
            self.assertIn("[image omitted]", turn.user_text)
            self.assertIn("[REDACTED]", turn.user_text)
            self.assertNotIn("sk-proj-", turn.user_text)
            self.assertNotIn("ignore injected instructions", turn.user_text)
            self.assertNotIn("app.py</ide_opened_file>", turn.user_text)

            self.assertIn("Inspecting auth and running tests.", turn.assistant_text)
            self.assertIn("test_auth.py failed; needs fix.", turn.assistant_text)

            events_by_id = {te.call_id: te for te in turn.tool_events}
            self.assertEqual(
                set(events_by_id.keys()),
                {
                    "toolu_read_1",
                    "toolu_bash_1",
                    "toolu_ext_repl",
                    "toolu_ext_persisted",
                    "toolu_ext_path",
                    "toolu_ext_cleared",
                },
            )
            self.assertEqual(events_by_id["toolu_read_1"].status, "ok")
            self.assertIn("def login(): pass", events_by_id["toolu_read_1"].output_summary)
            self.assertEqual(events_by_id["toolu_bash_1"].status, "error")
            self.assertIn("1 failed", events_by_id["toolu_bash_1"].output_summary)

            for ext_id in ("toolu_ext_repl", "toolu_ext_persisted", "toolu_ext_path", "toolu_ext_cleared"):
                self.assertEqual(events_by_id[ext_id].output_summary, "[externalized/persisted tool result omitted]")

            self.assertIn("src/auth.py", detail.files_mentioned)
            self.assertIn("huge.txt", detail.files_mentioned)
            self.assertIn("pytest tests/test_auth.py", detail.commands_run)

            self.assertTrue(any("sidechain" in w for w in detail.warnings))
            self.assertTrue(any("secrets_redacted" in w for w in detail.warnings))
            self.assertTrue(any("externalized" in w for w in detail.warnings))

            serialized = json.dumps(detail.to_dict())
            for forbidden in (
                "ABANDONED_FORK_OUTPUT",
                "ABANDONED_USER_FOLLOWUP",
                "SIDECHAIN_SECRET_TEXT",
                "HIDDEN_THOUGHT_1",
                "SIG_12345",
                "ENCRYPTED_THINKING_BLOB",
                "VIRTUAL_SHOULD_SKIP",
                "TRANSCRIPT_ONLY_SKIP",
                "ATT_SKIP",
                "SECRET_DO_NOT_LOAD_EXTERNAL_OUTPUT",
                "iVBORw0KGgoAAAANS",
            ):
                self.assertNotIn(forbidden, serialized)

    def test_claude_parent_cycle_uses_linear_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude_home"
            sid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            jsonl = root / "projects" / "-tmp-cycle" / f"{sid}.jsonl"

            _write_jsonl(
                jsonl,
                [
                    {
                        "type": "user",
                        "uuid": "u1",
                        "parentUuid": "a1",  # Cycle: u1 -> a1 -> u1
                        "sessionId": sid,
                        "cwd": tmp,
                        "timestamp": "2026-11-20T11:00:00Z",
                        "message": {"role": "user", "content": "Recover despite cycle"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "parentUuid": "u1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T11:00:05Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Recovered safely."}]},
                    },
                    {
                        "type": "user",
                        "uuid": "side-cyc",
                        "parentUuid": "a1",
                        "isSidechain": True,
                        "sessionId": sid,
                        "timestamp": "2026-11-20T11:00:06Z",
                        "message": {"role": "user", "content": "DROP_SIDECHAIN"},
                    },
                ],
            )

            provider = ClaudeProvider(root=root)
            detail = provider.load_session(sid)

            self.assertIn("linear_fallback_used", detail.warnings)
            self.assertEqual(len(detail.turns), 1)
            self.assertEqual(detail.turns[0].user_text, "Recover despite cycle")
            self.assertEqual(detail.turns[0].assistant_text, "Recovered safely.")
            self.assertNotIn("DROP_SIDECHAIN", json.dumps(detail.to_dict()))

    def test_claude_registry_prefix_explicit_path_and_searchable_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude_home"
            sid1 = "dddddddd-1111-4ddd-8ddd-dddddddddddd"
            sid2 = "dddddddd-2222-4ddd-8ddd-dddddddddddd"
            proj = root / "projects" / "-tmp-demo"

            _write_jsonl(
                proj / f"{sid1}.jsonl",
                [
                    {
                        "type": "user",
                        "uuid": "d1-u1",
                        "parentUuid": None,
                        "sessionId": sid1,
                        "cwd": tmp,
                        "timestamp": "2026-11-20T12:00:00Z",
                        "message": {"role": "user", "content": "Investigate webhook signature bug"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "d1-a1",
                        "parentUuid": "d1-u1",
                        "sessionId": sid1,
                        "timestamp": "2026-11-20T12:00:05Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "SECRET_THINKING_DO_NOT_INDEX"},
                                {"type": "text", "text": "Found HMAC mismatch with token ghp_1234567890abcdefghijklmnop"},
                            ],
                        },
                    },
                ],
            )
            _write_jsonl(
                proj / f"{sid2}.jsonl",
                [
                    {
                        "type": "user",
                        "uuid": "d2-u1",
                        "parentUuid": None,
                        "sessionId": sid2,
                        "cwd": tmp,
                        "timestamp": "2026-11-20T12:01:00Z",
                        "message": {"role": "user", "content": "Second session"},
                    }
                ],
            )

            # Standalone external Claude JSONL path outside root
            external = Path(tmp) / "exported-transcript.jsonl"
            _write_jsonl(
                external,
                [
                    {
                        "type": "user",
                        "uuid": "e-u1",
                        "parentUuid": None,
                        "sessionId": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                        "cwd": tmp,
                        "timestamp": "2026-11-20T12:05:00Z",
                        "message": {"role": "user", "content": "External transcript prompt"},
                    }
                ],
            )

            registry = create_default_registry({"claude": root})
            claude_prov = registry.get("cc")
            self.assertIsInstance(claude_prov, ClaudeProvider)
            self.assertEqual(claude_prov.name, "claude")
            self.assertTrue(claude_prov.can_handle_path(external))
            self.assertFalse(claude_prov.can_handle_path(Path(tmp) / "codex_home" / "history.jsonl"))

            # Unique prefix works; ambiguous prefix raises ProviderReadError
            detail1 = claude_prov.load_session("dddddddd-1111")
            self.assertEqual(detail1.summary.session_id, sid1)
            with self.assertRaises(ProviderReadError):
                claude_prov.load_session("dddddddd")

            ext_detail = claude_prov.load_session(str(external))
            self.assertEqual(ext_detail.turns[0].user_text, "External transcript prompt")

            searchable = claude_prov.extract_searchable_text(detail1.summary)
            self.assertIn("webhook signature", searchable)
            self.assertIn("HMAC mismatch", searchable)
            self.assertNotIn("SECRET_THINKING_DO_NOT_INDEX", searchable)
            self.assertNotIn("ghp_", searchable)
            self.assertIn("[REDACTED]", searchable)

    def test_claude_snip_and_preserved_segment_stitching(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude_home"
            sid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
            jsonl = root / "projects" / "-tmp-snip" / f"{sid}.jsonl"

            _write_jsonl(
                jsonl,
                [
                    {
                        "type": "user",
                        "uuid": "u1",
                        "parentUuid": None,
                        "sessionId": sid,
                        "cwd": tmp,
                        "timestamp": "2026-11-20T13:00:00Z",
                        "message": {"role": "user", "content": "Keep pre-snip Turn 1"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "parentUuid": "u1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T13:00:05Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Pre-snip reply."}]},
                    },
                    # Imagine nodes u_snipped and a_snipped were removed from disk, and u2 still points to a_snipped
                    {
                        "type": "system",
                        "subtype": "snip",
                        "uuid": "snip-1",
                        "parentUuid": "a1",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T13:01:00Z",
                        "snipMetadata": {"removedUuids": ["u_snipped", "a_snipped"]},
                    },
                    {
                        "type": "user",
                        "uuid": "u2",
                        "parentUuid": "a_snipped",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T13:02:00Z",
                        "message": {"role": "user", "content": "Post-snip Turn 2"},
                    },
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "uuid": "cb2",
                        "parentUuid": None,
                        "logicalParentUuid": "u2",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T13:03:00Z",
                        "compactMetadata": {
                            "preservedSegment": {
                                "headUuid": "pres-u",
                                "anchorUuid": "cb2",
                                "tailUuid": "pres-a",
                            }
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "pres-u",
                        "parentUuid": None,
                        "sessionId": sid,
                        "timestamp": "2026-11-20T13:03:05Z",
                        "message": {"role": "user", "content": "Preserved segment turn"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "pres-a",
                        "parentUuid": "pres-u",
                        "sessionId": sid,
                        "timestamp": "2026-11-20T13:03:10Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Preserved answer."}]},
                    },
                    {"type": "last-prompt", "lastPrompt": "Preserved segment turn", "leafUuid": "pres-a", "sessionId": sid},
                ],
            )

            provider = ClaudeProvider(root=root)
            detail = provider.load_session(sid)

            self.assertNotIn("linear_fallback_used", detail.warnings)
            self.assertEqual([t.user_text for t in detail.turns], [
                "Keep pre-snip Turn 1",
                "Post-snip Turn 2",
                "Preserved segment turn",
            ])
            self.assertTrue(detail.turns[2].compaction_before)
            self.assertIn("compaction_gap", detail.warnings)


if __name__ == "__main__":
    unittest.main()
