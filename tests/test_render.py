"""Unit tests for full/compact rendering, context bounding, and turn drill-down."""

import re
import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "skills" / "resume-session" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.models import NormalizedTurn, SessionDetail, SessionSummary, ToolEvent
from importer.render import (
    DEFAULT_COMPACT_MAX_CHARS,
    DEFAULT_FULL_MAX_CHARS,
    format_list_text,
    format_show_text,
    render_show_envelope,
    render_turns_envelope,
)
from importer.safety import TRUST_BOUNDARY
from importer.search import ResolutionResult

_EXPECTED_SHOW_KEYS = {
    "schema_version",
    "status",
    "reason",
    "trust_boundary",
    "mode",
    "provider",
    "cross_repo",
    "session",
    "candidates",
    "rendered",
    "warnings",
}


def _make_summary(
    *,
    provider: str = "codex",
    session_id: str = "sess-12345678",
    cwd: str = "/tmp/repo",
    repo_name: str = "repo",
    git_branch: str = "feat/demo",
    title: str = "Implement feature X",
    first_user_prompt: str = "Goal: implement feature X cleanly.",
    last_user_prompt: str = "Please verify edge cases.",
    turn_count: int = 3,
    warnings: list[str] | None = None,
) -> SessionSummary:
    return SessionSummary(
        provider=provider,
        session_id=session_id,
        source_kind=f"{provider}_rollout",
        source_path=f"/tmp/{provider}/{session_id}.jsonl",
        cwd=cwd,
        repo_name=repo_name,
        git_branch=git_branch,
        title=title,
        first_user_prompt=first_user_prompt,
        last_user_prompt=last_user_prompt,
        created_at="2026-11-20T01:00:00Z",
        updated_at="2026-11-20T02:00:00Z",
        turn_count=turn_count,
        archived=False,
        warnings=list(warnings or []),
    )


def _make_turns(count: int, *, body_repeat: int = 1) -> list[NormalizedTurn]:
    turns: list[NormalizedTurn] = []
    for idx in range(1, count + 1):
        tool_events = []
        if idx % 2 == 0:
            tool_events.append(
                ToolEvent(
                    call_id=f"call-{idx}",
                    tool_name="Bash",
                    arguments_summary=f"pytest tests/test_mod_{idx}.py",
                    output_summary="1 passed",
                    status="ok",
                    files_touched=[f"src/mod_{idx}.py"],
                )
            )
        turns.append(
            NormalizedTurn(
                turn_index=idx,
                timestamp=f"2026-11-20T01:{idx:02d}:00Z",
                user_text=f"User prompt for turn {idx}: " + ("detail " * body_repeat).strip(),
                assistant_text=f"Assistant reply for turn {idx}: " + ("analysis " * body_repeat).strip(),
                tool_events=tool_events,
                compaction_before=(idx == 5),
                warnings=[f"turn_{idx}_note"] if idx == 3 else [],
            )
        )
    return turns


def _make_detail(
    turn_count: int = 20,
    *,
    provider: str = "codex",
    body_repeat: int = 12,
    session_id: str = "sess-render-001",
    warnings: list[str] | None = None,
) -> SessionDetail:
    turns = _make_turns(turn_count, body_repeat=body_repeat)
    summary = _make_summary(
        provider=provider,
        session_id=session_id,
        turn_count=len(turns),
        first_user_prompt=turns[0].user_text if turns else "Initial goal",
        last_user_prompt=turns[-1].user_text if turns else "Final prompt",
    )
    return SessionDetail(
        summary=summary,
        turns=turns,
        files_mentioned=["src/app.py", "tests/test_app.py"],
        commands_run=["pytest -q", "git status --short"],
        compaction_count=1 if turn_count >= 5 else 0,
        total_chars=sum(len(t.user_text) + len(t.assistant_text) for t in turns),
        warnings=list(warnings or ["compacted_history"]),
    )


