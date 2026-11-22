"""Unit and subprocess stdio tests for skills/resume-session/scripts/mcp_server.py."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "skills" / "resume-session" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.models import NormalizedTurn, SessionDetail, SessionSummary, ToolEvent
from importer.providers.base import SessionProvider
from importer.providers.registry import ProviderRegistry, create_default_registry
from importer.safety import TRUST_BOUNDARY
from importer.store import ImportedSessionStore
import mcp_server
from mcp_server import APP_MIME_TYPE, APP_RESOURCE_URI, SessionImporterMCPServer

_MCP_SCRIPT_PATH = Path(_SCRIPTS_DIR) / "mcp_server.py"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(rec) for rec in records) + "\n", encoding="utf-8")


def _build_codex_rollout(
    codex_home: Path,
    *,
    session_id: str,
    cwd: str,
    branch: str = "feat/mcp-ui",
    turns: list[tuple[str, str]] | None = None,
    updated_ts: str = "2026-11-20T10:00:00Z",
) -> Path:
    rollout = codex_home / "sessions" / "2026" / "11" / "20" / f"rollout-2026-11-20T10-00-00-{session_id}.jsonl"
    turns = turns or [
        ("Implement MCP server tool suite.", "Added JSON-RPC 2.0 handlers and 8 MCP tools."),
        ("Verify stdio framing.", "Added newline and Content-Length framing."),
    ]
    records: list[dict] = [
        {
            "timestamp": "2026-11-20T09:50:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": cwd,
                "timestamp": "2026-11-20T09:50:00Z",
                "git": {"branch": branch},
            },
        }
    ]
    for idx, (user_msg, asst_msg) in enumerate(turns, start=1):
        ts_min = min(59, 49 + idx)
        ts_base = f"2026-11-20T09:{ts_min:02d}"
        records.append(
            {
                "timestamp": f"{ts_base}:00Z" if idx < len(turns) else updated_ts,
                "type": "turn_context",
                "payload": {"turn_id": f"turn-{idx}", "cwd": cwd},
            }
        )
        records.append(
            {
                "timestamp": f"{ts_base}:01Z" if idx < len(turns) else updated_ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_msg}],
                },
            }
        )
        records.append(
            {
                "timestamp": f"{ts_base}:02Z" if idx < len(turns) else updated_ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": asst_msg}],
                },
            }
        )
    _write_jsonl(rollout, records)
    return rollout


def _build_agy_project(projects_dir: Path, *, project_id: str, project_name: str, folder_path: Path) -> None:
    projects_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": project_id,
        "name": project_name,
        "projectResources": {
            "resources": [{"folderUri": folder_path.resolve().as_uri()}]
        },
    }
    (projects_dir / f"{project_id}.json").write_text(json.dumps(payload), encoding="utf-8")


class _SyntheticProvider(SessionProvider):
    """In-memory synthetic provider for testing large payloads and custom provider discovery."""

    def __init__(
        self,
        *,
        name: str = "cursor",
        display_name: str = "Cursor",
        aliases: tuple[str, ...] = ("cur",),
        details: list[SessionDetail] | None = None,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self.aliases = aliases
        self._details = {d.summary.session_id: d for d in (details or [])}

    def is_available(self) -> bool:
        return True

    def list_sessions(self, *, include_archived: bool = False) -> tuple[list[SessionSummary], list[str]]:
        summaries = [
            d.summary for d in self._details.values() if include_archived or not d.summary.archived
        ]
        return summaries, []

    def can_handle_path(self, path: Path) -> bool:
        return any(d.summary.source_path == str(path) for d in self._details.values())

    def load_session(self, ref: str, *, max_tool_chars: int = 600) -> SessionDetail:
        _ = max_tool_chars
        if ref in self._details:
            return self._details[ref]
        for d in self._details.values():
            if d.summary.session_id.startswith(ref) or d.summary.source_path == ref:
                return d
        raise FileNotFoundError(ref)


def _make_large_session_detail(
    *,
    provider: str = "cursor",
    session_id: str = "large-120",
    cwd: str = "/tmp/large-repo",
    turn_count: int = 120,
    per_turn_chars: int = 4000,
) -> SessionDetail:
    turns: list[NormalizedTurn] = []
    for idx in range(1, turn_count + 1):
        filler = f"turn-{idx}-" + ("x" * per_turn_chars)
        turns.append(
            NormalizedTurn(
                turn_index=idx,
                timestamp=f"2026-11-20T10:00:{idx % 60:02d}Z",
                user_text=f"User prompt {idx}: {filler}",
                assistant_text=f"Assistant reply {idx}: {filler}",
                tool_events=[
                    ToolEvent(
                        call_id=f"call-{idx}",
                        tool_name="run_shell",
                        arguments_summary="cmd=" + ("a" * 2000),
                        output_summary="out=" + ("b" * 2000),
                        status="success",
                        files_touched=[f"src/file_{idx}.py"],
                    )
                ],
            )
        )
    summary = SessionSummary(
        provider=provider,
        session_id=session_id,
        source_kind="synthetic",
        source_path=f"/tmp/{session_id}.json",
        cwd=cwd,
        repo_name="large-repo",
        git_branch="main",
        title="Large 120-turn session",
        first_user_prompt="User prompt 1",
        last_user_prompt=f"User prompt {turn_count}",
        created_at="2026-11-20T09:00:00Z",
        updated_at="2026-11-20T10:00:00Z",
        turn_count=turn_count,
    )
    return SessionDetail(
        summary=summary,
        turns=turns,
        files_mentioned=[f"src/file_{i}.py" for i in range(1, 6)],
        commands_run=["pytest -q"],
        compaction_count=0,
        total_chars=turn_count * per_turn_chars * 2,
    )


def _encode_content_length(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _read_content_length_frame(stream) -> dict:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            raise EOFError("Unexpected EOF while reading headers")
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.strip().lower()] = value.strip()
    length = int(headers["content-length"])
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


class TestMCPServer(unittest.TestCase):
    """Tests for SessionImporterMCPServer protocol, tools, framing, and safety."""

    def test_mcp_stdio_notifications_empty_args_and_payload_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / "codex_home"
            claude_home = tmp_path / "claude_home"
            data_dir = tmp_path / "plugin_data"
            projects_dir = tmp_path / "projects"
            repo_dir = tmp_path / "demo_repo"
            repo_dir.mkdir()

            _build_codex_rollout(
                codex_home,
                session_id="11111111-1111-1111-1111-111111111111",
                cwd=str(repo_dir),
            )
            _build_agy_project(
                projects_dir,
                project_id="proj-demo",
                project_name="demo_repo",
                folder_path=repo_dir,
            )

            # 1) Subprocess stdio newline-delimited protocol test
            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_home)
            env["CLAUDE_HOME"] = str(claude_home)
            with subprocess.Popen(
                [
                    sys.executable,
                    str(_MCP_SCRIPT_PATH),
                    "--data-dir",
                    str(data_dir),
                    "--projects-dir",
                    str(projects_dir),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            ) as proc:
                assert proc.stdin is not None and proc.stdout is not None

                requests = [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test-client", "version": "1.0"},
                        },
                    },
                    # Notification without id -> MUST produce zero stdout output
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 3, "method": "resources/templates/list"},
                    {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "resources/read",
                        "params": {"uri": APP_RESOURCE_URI},
                    },
                    {"jsonrpc": "2.0", "id": 6, "method": "tools/list"},
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": "open_session_importer", "arguments": {}},
                    },
                ]
                stdin_bytes = "".join(json.dumps(req) + "\n" for req in requests).encode("utf-8")
                stdout_data, stderr_data = proc.communicate(input=stdin_bytes, timeout=15)
                self.assertEqual(proc.returncode, 0, msg=stderr_data.decode("utf-8", errors="replace"))

            lines = [line for line in stdout_data.decode("utf-8").splitlines() if line.strip()]
            # 7 requests with id + 1 notification -> exactly 7 response lines
            self.assertEqual(len(lines), 7, msg=f"Unexpected stdout lines: {lines}")
            responses = [json.loads(line) for line in lines]
            by_id = {resp["id"]: resp for resp in responses}

            # initialize
            init_res = by_id[1]["result"]
            self.assertIn("tools", init_res["capabilities"])
            self.assertIn("resources", init_res["capabilities"])
            self.assertEqual(init_res["protocolVersion"], "2025-03-26")

            # ping & resources/templates/list & resources/list & resources/read
            self.assertEqual(by_id[2]["result"], {})
            self.assertEqual(by_id[3]["result"], {"resourceTemplates": []})
            resources = by_id[4]["result"]["resources"]
            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0]["uri"], APP_RESOURCE_URI)
            self.assertEqual(resources[0]["mimeType"], APP_MIME_TYPE)

            read_contents = by_id[5]["result"]["contents"]
            self.assertEqual(read_contents[0]["uri"], APP_RESOURCE_URI)
            self.assertEqual(read_contents[0]["mimeType"], APP_MIME_TYPE)
            self.assertIn("<html", read_contents[0]["text"].lower())

            # tools/list metadata checks
            tools = {t["name"]: t for t in by_id[6]["result"]["tools"]}
            expected_tools = {
                "open_session_importer",
                "render_imported_conversation",
                "import_session_context",
                "app_list_sessions",
                "app_get_session_detail",
                "app_list_imports",
                "app_start_import",
                "app_complete_import",
                "app_delete_import",
            }
            self.assertEqual(set(tools.keys()), expected_tools)

            open_meta = tools["open_session_importer"]["_meta"]
            self.assertEqual(open_meta["ui"]["resourceUri"], APP_RESOURCE_URI)
            self.assertEqual(open_meta["ui"]["visibility"], ["model", "app"])
            entrypoints = open_meta["com.google.antigravity/ui"]["entrypoints"]
            self.assertEqual(
                entrypoints,
                [
                    {"type": "aux_pane", "title": "Session Importer"},
                    {"type": "global", "title": "Session Importer"},
                ],
            )
            self.assertEqual(
                tools["import_session_context"]["_meta"]["ui"]["visibility"],
                ["model", "app"],
            )
            for name in expected_tools:
                if name.startswith("app_"):
                    self.assertEqual(tools[name]["_meta"]["ui"]["visibility"], ["app"])
                props = tools[name]["inputSchema"].get("properties", {})
                if "provider" in props:
                    self.assertEqual(props["provider"]["type"], "string")
                    self.assertNotIn("enum", props["provider"])

            # open_session_importer({}) result
            open_res = by_id[7]["result"]
            self.assertIn("content", open_res)
            self.assertEqual(open_res["content"][0]["type"], "text")
            sc = open_res["structuredContent"]
            self.assertEqual(sc["schema_version"], 1)
            self.assertEqual(sc["trust_boundary"], TRUST_BOUNDARY)
            self.assertEqual(sc["tab"], "discover")
            self.assertGreaterEqual(len(sc["providers"]), 2)
            self.assertEqual(sc["imports"], [])
            self.assertEqual(len(sc["candidates"]), 1)
            self.assertIsNone(sc["candidates"][0]["imported_record"])
            self.assertIsNone(sc["selected_session"])
            self.assertFalse(sc["has_more_turns"])
            self.assertEqual(sc["total_turns"], 0)

            # 2) In-process notification & 120-turn oversized payload cap (< 512_000 bytes, <= 25 turns)
            large_detail = _make_large_session_detail(turn_count=120, per_turn_chars=25_000)
            synth_provider = _SyntheticProvider(details=[large_detail])
            reg = ProviderRegistry()
            reg.register(synth_provider)
            store = ImportedSessionStore(data_dir=data_dir / "cap_test")
            server = SessionImporterMCPServer(registry=reg, store=store, projects_dir=projects_dir)

            self.assertIsNone(
                server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
            )
            cap_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 99,
                    "method": "tools/call",
                    "params": {
                        "name": "open_session_importer",
                        "arguments": {"session_ref": "large-120", "provider": "cursor"},
                    },
                }
            )
            assert cap_resp is not None
            cap_sc = cap_resp["result"]["structuredContent"]
            self.assertEqual(cap_sc["tab"], "history")
            self.assertEqual(cap_sc["total_turns"], 120)
            self.assertTrue(cap_sc["has_more_turns"])
            self.assertIsNotNone(cap_sc["selected_session"])
            self.assertLessEqual(len(cap_sc["selected_session"]["turns"]), 25)
            serialized_bytes = len(json.dumps(cap_sc, ensure_ascii=False).encode("utf-8"))
            self.assertLess(serialized_bytes, 512_000)

            # Saving an import causes default tab to switch from "discover" to "imported"
            store.save_import(large_detail, provider_display_name="Cursor", mode="compact", status="ready")
            imported_tab_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 100,
                    "method": "tools/call",
                    "params": {"name": "open_session_importer", "arguments": {}},
                }
            )
            assert imported_tab_resp is not None
            self.assertEqual(imported_tab_resp["result"]["structuredContent"]["tab"], "imported")

    def test_content_length_stdio_framing_in_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = os.environ.copy()
            env["CODEX_HOME"] = str(tmp_path / "codex_home")
            env["CLAUDE_HOME"] = str(tmp_path / "claude_home")

            with subprocess.Popen(
                [
                    sys.executable,
                    str(_MCP_SCRIPT_PATH),
                    "--data-dir",
                    str(tmp_path / "plugin_data"),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            ) as proc:
                assert proc.stdin is not None and proc.stdout is not None
                try:
                    proc.stdin.write(
                        _encode_content_length(
                            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                        )
                    )
                    proc.stdin.write(
                        _encode_content_length({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    )
                    proc.stdin.write(
                        _encode_content_length({"jsonrpc": "2.0", "id": 2, "method": "ping"})
                    )
                    proc.stdin.flush()
                    proc.stdin.close()

                    r1 = _read_content_length_frame(proc.stdout)
                    r2 = _read_content_length_frame(proc.stdout)
                    self.assertEqual(r1["id"], 1)
                    self.assertIn("tools", r1["result"]["capabilities"])
                    self.assertEqual(r2["id"], 2)
                    self.assertEqual(r2["result"], {})
                    proc.wait(timeout=10)
                    self.assertEqual(proc.returncode, 0)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=5)

    def test_app_get_session_detail_reads_snapshot_after_source_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / "codex_home"
            claude_home = tmp_path / "claude_home"
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()
            projects_dir = tmp_path / "projects"
            _build_agy_project(
                projects_dir,
                project_id="proj-repo",
                project_name="repo",
                folder_path=repo_dir,
            )

            turns = [(f"Prompt {i}", f"Reply {i}") for i in range(1, 8)]
            rollout_path = _build_codex_rollout(
                codex_home,
                session_id="22222222-2222-2222-2222-222222222222",
                cwd=str(repo_dir),
                turns=turns,
            )

            registry = create_default_registry(roots={"codex": codex_home, "claude": claude_home})
            store = ImportedSessionStore(data_dir=tmp_path / "data")
            server = SessionImporterMCPServer(
                registry=registry,
                store=store,
                projects_dir=projects_dir,
            )

            start_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "app_start_import",
                        "arguments": {
                            "ref": "22222222-2222-2222-2222-222222222222",
                            "mode": "full",
                            "spawn_via": "client_send_message",
                        },
                    },
                }
            )
            assert start_resp is not None
            start_sc = start_resp["result"]["structuredContent"]
            self.assertEqual(start_sc["status"], "creating")
            self.assertEqual(start_sc["spawn_via"], "client_send_message")
            self.assertIn("[UNTRUSTED_INERT_HISTORY", start_sc["seed_prompt"])
            import_id = start_sc["import_record"]["import_id"]
            self.assertEqual(start_sc["import_record"]["agy_project_id"], "proj-repo")

            # Delete original rollout source file
            rollout_path.unlink()
            self.assertFalse(rollout_path.exists())

            detail_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        "name": "app_get_session_detail",
                        "arguments": {
                            "import_id": import_id,
                            "from_snapshot": True,
                            "turn_start": 1,
                            "turn_end": 50,
                        },
                    },
                }
            )
            assert detail_resp is not None
            detail_sc = detail_resp["result"]["structuredContent"]
            self.assertEqual(detail_sc["schema_version"], 1)
            self.assertEqual(detail_sc["trust_boundary"], TRUST_BOUNDARY)
            self.assertEqual(detail_sc["source"], "snapshot")
            self.assertEqual(detail_sc["total_turns"], 7)
            self.assertEqual(len(detail_sc["turns"]), 7)
            self.assertEqual(detail_sc["turn_start"], 1)
            self.assertEqual(detail_sc["turn_end"], 7)
            self.assertFalse(detail_sc["has_more"])
            self.assertFalse(detail_sc["has_more_turns"])
            self.assertEqual(detail_sc["import_record"]["import_id"], import_id)

    def test_app_complete_import_fallback_to_cli_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / "codex_home"
            claude_home = tmp_path / "claude_home"
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()
            projects_dir = tmp_path / "projects"
            _build_agy_project(
                projects_dir,
                project_id="proj-repo",
                project_name="repo",
                folder_path=repo_dir,
            )
            _build_codex_rollout(
                codex_home,
                session_id="33333333-3333-3333-3333-333333333333",
                cwd=str(repo_dir),
            )

            seen_cmds: list[list[str]] = []

            def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                seen_cmds.append(cmd)
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"conversation_id": "conv-cli-999"}),
                    stderr="",
                )

            registry = create_default_registry(roots={"codex": codex_home, "claude": claude_home})
            store = ImportedSessionStore(data_dir=tmp_path / "data")
            server = SessionImporterMCPServer(
                registry=registry,
                store=store,
                projects_dir=projects_dir,
                agy_bin="fake-agy",
                spawn_runner=fake_runner,
            )

            start_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {
                        "name": "app_start_import",
                        "arguments": {
                            "ref": "33333333-3333-3333-3333-333333333333",
                            "spawn_via": "client_send_message",
                        },
                    },
                }
            )
            assert start_resp is not None
            import_id = start_resp["result"]["structuredContent"]["import_record"]["import_id"]

            complete_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "tools/call",
                    "params": {
                        "name": "app_complete_import",
                        "arguments": {
                            "import_id": import_id,
                            "fallback_to_cli": True,
                        },
                    },
                }
            )
            assert complete_resp is not None
            self.assertEqual(complete_resp["result"]["structuredContent"]["import_record"]["import_id"], import_id)

            deadline = time.monotonic() + 5.0
            final_rec = None
            while time.monotonic() < deadline:
                list_resp = server.handle_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 22,
                        "method": "tools/call",
                        "params": {"name": "app_list_imports", "arguments": {}},
                    }
                )
                assert list_resp is not None
                imports = list_resp["result"]["structuredContent"]["imports"]
                if imports and imports[0]["status"] == "ready":
                    final_rec = imports[0]
                    break
                time.sleep(0.02)

            self.assertIsNotNone(final_rec)
            assert final_rec is not None
            self.assertEqual(final_rec["status"], "ready")
            self.assertEqual(final_rec["agy_conversation_id"], "conv-cli-999")
            self.assertEqual(len(seen_cmds), 1)
            self.assertIn("--project", seen_cmds[0])
            self.assertIn("proj-repo", seen_cmds[0])

    def test_app_complete_import_direct_ready_error_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()
            detail = _make_large_session_detail(provider="cursor", session_id="cur-1", cwd=str(repo_dir), turn_count=3, per_turn_chars=20)
            reg = ProviderRegistry()
            reg.register(_SyntheticProvider(details=[detail]))
            store = ImportedSessionStore(data_dir=tmp_path / "data")
            server = SessionImporterMCPServer(registry=reg, store=store)

            start_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 30,
                    "method": "tools/call",
                    "params": {
                        "name": "app_start_import",
                        "arguments": {"ref": "cur-1", "spawn_via": "client_send_message"},
                    },
                }
            )
            assert start_resp is not None
            import_id = start_resp["result"]["structuredContent"]["import_record"]["import_id"]

            err_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 31,
                    "method": "tools/call",
                    "params": {
                        "name": "app_complete_import",
                        "arguments": {"import_id": import_id, "error": "host rejected message"},
                    },
                }
            )
            assert err_resp is not None
            err_sc = err_resp["result"]["structuredContent"]
            self.assertEqual(err_sc["status"], "error")
            self.assertEqual(err_sc["import_record"]["error_message"], "host rejected message")

            ok_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 32,
                    "method": "tools/call",
                    "params": {
                        "name": "app_complete_import",
                        "arguments": {"import_id": import_id, "agy_conversation_id": "conv-direct-42"},
                    },
                }
            )
            assert ok_resp is not None
            ok_sc = ok_resp["result"]["structuredContent"]
            self.assertEqual(ok_sc["status"], "ready")
            self.assertEqual(ok_sc["import_record"]["agy_conversation_id"], "conv-direct-42")

            del_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 33,
                    "method": "tools/call",
                    "params": {
                        "name": "app_delete_import",
                        "arguments": {"import_id": import_id},
                    },
                }
            )
            assert del_resp is not None
            self.assertEqual(
                del_resp["result"]["structuredContent"],
                {"schema_version": 1, "deleted": True, "import_id": import_id},
            )

    def test_import_session_context_cross_repo_and_persists_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_a = tmp_path / "repo_a"
            repo_b = tmp_path / "repo_b"
            repo_a.mkdir()
            repo_b.mkdir()
            projects_dir = tmp_path / "projects"
            _build_agy_project(projects_dir, project_id="proj-b", project_name="repo_b", folder_path=repo_b)

            detail = _make_large_session_detail(provider="cursor", session_id="cur-cross", cwd=str(repo_b), turn_count=2, per_turn_chars=30)
            reg = ProviderRegistry()
            reg.register(_SyntheticProvider(details=[detail]))
            store = ImportedSessionStore(data_dir=tmp_path / "data")
            server = SessionImporterMCPServer(registry=reg, store=store, projects_dir=projects_dir)

            # Without allow_cross_repo=True -> ambiguous + cross_repo=True, no snapshot saved
            blocked = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 40,
                    "method": "tools/call",
                    "params": {
                        "name": "import_session_context",
                        "arguments": {
                            "ref": "cur-cross",
                            "cwd": str(repo_a),
                            "allow_cross_repo": False,
                        },
                    },
                }
            )
            assert blocked is not None
            blocked_sc = blocked["result"]["structuredContent"]
            self.assertEqual(blocked_sc["status"], "ambiguous")
            self.assertTrue(blocked_sc["cross_repo"])
            self.assertIsNone(blocked_sc["import_record"])
            self.assertEqual(store.list_records(), [])

            # With allow_cross_repo=True and conversation_id -> matched + status="ready"
            allowed = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 41,
                    "method": "tools/call",
                    "params": {
                        "name": "import_session_context",
                        "arguments": {
                            "ref": "cur-cross",
                            "cwd": str(repo_a),
                            "allow_cross_repo": True,
                            "mode": "compact",
                            "conversation_id": "conv-active-1",
                        },
                    },
                }
            )
            assert allowed is not None
            allowed_sc = allowed["result"]["structuredContent"]
            self.assertEqual(allowed_sc["status"], "matched")
            self.assertEqual(allowed_sc["trust_boundary"], TRUST_BOUNDARY)
            self.assertEqual(allowed_sc["mode"], "compact")
            self.assertEqual(allowed_sc["provider"], "cursor")
            self.assertIsNotNone(allowed_sc["import_record"])
            self.assertEqual(allowed_sc["import_record"]["status"], "ready")
            self.assertEqual(allowed_sc["import_record"]["agy_conversation_id"], "conv-active-1")
            self.assertEqual(allowed_sc["import_record"]["agy_project_id"], "proj-b")
            self.assertIn("Session Handoff", allowed_sc["rendered"])

    def test_mcp_server_discovers_custom_registered_provider_dynamically(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / "codex_home"
            claude_home = tmp_path / "claude_home"
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()

            registry = create_default_registry(roots={"codex": codex_home, "claude": claude_home})
            custom_detail = _make_large_session_detail(
                provider="cursor",
                session_id="cursor-abc-123",
                cwd=str(repo_dir),
                turn_count=4,
                per_turn_chars=30,
            )
            registry.register(_SyntheticProvider(name="cursor", display_name="Cursor", aliases=("cur",), details=[custom_detail]))

            store = ImportedSessionStore(data_dir=tmp_path / "data")
            server = SessionImporterMCPServer(registry=registry, store=store)

            open_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 50,
                    "method": "tools/call",
                    "params": {"name": "open_session_importer", "arguments": {}},
                }
            )
            assert open_resp is not None
            open_sc = open_resp["result"]["structuredContent"]
            provider_names = [p["name"] for p in open_sc["providers"]]
            self.assertIn("cursor", provider_names)
            self.assertEqual(len(open_sc["candidates"]), 1)
            self.assertEqual(open_sc["candidates"][0]["provider"], "cursor")

            list_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 51,
                    "method": "tools/call",
                    "params": {
                        "name": "app_list_sessions",
                        "arguments": {"provider": "cur", "all_cwds": True},
                    },
                }
            )
            assert list_resp is not None
            list_sc = list_resp["result"]["structuredContent"]
            self.assertEqual(len(list_sc["candidates"]), 1)
            self.assertEqual(list_sc["candidates"][0]["session_id"], "cursor-abc-123")

            # Verify zero hardcoded provider names inside mcp_server.py source
            mcp_source = Path(mcp_server.__file__).read_text(encoding="utf-8").lower()
            self.assertNotIn("codex", mcp_source)
            self.assertNotIn("claude", mcp_source)

    def test_resources_read_custom_ui_html_and_cli_background_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            html_file = tmp_path / "custom_app.html"
            html_file.write_text("<!DOCTYPE html><html><body>Custom UI</body></html>", encoding="utf-8")

            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()
            detail = _make_large_session_detail(provider="cursor", session_id="cur-bg-1", cwd=str(repo_dir), turn_count=2, per_turn_chars=25)
            reg = ProviderRegistry()
            reg.register(_SyntheticProvider(details=[detail]))
            store = ImportedSessionStore(data_dir=tmp_path / "data")

            def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps({"conversation_id": "conv-bg-777"}),
                    stderr="",
                )

            server = SessionImporterMCPServer(
                registry=reg,
                store=store,
                ui_html_path=html_file,
                agy_bin="fake-agy",
                spawn_runner=fake_runner,
            )

            read_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 60,
                    "method": "resources/read",
                    "params": {"uri": APP_RESOURCE_URI},
                }
            )
            assert read_resp is not None
            self.assertEqual(
                read_resp["result"]["contents"][0]["text"],
                "<!DOCTYPE html><html><body>Custom UI</body></html>",
            )

            bg_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 61,
                    "method": "tools/call",
                    "params": {
                        "name": "app_start_import",
                        "arguments": {"ref": "cur-bg-1", "spawn_via": "cli_background"},
                    },
                }
            )
            assert bg_resp is not None
            bg_sc = bg_resp["result"]["structuredContent"]
            self.assertEqual(bg_sc["spawn_via"], "cli_background")
            self.assertEqual(bg_sc["seed_prompt"], "")
            self.assertIn(bg_sc["status"], ("creating", "ready"))

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                recs = store.list_records()
                if recs and recs[0].status == "ready":
                    break
                time.sleep(0.02)
            self.assertEqual(store.list_records()[0].status, "ready")
            self.assertEqual(store.list_records()[0].agy_conversation_id, "conv-bg-777")

    def test_open_payload_budget_trims_metadata_and_drops_turns_if_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()
            detail = _make_large_session_detail(
                provider="cursor",
                session_id="meta-heavy-1",
                cwd=str(repo_dir),
                turn_count=3,
                per_turn_chars=30,
            )
            detail.files_mentioned = [f"src/very/long/path/module_{i}.py" for i in range(15_000)]
            detail.commands_run = [f"pytest tests/test_module_{i}.py -v --tb=short" for i in range(10_000)]

            reg = ProviderRegistry()
            reg.register(_SyntheticProvider(details=[detail]))
            store = ImportedSessionStore(data_dir=tmp_path / "data")
            server = SessionImporterMCPServer(registry=reg, store=store)

            resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 70,
                    "method": "tools/call",
                    "params": {
                        "name": "open_session_importer",
                        "arguments": {"session_ref": "meta-heavy-1", "provider": "cursor"},
                    },
                }
            )
            assert resp is not None
            sc = resp["result"]["structuredContent"]
            self.assertLess(len(json.dumps(sc, ensure_ascii=False).encode("utf-8")), 512_000)
            self.assertIn("metadata_trimmed_for_payload_limit", sc["warnings"])
            self.assertEqual(len(sc["selected_session"]["turns"]), 3)

            # Last-resort drop when non-turn payload still exceeds budget
            extreme_payload = {
                "schema_version": 1,
                "trust_boundary": TRUST_BOUNDARY,
                "tab": "history",
                "providers": [],
                "imports": [],
                "candidates": [],
                "selected_session": {
                    "summary": {"extra_blob": "z" * 600_000},
                    "turns": [{"turn_index": 1, "user_text": "hi", "assistant_text": "ok", "tool_events": []}],
                    "total_turns": 5,
                    "turn_start": 1,
                    "turn_end": 1,
                    "has_more": True,
                    "has_more_turns": True,
                    "files_mentioned": [],
                    "commands_run": [],
                    "warnings": [],
                },
                "has_more_turns": False,
                "total_turns": 5,
                "warnings": [],
            }
            mcp_server._enforce_open_payload_budget(extreme_payload)
            self.assertLess(len(json.dumps(extreme_payload, ensure_ascii=False).encode("utf-8")), 512_000)
            self.assertIsNone(extreme_payload["selected_session"])
            self.assertTrue(extreme_payload["has_more_turns"])

    def test_provider_alias_and_multi_selector_snapshot_fallback_and_open_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / "codex_home"
            claude_home = tmp_path / "claude_home"
            repo_a = tmp_path / "repo_a"
            repo_b = tmp_path / "repo_b"
            repo_a.mkdir()
            repo_b.mkdir()

            rollout_a = _build_codex_rollout(
                codex_home,
                session_id="aaaa1111-1111-1111-1111-111111111111",
                cwd=str(repo_a),
                turns=[("Repo A task", "Repo A done")],
                updated_ts="2026-11-20T10:00:00Z",
            )
            _build_codex_rollout(
                codex_home,
                session_id="bbbb2222-2222-2222-2222-222222222222",
                cwd=str(repo_b),
                turns=[("Repo B task", "Repo B done")],
                updated_ts="2026-11-21T10:00:00Z",
            )

            reg = create_default_registry(roots={"codex": codex_home, "claude": claude_home})
            custom_detail = _make_large_session_detail(
                provider="cursor",
                session_id="cur-alias-1",
                cwd=str(repo_a),
                turn_count=2,
                per_turn_chars=25,
            )
            reg.register(
                _SyntheticProvider(
                    name="cursor",
                    display_name="Cursor",
                    aliases=("cur",),
                    details=[custom_detail],
                )
            )
            other_detail = _make_large_session_detail(
                provider="windsurf",
                session_id="ws-1",
                cwd=str(repo_b),
                turn_count=1,
                per_turn_chars=20,
            )
            reg.register(
                _SyntheticProvider(
                    name="windsurf",
                    display_name="Windsurf",
                    aliases=("ws",),
                    details=[other_detail],
                )
            )

            store = ImportedSessionStore(data_dir=tmp_path / "data")
            server = SessionImporterMCPServer(registry=reg, store=store)

            # 1) open_session_importer forwards cwd to _load_detail_for_ui for session_ref="latest"
            open_a = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 80,
                    "method": "tools/call",
                    "params": {
                        "name": "open_session_importer",
                        "arguments": {"session_ref": "latest", "cwd": str(repo_a)},
                    },
                }
            )
            assert open_a is not None
            sel_a = open_a["result"]["structuredContent"]["selected_session"]
            self.assertIsNotNone(sel_a)
            self.assertEqual(sel_a["summary"]["session_id"], "aaaa1111-1111-1111-1111-111111111111")

            # 2) Save import for codex rollout, delete source, then resolve using alias/multi-selector
            store.save_import(
                reg.get("codex").load_session("aaaa1111-1111-1111-1111-111111111111"),
                provider_display_name="Codex",
                status="ready",
            )
            rollout_a.unlink()
            # Also clear cursor live details to simulate pruned source for alias/multi-selector test
            store.save_import(custom_detail, provider_display_name="Cursor", status="ready")
            reg.get("cursor")._details.clear()

            alias_detail = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 81,
                    "method": "tools/call",
                    "params": {
                        "name": "app_get_session_detail",
                        "arguments": {"ref": "cur-alias-1", "provider": "cur", "from_snapshot": True},
                    },
                }
            )
            assert alias_detail is not None
            self.assertFalse(alias_detail["result"].get("isError", False))
            self.assertEqual(alias_detail["result"]["structuredContent"]["source"], "snapshot")
            self.assertEqual(alias_detail["result"]["structuredContent"]["summary"]["session_id"], "cur-alias-1")

            multi_start = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 82,
                    "method": "tools/call",
                    "params": {
                        "name": "app_start_import",
                        "arguments": {
                            "ref": "cur-alias-1",
                            "provider": "cur,ws",
                            "spawn_via": "client_send_message",
                        },
                    },
                }
            )
            assert multi_start is not None
            self.assertEqual(multi_start["result"]["structuredContent"]["status"], "creating")
            self.assertIn("cur-alias-1", multi_start["result"]["structuredContent"]["import_record"]["session_id"])

    def test_sqlite_codex_session_detail_start_import_open_cwd_meta_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / "codex_home"
            claude_home = tmp_path / "claude_home"
            codex_home.mkdir(parents=True)
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()

            sql_id = "019a0000-5555-7000-8000-000000000001"
            state_conn = sqlite3.connect(str(codex_home / "state_10.sqlite"))
            state_conn.execute(
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
            state_conn.execute(
                """
                INSERT INTO threads (
                    id, rollout_path, created_at, updated_at, cwd, title, git_branch,
                    first_user_message, archived, preview
                ) VALUES (?, '', 1776916275, 1776916280, ?, 'SQLite Codex Thread', 'main', 'Inspect SQLite thread', 0, 'Inspect SQLite thread')
                """,
                (sql_id, str(repo_dir)),
            )
            state_conn.commit()
            state_conn.close()

            hist_conn = sqlite3.connect(str(codex_home / "thread_history_1.sqlite"))
            hist_conn.execute(
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
            hist_conn.execute(
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
            hist_conn.execute(
                "INSERT INTO thread_turns VALUES (?, 'turn-1', 0, 'completed', NULL, 1776916275, 1776916280)",
                (sql_id,),
            )
            hist_conn.execute(
                "INSERT INTO thread_items VALUES (?, 'turn-1', 'item-1', 0, 1776916275000, 'userMessage', ?)",
                (sql_id, json.dumps({"type": "userMessage", "content": [{"type": "text", "text": "Inspect SQLite thread"}]})),
            )
            hist_conn.execute(
                "INSERT INTO thread_items VALUES (?, 'turn-1', 'item-2', 1, 1776916276000, 'agentMessage', ?)",
                (sql_id, json.dumps({"type": "agentMessage", "text": "Loaded from SQLite history"})),
            )
            hist_conn.commit()
            hist_conn.close()

            reg = create_default_registry(roots={"codex": codex_home, "claude": claude_home})
            store = ImportedSessionStore(data_dir=tmp_path / "data")
            server = SessionImporterMCPServer(registry=reg, store=store)

            # 1) open_session_importer returns cwd & all_cwds=False when cwd is specified, and prefersBorder=False on resources
            open_cwd_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 90,
                    "method": "tools/call",
                    "params": {"name": "open_session_importer", "arguments": {"cwd": str(repo_dir)}},
                }
            )
            assert open_cwd_resp is not None
            open_sc = open_cwd_resp["result"]["structuredContent"]
            self.assertEqual(open_sc["cwd"], str(repo_dir))
            self.assertFalse(open_sc["all_cwds"])
            self.assertEqual(len(open_sc["candidates"]), 1)
            self.assertEqual(open_sc["candidates"][0]["source_kind"], "sqlite_history")
            self.assertTrue(open_sc["candidates"][0]["source_path"].endswith("thread_history_1.sqlite"))

            open_all_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 91,
                    "method": "tools/call",
                    "params": {"name": "open_session_importer", "arguments": {}},
                }
            )
            assert open_all_resp is not None
            self.assertEqual(open_all_resp["result"]["structuredContent"]["cwd"], "")
            self.assertTrue(open_all_resp["result"]["structuredContent"]["all_cwds"])

            res_list = server.handle_message({"jsonrpc": "2.0", "id": 92, "method": "resources/list"})
            assert res_list is not None
            self.assertEqual(res_list["result"]["resources"][0]["_meta"], {"ui": {"prefersBorder": False}})
            res_read = server.handle_message(
                {"jsonrpc": "2.0", "id": 93, "method": "resources/read", "params": {"uri": APP_RESOURCE_URI}}
            )
            assert res_read is not None
            self.assertEqual(res_read["result"]["contents"][0]["_meta"], {"ui": {"prefersBorder": False}})

            # 2) SQLite-backed Codex session works with app_get_session_detail & app_start_import via session_id
            detail_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 94,
                    "method": "tools/call",
                    "params": {
                        "name": "app_get_session_detail",
                        "arguments": {"ref": sql_id, "provider": "codex", "from_snapshot": True},
                    },
                }
            )
            assert detail_resp is not None
            self.assertFalse(detail_resp["result"].get("isError", False))
            detail_sc = detail_resp["result"]["structuredContent"]
            self.assertEqual(detail_sc["summary"]["session_id"], sql_id)
            self.assertEqual(detail_sc["turns"][0]["assistant_text"], "Loaded from SQLite history")

            start_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 95,
                    "method": "tools/call",
                    "params": {
                        "name": "app_start_import",
                        "arguments": {"ref": sql_id, "provider": "codex", "spawn_via": "client_send_message"},
                    },
                }
            )
            assert start_resp is not None
            self.assertEqual(start_resp["result"]["structuredContent"]["status"], "creating")
            self.assertEqual(start_resp["result"]["structuredContent"]["import_record"]["session_id"], sql_id)

            # 3) _enforce_open_payload_budget preserves small files_mentioned when popping turns suffices
            small_files = ["src/a.py", "src/b.py", "tests/test_a.py"]
            small_cmds = ["pytest -q"]
            turn_heavy_payload = {
                "schema_version": 1,
                "trust_boundary": TRUST_BOUNDARY,
                "tab": "history",
                "providers": [],
                "imports": [],
                "candidates": [],
                "selected_session": {
                    "summary": {"provider": "cursor", "session_id": "turn-heavy-1"},
                    "turns": [
                        {
                            "turn_index": i,
                            "user_text": f"U{i} " + ("u" * 60_000),
                            "assistant_text": f"A{i} " + ("a" * 60_000),
                            "tool_events": [],
                        }
                        for i in range(1, 8)
                    ],
                    "total_turns": 7,
                    "turn_start": 1,
                    "turn_end": 7,
                    "has_more": False,
                    "has_more_turns": False,
                    "files_mentioned": list(small_files),
                    "commands_run": list(small_cmds),
                    "warnings": [],
                },
                "has_more_turns": False,
                "total_turns": 7,
                "warnings": [],
            }
            mcp_server._enforce_open_payload_budget(turn_heavy_payload, max_bytes=250_000)
            self.assertLess(len(json.dumps(turn_heavy_payload, ensure_ascii=False).encode("utf-8")), 250_000)
            self.assertEqual(turn_heavy_payload["selected_session"]["files_mentioned"], small_files)
            self.assertEqual(turn_heavy_payload["selected_session"]["commands_run"], small_cmds)
            self.assertLess(len(turn_heavy_payload["selected_session"]["turns"]), 7)
            self.assertTrue(turn_heavy_payload["has_more_turns"])

            # 4) serve_stdio rejects negative Content-Length and drains trailing headers before next message
            ping_body = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}).encode("utf-8")
            raw_stream = (
                b"Content-Length: -5\r\n"
                b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
                b"\r\n"
                + f"Content-Length: {len(ping_body)}\r\n\r\n".encode("ascii")
                + ping_body
            )
            stdin_buf = io.BytesIO(raw_stream)
            stdout_buf = io.BytesIO()
            server.serve_stdio(stdin=stdin_buf, stdout=stdout_buf)
            out_bytes = stdout_buf.getvalue()
            frames: list[dict] = []
            pos = 0
            while pos < len(out_bytes):
                hdr_end = out_bytes.find(b"\r\n\r\n", pos)
                self.assertNotEqual(hdr_end, -1)
                hdr = out_bytes[pos:hdr_end].decode("ascii")
                cl = int(hdr.split(":", 1)[1].strip())
                body_start = hdr_end + 4
                frames.append(json.loads(out_bytes[body_start : body_start + cl].decode("utf-8")))
                pos = body_start + cl
            self.assertEqual(len(frames), 2)
            self.assertEqual(frames[0]["error"]["code"], -32700)
            self.assertEqual(frames[1]["id"], 99)
            self.assertEqual(frames[1]["result"], {})

    def test_render_imported_conversation_inline_tool_and_seed_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()
            detail = _make_large_session_detail(
                provider="cursor",
                session_id="inline-sess-1",
                cwd=str(repo_dir),
                turn_count=35,
                per_turn_chars=40,
            )
            reg = ProviderRegistry()
            reg.register(_SyntheticProvider(details=[detail]))
            store = ImportedSessionStore(data_dir=tmp_path / "data")
            server = SessionImporterMCPServer(registry=reg, store=store)

            list_resp = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert list_resp is not None
            tools_by_name = {t["name"]: t for t in list_resp["result"]["tools"]}
            self.assertIn("render_imported_conversation", tools_by_name)
            inline_meta = tools_by_name["render_imported_conversation"]["_meta"]
            self.assertEqual(
                inline_meta,
                {"ui": {"resourceUri": APP_RESOURCE_URI, "visibility": ["model", "app"]}},
            )
            self.assertNotIn("com.google.antigravity/ui", inline_meta)

            start_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "app_start_import",
                        "arguments": {
                            "ref": "inline-sess-1",
                            "provider": "cursor",
                            "spawn_via": "client_send_message",
                        },
                    },
                }
            )
            assert start_resp is not None
            start_sc = start_resp["result"]["structuredContent"]
            import_id = start_sc["import_record"]["import_id"]
            self.assertIn("render_imported_conversation", start_sc["seed_prompt"])
            self.assertIn(import_id, start_sc["seed_prompt"])

            render_resp = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "render_imported_conversation",
                        "arguments": {"import_id": import_id},
                    },
                }
            )
            assert render_resp is not None
            rsc = render_resp["result"]["structuredContent"]
            self.assertTrue(rsc["inline_mode"])
            self.assertEqual(rsc["tab"], "history")
            self.assertEqual(rsc["total_turns"], 35)
            self.assertTrue(rsc["has_more_turns"])
            self.assertEqual(len(rsc["selected_session"]["turns"]), 25)
            self.assertEqual(rsc["selected_session"]["source"], "snapshot")


if __name__ == "__main__":
    unittest.main()
