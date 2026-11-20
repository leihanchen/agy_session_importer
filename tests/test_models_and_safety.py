"""Unit tests for normalized data models, timestamps, and safety filtering."""

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "skills" / "resume-session" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.models import (
    NormalizedTurn,
    SessionDetail,
    SessionSummary,
    ToolEvent,
    normalize_timestamp,
    timestamp_to_epoch_ms,
)
from importer.safety import (
    TRUST_BOUNDARY,
    clean_text,
    redact_secrets,
    sanitize_text,
    strip_wrappers,
    truncate_text,
)


class TestModelsAndSafety(unittest.TestCase):
    """Tests for Task 1 models, timestamp normalization, and safety filters."""

    def test_timestamp_normalization_sec_ms_iso(self):
        self.assertEqual(normalize_timestamp(1776916275), "2026-04-23T03:51:15Z")
        self.assertEqual(normalize_timestamp(1776916275000), "2026-04-23T03:51:15Z")
        self.assertEqual(normalize_timestamp("2026-11-19T15:30:00.123-05:00"), "2026-11-19T20:30:00Z")
        self.assertEqual(normalize_timestamp(None), "")
        self.assertGreater(timestamp_to_epoch_ms("2026-11-19T20:30:00Z"), 0)

    def test_timestamp_edge_cases(self):
        self.assertEqual(normalize_timestamp(""), "")
        self.assertEqual(normalize_timestamp("not-a-timestamp"), "")
        self.assertEqual(normalize_timestamp(True), "")
        self.assertEqual(normalize_timestamp("2026-11-19T20:30:00"), "2026-11-19T20:30:00Z")
        self.assertEqual(timestamp_to_epoch_ms(1776916275), 1776916275000)
        self.assertEqual(timestamp_to_epoch_ms(1776916275000), 1776916275000)
        self.assertEqual(timestamp_to_epoch_ms(None), 0)
        self.assertEqual(timestamp_to_epoch_ms("invalid"), 0)
        self.assertEqual(timestamp_to_epoch_ms(False), 0)

    def test_sanitize_text_control_chars(self):
        self.assertEqual(sanitize_text(None), "")
        raw = "hello\x00world\tline1\nline2\x07\x1b"
        cleaned = sanitize_text(raw)
        self.assertEqual(cleaned, "hello\ufffdworld\tline1\nline2\ufffd\ufffd")

    def test_strip_wrappers_preserves_surrounding_prose(self):
        raw = (
            "Before note\n"
            "<system-reminder>secret injected rule</system-reminder>\n"
            "Fix auth middleware\n"
            "<environment_context>\n<cwd>/tmp/repo</cwd>\n</environment_context>\n"
            "After note"
        )
        cleaned, _ = clean_text(raw)
        self.assertIn("Before note", cleaned)
        self.assertIn("Fix auth middleware", cleaned)
        self.assertIn("After note", cleaned)
        self.assertNotIn("secret injected rule", cleaned)
        self.assertNotIn("<environment_context>", cleaned)

    def test_strip_all_supported_wrappers(self):
        raw = (
            "Keep A\n"
            "<local-command-caveat>caveat</local-command-caveat>\n"
            "<turn_aborted>aborted</turn_aborted>\n"
            "<ide_opened_file>foo.py</ide_opened_file>\n"
            "<command-name>/clear</command-name>\n"
            "Keep B"
        )
        stripped = strip_wrappers(raw)
        self.assertIn("Keep A", stripped)
        self.assertIn("Keep B", stripped)
        for hidden in ("caveat", "aborted", "foo.py", "/clear"):
            self.assertNotIn(hidden, stripped)

    def test_redact_secrets_multiline_pem_and_tokens(self):
        fake_openai = "-".join(("sk", "proj", "abc1234567890xyz"))
        fake_ghp = "_".join(("ghp", "1234567890abcdefghijklmnopqrstuvwxyz"))
        fake_aws = "".join(("AKIA", "IOSFODNN7EXAMPLE"))
        pem_header = " ".join(("-----BEGIN", "RSA", "PRIVATE", "KEY-----"))
        pem_footer = " ".join(("-----END", "RSA", "PRIVATE", "KEY-----"))
        raw = (
            "token=Bearer eyJhbGciOiJIUzI1NiJ9.abc.def\n"
            f"OPENAI_API_KEY={fake_openai}\n"
            f"gh={fake_ghp}\n"
            f"aws={fake_aws}\n"
            f"{pem_header}\nMIIEowIBAAKCAQEAtest\n{pem_footer}\n"
            "keep normal text"
        )
        cleaned, count = clean_text(raw)
        self.assertGreaterEqual(count, 5)
        self.assertNotIn("sk-proj-", cleaned)
        self.assertNotIn("ghp_", cleaned)
        self.assertNotIn(fake_aws, cleaned)
        self.assertNotIn("MIIEowIBAAKCAQEAtest", cleaned)
        self.assertIn("[REDACTED]", cleaned)
        self.assertIn("keep normal text", cleaned)

    def test_redact_additional_secret_patterns(self):
        fake_anthropic = "-".join(("sk", "ant", "api03", "abcdef1234567890"))
        fake_gho = "_".join(("gho", "1234567890abcdefghijklmnopqrstuvwxyz"))
        fake_slack = "-".join(("xoxb", "123456789012", "123456789012", "abcdefghijklmnop"))
        raw = (
            f"anthropic={fake_anthropic}\n"
            f"gh_oauth={fake_gho}\n"
            f"slack={fake_slack}\n"
            "DB_PASSWORD='supersecretvalue'\n"
            "APP_SECRET=mysecret123\n"
        )
        cleaned, count = redact_secrets(raw)
        self.assertGreaterEqual(count, 5)
        self.assertNotIn("sk-ant-", cleaned)
        self.assertNotIn("gho_", cleaned)
        self.assertNotIn("xoxb-", cleaned)
        self.assertNotIn("supersecretvalue", cleaned)
        self.assertNotIn("mysecret123", cleaned)
        self.assertEqual(TRUST_BOUNDARY, "untrusted_inert_history")

    def test_clean_text_strip_tags_false(self):
        raw = "<system-reminder>keep tag</system-reminder> sk-proj-abc1234567890xyz"
        cleaned, count = clean_text(raw, strip_tags=False)
        self.assertIn("<system-reminder>keep tag</system-reminder>", cleaned)
        self.assertNotIn("sk-proj-", cleaned)
        self.assertEqual(count, 1)

    def test_dataclass_to_dict_and_truncate_text(self):
        detail = SessionDetail(
            summary=SessionSummary(
                provider="codex",
                session_id="abc",
                source_kind="rollout_jsonl",
                source_path="/tmp/a.jsonl",
                cwd="/repo",
                repo_name="repo",
                git_branch="main",
                title="Fix bug",
                first_user_prompt="hi",
                last_user_prompt="bye",
                created_at="2026-11-19T00:00:00Z",
                updated_at="2026-11-19T00:01:00Z",
                turn_count=1,
            ),
            turns=[
                NormalizedTurn(
                    turn_index=1,
                    timestamp="2026-11-19T00:00:00Z",
                    user_text="hi",
                    assistant_text="ok",
                    tool_events=[
                        ToolEvent(
                            call_id="call_1",
                            tool_name="read_file",
                            arguments_summary="path=a.py",
                            output_summary="ok",
                            status="success",
                            files_touched=["a.py"],
                        )
                    ],
                )
            ],
        )
        payload = detail.to_dict()
        self.assertEqual(payload["summary"]["provider"], "codex")
        self.assertEqual(len(payload["turns"]), 1)
        self.assertEqual(payload["turns"][0]["tool_events"][0]["tool_name"], "read_file")
        short, truncated = truncate_text("hello   world\nnext", 10, single_line=True)
        self.assertTrue(truncated)
        self.assertLessEqual(len(short), 13)
        self.assertNotIn("\n", short)
        unchanged, not_truncated = truncate_text("hello", 10)
        self.assertEqual(unchanged, "hello")
        self.assertFalse(not_truncated)


if __name__ == "__main__":
    unittest.main()