class RenderTests(unittest.TestCase):
    def test_defaults_match_spec(self) -> None:
        self.assertEqual(DEFAULT_FULL_MAX_CHARS, 60_000)
        self.assertEqual(DEFAULT_COMPACT_MAX_CHARS, 12_000)

    def test_full_mode_bounding_retains_turn1_and_tail_with_drilldown_marker(self) -> None:
        detail = _make_detail(20, body_repeat=20, session_id="sess-20turns")
        result = ResolutionResult(
            status="matched",
            reason="latest_same_repo",
            provider="codex",
            cross_repo=False,
            detail=detail,
            candidates=[],
            warnings=list(detail.warnings),
        )

        envelope = render_show_envelope(result, mode="full", max_chars=2500)

        self.assertEqual(set(envelope.keys()), _EXPECTED_SHOW_KEYS)
        self.assertEqual(envelope["schema_version"], 1)
        self.assertEqual(envelope["status"], "matched")
        self.assertEqual(envelope["mode"], "full")
        self.assertEqual(envelope["trust_boundary"], TRUST_BOUNDARY)
        self.assertEqual(envelope["provider"], "codex")
        self.assertFalse(envelope["cross_repo"])
        self.assertIsInstance(envelope["session"], dict)
        self.assertEqual(envelope["candidates"], [])
        self.assertIn("bounded_history", envelope["warnings"])

        rendered = envelope["rendered"]
        self.assertLessEqual(len(rendered), 2500)
        self.assertIn("User prompt for turn 1:", rendered)
        self.assertIn("User prompt for turn 20:", rendered)

        marker_match = re.search(
            r"\[Turns 2\.\.(\d+) omitted due to max_chars budget; call turns sess-20turns --range 2:(\d+) to inspect\]",
            rendered,
        )
        self.assertIsNotNone(marker_match, f"Missing drill-down marker in:\n{rendered}")
        assert marker_match is not None
        self.assertEqual(marker_match.group(1), marker_match.group(2))
        omitted_end = int(marker_match.group(1))
        self.assertGreaterEqual(omitted_end, 2)
        self.assertLess(omitted_end, 20)
        # First retained tail turn (K = omitted_end + 1) must be present
        self.assertIn(f"User prompt for turn {omitted_end + 1}:", rendered)
        # Omitted middle turn should not be present
        self.assertNotIn("User prompt for turn 2:", rendered)

    def test_full_mode_truncates_oversized_single_turn(self) -> None:
        huge_turn = NormalizedTurn(
            turn_index=1,
            timestamp="2026-11-20T01:00:00Z",
            user_text="Build a huge index: " + ("A" * 50_000),
            assistant_text="Result summary: " + ("B" * 50_000),
            tool_events=[],
        )
        detail = SessionDetail(
            summary=_make_summary(session_id="sess-huge-1", turn_count=1),
            turns=[huge_turn],
            total_chars=100_000,
            warnings=[],
        )
        result = ResolutionResult(
            status="matched",
            reason="session_id",
            provider="codex",
            cross_repo=False,
            detail=detail,
            candidates=[],
            warnings=[],
        )

        envelope = render_show_envelope(result, mode="full", max_chars=4000)

        self.assertEqual(set(envelope.keys()), _EXPECTED_SHOW_KEYS)
        rendered = envelope["rendered"]
        self.assertTrue(rendered)
        self.assertLessEqual(len(rendered), 4000)
        self.assertIn("bounded_history", envelope["warnings"])
        self.assertIn("Build a huge index:", rendered)

    def test_full_mode_tail_turns_option_bounds_to_turn1_and_tail(self) -> None:
        detail = _make_detail(10, body_repeat=2, session_id="sess-tail")
        result = ResolutionResult(
            status="matched",
            reason="latest_same_repo",
            provider="codex",
            cross_repo=False,
            detail=detail,
        )

        envelope = render_show_envelope(result, mode="full", max_chars=60_000, tail_turns=2)
        rendered = envelope["rendered"]

        self.assertIn("User prompt for turn 1:", rendered)
        self.assertIn("User prompt for turn 9:", rendered)
        self.assertIn("User prompt for turn 10:", rendered)
        self.assertNotIn("User prompt for turn 5:", rendered)
        self.assertIn(
            "[Turns 2..8 omitted due to max_chars budget; call turns sess-tail --range 2:8 to inspect]",
            rendered,
        )
        self.assertIn("bounded_history", envelope["warnings"])

        zero_tail = render_show_envelope(result, mode="full", max_chars=60_000, tail_turns=0)
        self.assertIn("User prompt for turn 1:", zero_tail["rendered"])
        self.assertNotIn("User prompt for turn 10:", zero_tail["rendered"])
        self.assertIn(
            "[Turns 2..10 omitted due to max_chars budget; call turns sess-tail --range 2:10 to inspect]",
            zero_tail["rendered"],
        )
        self.assertIn("bounded_history", zero_tail["warnings"])

    def test_full_mode_unbounded_includes_all_turns_and_tool_events(self) -> None:
        detail = _make_detail(4, body_repeat=1, session_id="sess-small", warnings=[])
        result = ResolutionResult(
            status="matched",
            reason="latest_same_repo",
            provider="codex",
            cross_repo=False,
            detail=detail,
        )

        envelope = render_show_envelope(result, mode="full")
        rendered = envelope["rendered"]

        self.assertNotIn("bounded_history", envelope["warnings"])
        for idx in range(1, 5):
            self.assertIn(f"User prompt for turn {idx}:", rendered)
            self.assertIn(f"Assistant reply for turn {idx}:", rendered)
        self.assertIn("Bash", rendered)
        self.assertIn("pytest tests/test_mod_2.py", rendered)
        self.assertIn("1 passed", rendered)

    def test_compact_mode_structure_and_budget(self) -> None:
        detail = _make_detail(12, provider="claude", body_repeat=40, session_id="sess-compact", warnings=["compacted_history", "secrets_redacted:1"])
        detail.turns[0].user_text = "Initial goal: fix OAuth token refresh race condition."
        detail.turns[-1].user_text = "Final request: run the regression suite and summarize."
        detail.summary.first_user_prompt = detail.turns[0].user_text
        detail.summary.last_user_prompt = detail.turns[-1].user_text

        result = ResolutionResult(
            status="matched",
            reason="latest_same_repo",
            provider="claude",
            cross_repo=False,
            detail=detail,
            warnings=list(detail.warnings),
        )

        envelope = render_show_envelope(result, mode="compact")
        self.assertEqual(set(envelope.keys()), _EXPECTED_SHOW_KEYS)
        self.assertEqual(envelope["mode"], "compact")
        self.assertEqual(envelope["trust_boundary"], TRUST_BOUNDARY)
        rendered = envelope["rendered"]
        self.assertLessEqual(len(rendered), DEFAULT_COMPACT_MAX_CHARS)

        # Metadata header
        self.assertIn("sess-compact", rendered)
        self.assertIn("claude", rendered)
        self.assertIn("feat/demo", rendered)
        # Turn 1 user goal & final user prompt
        self.assertIn("Initial goal: fix OAuth token refresh race condition.", rendered)
        self.assertIn("Final request: run the regression suite and summarize.", rendered)
        # Condensed earlier outline + fuller last 2-3 turns
        self.assertIn("Turn 2", rendered)
        self.assertIn("User prompt for turn 10:", rendered)
        self.assertIn("Assistant reply for turn 11:", rendered)
        # files_mentioned, commands_run, warnings
        self.assertIn("src/app.py", rendered)
        self.assertIn("pytest -q", rendered)
        self.assertIn("compacted_history", rendered)
        self.assertIn("secrets_redacted:1", rendered)

        # Custom tight budget must also be respected strictly
        tight = render_show_envelope(result, mode="compact", max_chars=900)
        self.assertLessEqual(len(tight["rendered"]), 900)
        self.assertTrue(tight["rendered"])
        self.assertIn("bounded_history", tight["warnings"])

    def test_compact_mode_tight_budget_short_sessions_emit_valid_drilldown_range(self) -> None:
        for turn_count, budget in ((2, 1100), (3, 1300), (5, 1400)):
            detail = _make_detail(
                turn_count,
                provider="codex",
                body_repeat=45,
                session_id=f"sess-short-{turn_count}",
                warnings=[],
            )
            result = ResolutionResult(
                status="matched",
                reason="latest_same_repo",
                provider="codex",
                cross_repo=False,
                detail=detail,
            )
            env = render_show_envelope(result, mode="compact", max_chars=budget)
            rendered = env["rendered"]
            self.assertLessEqual(len(rendered), budget)
            self.assertIn("bounded_history", env["warnings"])
            self.assertNotIn("--range 2:1", rendered)

            ranges = re.findall(r"--range (\d+):(\d+)", rendered)
            self.assertTrue(ranges, f"Expected a valid drill-down --range marker in:\n{rendered}")
            for start_s, end_s in ranges:
                start_i, end_i = int(start_s), int(end_s)
                self.assertEqual(start_i, 1)
                self.assertGreaterEqual(end_i, start_i)
                drill = render_turns_envelope(detail, start_i, end_i)
                self.assertEqual(drill["start"], start_i)
                self.assertEqual(drill["end"], end_i)

    def test_ambiguous_and_no_match_envelopes(self) -> None:
        cand1 = _make_summary(provider="codex", session_id="cand-1", title="First candidate")
        cand2 = _make_summary(provider="claude", session_id="cand-2", title="Second candidate")

        ambiguous_res = ResolutionResult(
            status="ambiguous",
            reason="multiple_candidates",
            provider="",
            cross_repo=False,
            detail=None,
            candidates=[cand1, cand2],
            warnings=["partial_index"],
        )
        amb_env = render_show_envelope(ambiguous_res, mode="full")
        self.assertEqual(set(amb_env.keys()), _EXPECTED_SHOW_KEYS)
        self.assertEqual(amb_env["status"], "ambiguous")
        self.assertEqual(amb_env["reason"], "multiple_candidates")
        self.assertEqual(amb_env["trust_boundary"], TRUST_BOUNDARY)
        self.assertIsNone(amb_env["session"])
        self.assertEqual(amb_env["rendered"], "")
        self.assertEqual(len(amb_env["candidates"]), 2)
        self.assertEqual(amb_env["candidates"][0]["session_id"], "cand-1")
        self.assertEqual(amb_env["warnings"], ["partial_index"])

        no_match_res = ResolutionResult(
            status="no_match",
            reason="no_keyword_matches",
            provider="codex",
            cross_repo=False,
            detail=None,
            candidates=[cand1],
            warnings=[],
        )
        nm_env = render_show_envelope(no_match_res, mode="compact")
        self.assertEqual(set(nm_env.keys()), _EXPECTED_SHOW_KEYS)
        self.assertEqual(nm_env["status"], "no_match")
        self.assertEqual(nm_env["mode"], "compact")
        self.assertEqual(nm_env["trust_boundary"], TRUST_BOUNDARY)
        self.assertIsNone(nm_env["session"])
        self.assertEqual(nm_env["rendered"], "")
        self.assertEqual(len(nm_env["candidates"]), 1)

    def test_turns_range_and_validation_errors(self) -> None:
        detail = _make_detail(6, body_repeat=1, session_id="sess-drill", warnings=["w1"])

        env = render_turns_envelope(detail, 2, 4)
        self.assertEqual(
            set(env.keys()),
            {
                "schema_version",
                "trust_boundary",
                "provider",
                "session_id",
                "start",
                "end",
                "total_turns",
                "turns",
                "warnings",
            },
        )
        self.assertEqual(env["schema_version"], 1)
        self.assertEqual(env["trust_boundary"], TRUST_BOUNDARY)
        self.assertEqual(env["provider"], "codex")
        self.assertEqual(env["session_id"], "sess-drill")
        self.assertEqual(env["start"], 2)
        self.assertEqual(env["end"], 4)
        self.assertEqual(env["total_turns"], 6)
        self.assertEqual([t["turn_index"] for t in env["turns"]], [2, 3, 4])
        self.assertEqual(env["warnings"], ["w1"])

        # Clamp end to total_turns when end > len(detail.turns)
        clamped = render_turns_envelope(detail, 5, 99)
        self.assertEqual(clamped["start"], 5)
        self.assertEqual(clamped["end"], 6)
        self.assertEqual([t["turn_index"] for t in clamped["turns"]], [5, 6])

        # Validation errors
        with self.assertRaises(ValueError):
            render_turns_envelope(detail, 0, 2)
        with self.assertRaises(ValueError):
            render_turns_envelope(detail, 4, 2)
        with self.assertRaises(ValueError):
            render_turns_envelope(detail, 7, 8)

        # Empty turns behavior per Pre-flight Ruling 3
        empty_detail = _make_detail(0, warnings=["metadata_only"])
        empty_ok = render_turns_envelope(empty_detail, 1, 5)
        self.assertEqual(empty_ok["start"], 1)
        self.assertEqual(empty_ok["end"], 0)
        self.assertEqual(empty_ok["total_turns"], 0)
        self.assertEqual(empty_ok["turns"], [])
        with self.assertRaises(ValueError):
            render_turns_envelope(empty_detail, 2, 3)
        with self.assertRaises(ValueError):
            render_turns_envelope(empty_detail, 1, 0)

    def test_invalid_mode_and_tail_turns_raise_value_error(self) -> None:
        detail = _make_detail(2, body_repeat=1)
        result = ResolutionResult(
            status="matched",
            reason="latest_same_repo",
            provider="codex",
            cross_repo=False,
            detail=detail,
        )
        with self.assertRaises(ValueError):
            render_show_envelope(result, mode="invalid")
        with self.assertRaises(ValueError):
            render_show_envelope(result, mode="full", tail_turns=-1)

    def test_format_list_text_and_format_show_text(self) -> None:
        s1 = _make_summary(provider="codex", session_id="id-1", title="Fix auth")
        list_text = format_list_text([s1], ["sqlite_locked"])
        self.assertIn("id-1", list_text)
        self.assertIn("Fix auth", list_text)
        self.assertIn("sqlite_locked", list_text)

        empty_list_text = format_list_text([], ["warn_a"])
        self.assertIn("No matching sessions", empty_list_text)
        self.assertIn("warn_a", empty_list_text)

        amb_env = render_show_envelope(
            ResolutionResult(
                status="ambiguous",
                reason="multiple_candidates",
                provider="codex",
                cross_repo=False,
                detail=None,
                candidates=[s1],
                warnings=["partial"],
            )
        )
        amb_text = format_show_text(amb_env)
        self.assertIn("ambiguous", amb_text)
        self.assertIn("id-1", amb_text)
        self.assertIn("partial", amb_text)


if __name__ == "__main__":
    unittest.main()
