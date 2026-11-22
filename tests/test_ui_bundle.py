"""Verification tests for the single-file MCP App UI bundle and source hygiene."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_UI_SRC_DIR = _REPO_ROOT / "ui_src"
_APP_TS_PATH = _UI_SRC_DIR / "app.ts"
_BUILD_MJS_PATH = _UI_SRC_DIR / "build.mjs"
_APP_HTML_PATH = _REPO_ROOT / "skills" / "resume-session" / "ui" / "app.html"


class TestUIBundle(unittest.TestCase):
    """Tests for skills/resume-session/ui/app.html and ui_src/ invariants."""

    def test_ui_bundle_exists_size_and_csp_compliance(self) -> None:
        self.assertTrue(_APP_HTML_PATH.is_file(), f"Missing UI bundle at {_APP_HTML_PATH}")
        html = _APP_HTML_PATH.read_text(encoding="utf-8")
        size_bytes = len(html.encode("utf-8"))
        self.assertGreater(size_bytes, 1_000)
        self.assertLess(size_bytes, 10 * 1024 * 1024)
        self.assertIn("<!doctype html>", html[:200].lower())

        # Strict CSP: no external scripts or external stylesheets
        self.assertIsNone(
            re.search(r"<script\b[^>]*\bsrc\s*=", html, re.IGNORECASE),
            "External <script src=...> is forbidden by MCP App CSP",
        )
        self.assertIsNone(
            re.search(
                r"<link\b[^>]*\brel\s*=\s*[\"']?stylesheet\b[^>]*\bhref\s*=",
                html,
                re.IGNORECASE,
            ),
            "External <link rel='stylesheet' href=...> is forbidden by MCP App CSP",
        )
        self.assertIsNone(
            re.search(r"<link\b[^>]*\bhref\s*=", html, re.IGNORECASE),
            "External <link href=...> is forbidden by MCP App CSP",
        )

        # Inline <style> present
        self.assertIn("<style>", html)
        self.assertIn("</style>", html)

        # Exact single <script>...</script> block with zero unescaped </script> inside JS body
        script_opens = re.findall(r"<script\b[^>]*>", html, re.IGNORECASE)
        script_closes = re.findall(r"</script\s*>", html, re.IGNORECASE)
        self.assertEqual(len(script_opens), 1, f"Expected 1 <script> tag, found {len(script_opens)}")
        self.assertEqual(len(script_closes), 1, f"Expected 1 </script> tag, found {len(script_closes)}")

        script_match = re.search(r"<script\b[^>]*>(.*)</script\s*>", html, re.IGNORECASE | re.DOTALL)
        self.assertIsNotNone(script_match)
        assert script_match is not None
        js_body = script_match.group(1)
        self.assertNotIn("</script", js_body.lower())

    def test_ui_bundle_capability_gating_and_handshake_order(self) -> None:
        self.assertTrue(_APP_TS_PATH.is_file(), f"Missing {_APP_TS_PATH}")
        self.assertTrue(_APP_HTML_PATH.is_file(), f"Missing {_APP_HTML_PATH}")
        app_ts = _APP_TS_PATH.read_text(encoding="utf-8")
        app_html = _APP_HTML_PATH.read_text(encoding="utf-8")

        connect_idx = app_ts.find("app.connect(")
        self.assertGreater(connect_idx, 0, "app.connect() call missing in ui_src/app.ts")

        for handler in (
            "app.ontoolinput",
            "app.ontoolresult",
            "app.ontoolcancelled",
            "app.onhostcontextchanged",
        ):
            idx = app_ts.find(handler)
            self.assertGreaterEqual(idx, 0, f"Missing handler {handler} in ui_src/app.ts")
            self.assertLess(idx, connect_idx, f"{handler} must be registered before app.connect()")

        required_tokens = [
            "getHostCapabilities",
            "sendMessage",
            "target",
            "fallback_to_cli",
            "cli_background",
            "updateModelContext",
            "com.google.antigravity/toggle-aux-pane",
            "discoverRequestGeneration",
            "openToolDetails",
            "completeRes.isError",
            "fbRes.isError",
        ]
        for token in required_tokens:
            self.assertIn(token, app_ts, f"Missing {token!r} in ui_src/app.ts")

        upsert_idx = app_ts.find("upsertImportRecord(record)")
        err_status_idx = app_ts.find('startSc.status === "error"')
        self.assertGreater(upsert_idx, 0)
        self.assertGreater(err_status_idx, upsert_idx)

        self.assertIsNotNone(
            re.search(r'target\s*:\s*["\']new["\']', app_ts),
            'sendMessage must use target: "new" under _meta',
        )

        # Provider isolation: no hardcoded codex/claude provider lists in UI source
        self.assertNotIn("codex", app_ts.lower())
        self.assertNotIn("claude", app_ts.lower())

        # Session ref resolution must prefer session_id before source_path (SQLite-backed sessions)
        self.assertIn("cand.session_id || cand.source_path", app_ts)
        self.assertIn("detail.import_record?.import_id || s.session_id || s.source_path", app_ts)
        self.assertIn("current.summary.session_id || current.summary.source_path", app_ts)
        self.assertIsNone(
            re.search(r"\bsource_path\s*\|\|\s*[^;\n]*session_id", app_ts),
            "ui_src/app.ts must never prefer source_path ahead of session_id",
        )
        self.assertIn("fetchInitialOpen(false)", app_ts)
        self.assertIn("if (!force && initialResultReceived) return;", app_ts)
        self.assertIn("refreshActiveView", app_ts)
        self.assertIn("!r.session_id && r.source_path === sourcePath", app_ts)
        self.assertIn("inline_mode", app_ts)
        self.assertIn("inlineMode", app_ts)

        for tool_name in (
            "open_session_importer",
            "app_list_sessions",
            "app_get_session_detail",
            "app_list_imports",
            "app_start_import",
            "app_complete_import",
            "app_delete_import",
            "com.google.antigravity/toggle-aux-pane",
            "fallback_to_cli",
            "cli_background",
        ):
            self.assertIn(tool_name, app_html, f"Missing {tool_name!r} in built app.html")

    def test_ui_bundle_csp_inline_script_escape_and_no_unsafe_innerhtml(self) -> None:
        self.assertTrue(_APP_TS_PATH.is_file(), f"Missing {_APP_TS_PATH}")
        self.assertTrue(_BUILD_MJS_PATH.is_file(), f"Missing {_BUILD_MJS_PATH}")

        app_ts = _APP_TS_PATH.read_text(encoding="utf-8")
        build_mjs = _BUILD_MJS_PATH.read_text(encoding="utf-8")

        # Strict XSS boundary: only safe DOM helpers
        self.assertNotIn(".innerHTML", app_ts)
        self.assertNotIn(".outerHTML", app_ts)
        self.assertNotIn(".insertAdjacentHTML", app_ts)
        self.assertNotIn("document.write", app_ts)
        self.assertIn("createElement", app_ts)
        self.assertIn("textContent", app_ts)
        self.assertIn("replaceChildren", app_ts)

        # Ensure build script escapes </script
        self.assertIn("<\\/script", build_mjs)

        node_bin = shutil.which("node")
        node_modules_dir = _UI_SRC_DIR / "node_modules"
        if node_bin and node_modules_dir.is_dir():
            proc = subprocess.run(
                [node_bin, str(_BUILD_MJS_PATH), "--check"],
                cwd=str(_REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                proc.returncode,
                0,
                msg=f"build.mjs --check failed:\nstdout={proc.stdout}\nstderr={proc.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
