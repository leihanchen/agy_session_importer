"""Unit and subprocess CLI tests for skills/resume-session/scripts/session_importer.py."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "skills" / "resume-session" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.models import NormalizedTurn, SessionDetail, SessionSummary
from importer.providers.base import ProviderReadError, SessionProvider
from importer.providers.registry import ProviderRegistry, create_default_registry
from importer.safety import TRUST_BOUNDARY
import session_importer

_SCRIPT_PATH = Path(_SCRIPTS_DIR) / "session_importer.py"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(rec) for rec in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_codex_rollout(
    codex_home: Path,
    *,
    session_id: str,
    cwd: str,
    branch: str = "feat/cli-test",
    turns: list[tuple[str, str]] | None = None,
    updated_ts: str = "2026-11-20T10:00:00Z",
) -> Path:
    rollout = codex_home / "sessions" / "2026" / "11" / "20" / f"rollout-2026-11-20T10-00-00-{session_id}.jsonl"
    turns = turns or [
        ("Build the CLI entrypoint for session_importer.", "Implemented subcommands and JSON output."),
        ("Verify edge cases and exit codes.", "Added tests for exit code 0, 1, and 2."),
        ("Document the drill-down range format.", "Updated help text for --range START:END."),
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
        records.append(
            {
                "timestamp": f"2026-11-20T09:5{idx}:00Z" if idx < 10 else updated_ts,
                "type": "turn_context",
                "payload": {"turn_id": f"turn-{idx}", "cwd": cwd},
            }
        )
        records.append(
            {
                "timestamp": f"2026-11-20T09:5{idx}:01Z" if idx < 10 else updated_ts,
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
                "timestamp": updated_ts if idx == len(turns) else f"2026-11-20T09:5{idx}:30Z",
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


def _build_claude_session(
    claude_dir: Path,
    *,
    session_id: str,
    cwd: str,
    branch: str = "main",
    title: str = "Claude CLI check",
    turns: list[tuple[str, str]] | None = None,
) -> Path:
    slug = cwd.replace("/", "-") or "-root"
    session_file = claude_dir / "projects" / slug / f"{session_id}.jsonl"
    turns = turns or [
        ("Audit Claude transcript parsing.", "Verified DAG reconstruction and compaction handling."),
        ("Check compact mode budget.", "Confirmed compact output stays under 12000 chars."),
    ]
    records: list[dict] = []
    parent_uuid: str | None = None
    for idx, (user_msg, asst_msg) in enumerate(turns, start=1):
        u_uuid = f"{session_id}-u{idx}"
        a_uuid = f"{session_id}-a{idx}"
        records.append(
            {
                "type": "user",
                "uuid": u_uuid,
                "parentUuid": parent_uuid,
                "sessionId": session_id,
                "cwd": cwd,
                "gitBranch": branch,
                "timestamp": f"2026-11-20T08:0{idx}:00Z",
                "message": {"role": "user", "content": user_msg},
            }
        )
        records.append(
            {
                "type": "assistant",
                "uuid": a_uuid,
                "parentUuid": u_uuid,
                "sessionId": session_id,
                "timestamp": f"2026-11-20T08:0{idx}:20Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": asst_msg}]},
            }
        )
        parent_uuid = a_uuid
    records.append({"type": "custom-title", "customTitle": title, "sessionId": session_id})
    _write_jsonl(session_file, records)
    return session_file


class _CustomMockProvider(SessionProvider):
    """3rd mock provider verifying dynamic registry routing without CLI edits."""

    name = "cursor"
    display_name = "Cursor"
    aliases = ("custom_alias",)

    def __init__(self, *, cwd: str = "/tmp/mock-repo") -> None:
        self._cwd = cwd
        self._summary = SessionSummary(
            provider=self.name,
            session_id="cursor-sess-0001",
            source_kind="cursor_log",
            source_path="/tmp/cursor/cursor-sess-0001.log",
            cwd=cwd,
            repo_name="mock-repo",
            git_branch="main",
            title="Mock cursor task",
            first_user_prompt="Refactor widget renderer",
            last_user_prompt="Run widget tests",
            created_at="2026-11-20T11:00:00Z",
            updated_at="2026-11-20T11:10:00Z",
            turn_count=2,
        )

    def is_available(self) -> bool:
        return True

    def list_sessions(self, *, include_archived: bool = False) -> tuple[list[SessionSummary], list[str]]:
        return ([self._summary], [])

    def can_handle_path(self, path: Path) -> bool:
        return path.suffix == ".cursor"

    def load_session(self, ref: str, *, max_tool_chars: int = 600) -> SessionDetail:
        _ = max_tool_chars
        return SessionDetail(
            summary=self._summary,
            turns=[
                NormalizedTurn(
                    turn_index=1,
                    timestamp="2026-11-20T11:00:00Z",
                    user_text="Refactor widget renderer",
                    assistant_text="Extracted base class.",
                ),
                NormalizedTurn(
                    turn_index=2,
                    timestamp="2026-11-20T11:10:00Z",
                    user_text="Run widget tests",
                    assistant_text="All widget tests passed.",
                ),
            ],
        )


def _run_main(argv: list[str], *, registry: ProviderRegistry | None = None) -> tuple[int, str, str]:
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        code = session_importer.main(argv, registry=registry)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _run_cli_subprocess(
    args: list[str],
    *,
    env: dict[str, str],
    cwd: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


class TestSessionImporterCLI(unittest.TestCase):
    """Tests for Task 7 CLI entrypoint and dynamic provider routing."""

    def test_cli_providers_and_dynamic_mock_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex_home"
            claude_dir = Path(tmp) / "claude_dir"
            codex_home.mkdir()
            claude_dir.mkdir()
            repo_dir = Path(tmp) / "mock-repo"
            repo_dir.mkdir()

            registry = create_default_registry({"codex": codex_home, "claude": claude_dir})
            registry.register(_CustomMockProvider(cwd=str(repo_dir)))

            code, out, err = _run_main(["providers", "--json"], registry=registry)
            self.assertEqual(code, 0, msg=err)
            payload = json.loads(out)
            self.assertEqual(payload["schema_version"], 1)
            by_name = {entry["name"]: entry for entry in payload["providers"]}
            self.assertIn("codex", by_name)
            self.assertIn("claude", by_name)
            self.assertIn("cursor", by_name)
            self.assertTrue(by_name["cursor"]["available"])
            self.assertEqual(by_name["cursor"]["aliases"], ["custom_alias"])

            # Dynamic alias routing works without hardcoding provider names in session_importer.py.
            code, out, err = _run_main(
                ["list", "--provider", "custom_alias", "--all-cwds", "--json"],
                registry=registry,
            )
            self.assertEqual(code, 0, msg=err)
            list_payload = json.loads(out)
            self.assertEqual(list_payload["trust_boundary"], TRUST_BOUNDARY)
            self.assertEqual(len(list_payload["sessions"]), 1)
            self.assertEqual(list_payload["sessions"][0]["provider"], "cursor")

            code, out, err = _run_main(
                ["show", "cursor-sess-0001", "--provider", "custom_alias", "--cwd", str(repo_dir), "--json"],
                registry=registry,
            )
            self.assertEqual(code, 0, msg=err)
            show_payload = json.loads(out)
            self.assertEqual(show_payload["status"], "matched")
            self.assertEqual(show_payload["provider"], "cursor")

            # Unknown provider exits 2 with JSON error when --json is set.
            code, out, _ = _run_main(
                ["list", "--provider", "nonexistent_provider", "--json"],
                registry=registry,
            )
            self.assertEqual(code, 2)
            self.assertIn("error", json.loads(out))

    def test_cli_subprocess_list_show_and_turns_codex_and_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / "codex_home"
            claude_dir = tmp_path / "claude_dir"
            repo_a = tmp_path / "repo_a"
            repo_a.mkdir()
            (repo_a / ".git").mkdir()

            codex_sid = "11111111-1111-4111-8111-111111111111"
            claude_sid = "22222222-2222-4222-8222-222222222222"

            _build_codex_rollout(
                codex_home,
                session_id=codex_sid,
                cwd=str(repo_a),
                turns=[
                    (f"Turn {i} user prompt: " + ("alpha " * 30), f"Turn {i} assistant answer: " + ("beta " * 30))
                    for i in range(1, 6)
                ],
            )
            _build_claude_session(
                claude_dir,
                session_id=claude_sid,
                cwd=str(repo_a),
                title="Claude refactor check",
            )

            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_home)
            env["CLAUDE_CONFIG_DIR"] = str(claude_dir)

            # 1. list --all-cwds --json returns both Codex and Claude sessions
            proc = _run_cli_subprocess(["list", "--all-cwds", "--json"], env=env, cwd=str(repo_a))
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            list_data = json.loads(proc.stdout)
            self.assertEqual(list_data["schema_version"], 1)
            self.assertEqual(list_data["trust_boundary"], TRUST_BOUNDARY)
            found_ids = {s["session_id"] for s in list_data["sessions"]}
            self.assertEqual(found_ids, {codex_sid, claude_sid})

            # 2. show --mode compact --json on Claude session
            proc = _run_cli_subprocess(
                ["show", claude_sid, "--provider", "claude", "--cwd", str(repo_a), "--mode", "compact", "--json"],
                env=env,
                cwd=str(repo_a),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            compact_data = json.loads(proc.stdout)
            self.assertEqual(compact_data["trust_boundary"], TRUST_BOUNDARY)
            self.assertEqual(compact_data["status"], "matched")
            self.assertEqual(compact_data["mode"], "compact")
            self.assertEqual(compact_data["provider"], "claude")
            self.assertIn("Claude refactor check", compact_data["rendered"])

            # 3. show --mode full --max-chars 600 --json bounds Codex output and preserves Turn 1 + drill-down marker
            proc = _run_cli_subprocess(
                ["show", codex_sid, "--provider", "codex", "--cwd", str(repo_a), "--mode", "full", "--max-chars", "600", "--json"],
                env=env,
                cwd=str(repo_a),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            full_data = json.loads(proc.stdout)
            self.assertEqual(full_data["trust_boundary"], TRUST_BOUNDARY)
            self.assertEqual(full_data["status"], "matched")
            self.assertEqual(full_data["mode"], "full")
            self.assertLessEqual(len(full_data["rendered"]), 600)
            self.assertIn("bounded_history", full_data["warnings"])

            # 4. turns <id> --range 1:2 --json returns exact 2 turns
            proc = _run_cli_subprocess(
                ["turns", codex_sid, "--range", "1:2", "--json"],
                env=env,
                cwd=str(repo_a),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            turns_data = json.loads(proc.stdout)
            self.assertEqual(turns_data["schema_version"], 1)
            self.assertEqual(turns_data["trust_boundary"], TRUST_BOUNDARY)
            self.assertEqual(turns_data["start"], 1)
            self.assertEqual(turns_data["end"], 2)
            self.assertEqual(turns_data["total_turns"], 5)
            self.assertEqual([t["turn_index"] for t in turns_data["turns"]], [1, 2])

    def test_cli_cross_repo_gate_and_allow_cross_repo_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / "codex_home"
            claude_dir = tmp_path / "claude_dir"
            claude_dir.mkdir()
            repo_a = tmp_path / "repo_a"
            repo_b = tmp_path / "repo_b"
            repo_a.mkdir()
            repo_b.mkdir()
            (repo_a / ".git").mkdir()
            (repo_b / ".git").mkdir()

            codex_sid = "33333333-3333-4333-8333-333333333333"
            _build_codex_rollout(codex_home, session_id=codex_sid, cwd=str(repo_a))

            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_home)
            env["CLAUDE_CONFIG_DIR"] = str(claude_dir)

            # Without --allow-cross-repo, cross-repo lookup returns status == "ambiguous" with exit 0
            proc = _run_cli_subprocess(
                ["show", codex_sid, "--cwd", str(repo_b), "--json"],
                env=env,
                cwd=str(repo_b),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            blocked_data = json.loads(proc.stdout)
            self.assertEqual(blocked_data["status"], "ambiguous")
            self.assertEqual(blocked_data["reason"], "cross_repo_confirmation_required")
            self.assertTrue(blocked_data["cross_repo"])

            # With --allow-cross-repo, returns status == "matched" with exit 0
            proc = _run_cli_subprocess(
                ["show", codex_sid, "--cwd", str(repo_b), "--allow-cross-repo", "--json"],
                env=env,
                cwd=str(repo_b),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            allowed_data = json.loads(proc.stdout)
            self.assertEqual(allowed_data["status"], "matched")
            self.assertTrue(allowed_data["cross_repo"])
            self.assertEqual(allowed_data["session"]["session_id"], codex_sid)

    def test_cli_invalid_range_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            codex_home = tmp_path / "codex_home"
            claude_dir = tmp_path / "claude_dir"
            claude_dir.mkdir()
            repo_a = tmp_path / "repo_a"
            repo_a.mkdir()

            codex_sid = "44444444-4444-4444-8444-444444444444"
            _build_codex_rollout(codex_home, session_id=codex_sid, cwd=str(repo_a))

            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_home)
            env["CLAUDE_CONFIG_DIR"] = str(claude_dir)

            for bad_range in ("5:2", "abc", "0:2", "99:100"):
                proc = _run_cli_subprocess(
                    ["turns", codex_sid, "--range", bad_range, "--json"],
                    env=env,
                    cwd=str(repo_a),
                )
                self.assertEqual(proc.returncode, 2, msg=f"range={bad_range} stdout={proc.stdout} stderr={proc.stderr}")
                err_data = json.loads(proc.stdout)
                self.assertIn("error", err_data)
                self.assertTrue(err_data["error"])

    def test_cli_validation_and_runtime_error_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "mock-repo"
            repo_dir.mkdir()

            # Unresolved ref in turns -> exit 2 with JSON error
            registry = ProviderRegistry()
            registry.register(_CustomMockProvider(cwd=str(repo_dir)))
            code, out, _ = _run_main(
                ["turns", "missing-session-id", "--range", "1:2", "--json"],
                registry=registry,
            )
            self.assertEqual(code, 2)
            self.assertIn("error", json.loads(out))

            # Invalid --mode and --max-chars -> exit 2 with JSON error
            for bad_args in (
                ["show", "--mode", "invalid_mode", "--json"],
                ["show", "--max-chars", "0", "--json"],
                ["show", "--max-chars", "-10", "--json"],
            ):
                code, out, _ = _run_main(bad_args, registry=registry)
                self.assertEqual(code, 2, msg=f"args={bad_args} out={out}")
                self.assertIn("error", json.loads(out))

            # Unexpected runtime error -> exit 1 with JSON error
            with mock.patch.object(
                session_importer,
                "list_sessions",
                side_effect=ProviderReadError("synthetic storage failure"),
            ):
                code, out, _ = _run_main(["list", "--all-cwds", "--json"], registry=registry)
            self.assertEqual(code, 1)
            err_payload = json.loads(out)
            self.assertIn("error", err_payload)
            self.assertIn("synthetic storage failure", err_payload["error"])

            # Human-readable text outputs exit 0 cleanly
            for text_args in (
                ["providers"],
                ["list", "--all-cwds"],
                ["show", "cursor-sess-0001", "--cwd", str(repo_dir)],
                ["turns", "cursor-sess-0001", "--range", "1:2"],
            ):
                code, out, err = _run_main(text_args, registry=registry)
                self.assertEqual(code, 0, msg=f"args={text_args} err={err}")
                self.assertTrue(out.strip())

    def test_cli_internal_value_error_exits_1_and_startup_failure_emits_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "mock-repo"
            repo_dir.mkdir()
            registry = ProviderRegistry()
            registry.register(_CustomMockProvider(cwd=str(repo_dir)))

            # Unexpected internal ValueError from downstream logic must exit 1, not 2
            with mock.patch.object(
                session_importer,
                "list_sessions",
                side_effect=ValueError("internal parser bug"),
            ):
                code, out, _ = _run_main(["list", "--all-cwds", "--json"], registry=registry)
            self.assertEqual(code, 1)
            self.assertIn("internal parser bug", json.loads(out)["error"])

            with mock.patch.object(
                session_importer,
                "render_show_envelope",
                side_effect=ValueError("unexpected render bug"),
            ):
                code, out, _ = _run_main(
                    ["show", "cursor-sess-0001", "--cwd", str(repo_dir), "--json"],
                    registry=registry,
                )
            self.assertEqual(code, 1)
            self.assertIn("unexpected render bug", json.loads(out)["error"])

            # Startup errors inside create_default_registry / build_parser must respect --json
            with mock.patch.object(
                session_importer,
                "create_default_registry",
                side_effect=RuntimeError("registry init failed"),
            ):
                code, out, err = _run_main(["list", "--json"])
            self.assertEqual(code, 1)
            self.assertEqual(err, "")
            self.assertIn("registry init failed", json.loads(out)["error"])

            with mock.patch.object(
                session_importer,
                "build_parser",
                side_effect=ValueError("parser init failed"),
            ):
                code, out, err = _run_main(["providers", "--json"], registry=registry)
            self.assertEqual(code, 1)
            self.assertEqual(err, "")
            self.assertIn("parser init failed", json.loads(out)["error"])


if __name__ == "__main__":
    unittest.main()
