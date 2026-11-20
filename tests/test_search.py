"""Unit tests for Unicode-aware search, scoring, and session resolution."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "skills" / "resume-session" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.models import NormalizedTurn, SessionDetail, SessionSummary
from importer.providers import ProviderReadError, ProviderRegistry, SessionProvider
from importer.search import (
    ResolutionResult,
    list_sessions,
    resolve_session,
    tokenize_query,
)


def _iso_minutes_ago(minutes: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_summary(
    *,
    provider: str,
    session_id: str,
    cwd: str,
    repo_name: str = "repo",
    git_branch: str = "main",
    title: str = "",
    first_user_prompt: str = "",
    last_user_prompt: str = "",
    created_at: str = "2026-11-20T00:00:00Z",
    updated_at: str = "2026-11-20T00:01:00Z",
    source_path: str = "",
    turn_count: int = 1,
    archived: bool = False,
) -> SessionSummary:
    return SessionSummary(
        provider=provider,
        session_id=session_id,
        source_kind=f"{provider}_log",
        source_path=source_path or f"/tmp/{provider}/{session_id}.log",
        cwd=cwd,
        repo_name=repo_name,
        git_branch=git_branch,
        title=title,
        first_user_prompt=first_user_prompt,
        last_user_prompt=last_user_prompt,
        created_at=created_at,
        updated_at=updated_at,
        turn_count=turn_count,
        archived=archived,
    )


class MockSearchProvider(SessionProvider):
    """Configurable mock provider for testing search and resolution without disk storage."""

    def __init__(
        self,
        name: str,
        *,
        aliases: tuple[str, ...] = (),
        suffix: str = ".mock",
        sessions: list[SessionSummary] | None = None,
        bodies: dict[str, str] | None = None,
        available: bool = True,
        raise_on_list: bool = False,
        raise_on_load: bool = False,
        raise_on_load_refs: set[str] | None = None,
    ) -> None:
        self.name = name
        self.display_name = name.upper()
        self.aliases = aliases
        self._suffix = suffix
        self._sessions = list(sessions or [])
        self._bodies = dict(bodies or {})
        self._available = available
        self._raise_on_list = raise_on_list
        self._raise_on_load = raise_on_load
        self._raise_on_load_refs = set(raise_on_load_refs or ())
        self.extract_calls: list[tuple[str, int]] = []
        self.load_calls: list[tuple[str, int]] = []

    def is_available(self) -> bool:
        return self._available

    def list_sessions(
        self, *, include_archived: bool = False
    ) -> tuple[list[SessionSummary], list[str]]:
        if self._raise_on_list:
            raise ProviderReadError(f"{self.name} storage unreadable")
        items = [s for s in self._sessions if include_archived or not s.archived]
        return items, []

    def can_handle_path(self, path: Path) -> bool:
        return Path(path).suffix == self._suffix

    def load_session(self, ref: str, *, max_tool_chars: int = 600) -> SessionDetail:
        self.load_calls.append((ref, max_tool_chars))
        ref_clean = ref.strip()
        if self._raise_on_load or ref_clean in self._raise_on_load_refs:
            raise ProviderReadError(f"{self.name} failed reading {ref_clean}")
        target = None
        for s in self._sessions:
            if s.session_id.lower() == ref_clean.lower() or s.source_path == ref_clean:
                target = s
                break
        if target is None:
            p = Path(ref_clean)
            if p.is_file():
                for s in self._sessions:
                    if Path(s.source_path).resolve() == p.resolve():
                        target = s
                        break
        if target is None:
            raise ProviderReadError(f"{self.name} session not found: {ref}")
        body = self._bodies.get(target.session_id, target.first_user_prompt or "hello")
        return SessionDetail(
            summary=target,
            turns=[
                NormalizedTurn(
                    turn_index=1,
                    timestamp=target.created_at,
                    user_text=target.first_user_prompt or "prompt",
                    assistant_text=body,
                )
            ],
        )

    def extract_searchable_text(
        self, summary: SessionSummary, *, max_bytes: int = 262_144
    ) -> str:
        self.extract_calls.append((summary.session_id, max_bytes))
        return self._bodies.get(summary.session_id, "")[:max_bytes]


class TestSearchAndResolution(unittest.TestCase):
    """Tests for Task 5 search, scoring, and disambiguation rules."""

    def test_latest_same_repo_vs_no_sessions_for_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "payments"
            (repo / ".git").mkdir(parents=True)
            sub = repo / "src"
            sub.mkdir()
            other_repo = base / "other"
            (other_repo / ".git").mkdir(parents=True)
            empty_repo = base / "empty"
            (empty_repo / ".git").mkdir(parents=True)

            codex = MockSearchProvider(
                "codex",
                sessions=[
                    _make_summary(
                        provider="codex",
                        session_id="c-old",
                        cwd=str(repo),
                        repo_name="payments",
                        title="Old same repo",
                        updated_at="2026-11-20T01:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="c-other-newer",
                        cwd=str(other_repo),
                        repo_name="other",
                        title="Newer other repo",
                        updated_at="2026-11-20T05:00:00Z",
                    ),
                ],
            )
            claude = MockSearchProvider(
                "claude",
                aliases=("claude-code", "cc"),
                sessions=[
                    _make_summary(
                        provider="claude",
                        session_id="cl-newest-same",
                        cwd=str(sub),
                        repo_name="payments",
                        title="Newest same repo subdir",
                        updated_at="2026-11-20T03:00:00Z",
                    ),
                ],
            )
            registry = ProviderRegistry()
            registry.register(codex)
            registry.register(claude)

            for ref_val in ("", "latest", None):
                res = resolve_session(registry, ref_val, cwd=str(repo))
                self.assertIsInstance(res, ResolutionResult)
                self.assertEqual(res.status, "matched")
                self.assertEqual(res.provider, "claude")
                self.assertFalse(res.cross_repo)
                self.assertIsNotNone(res.detail)
                self.assertEqual(res.detail.summary.session_id, "cl-newest-same")

            # Equal updated_at tie-break prefers exact cwd over sibling subdir
            tie_provider = MockSearchProvider(
                "gemini_cli",
                sessions=[
                    _make_summary(
                        provider="gemini_cli",
                        session_id="g-sub",
                        cwd=str(sub),
                        updated_at="2026-11-20T10:00:00Z",
                    ),
                    _make_summary(
                        provider="gemini_cli",
                        session_id="g-exact",
                        cwd=str(repo),
                        updated_at="2026-11-20T10:00:00Z",
                    ),
                ],
            )
            tie_reg = ProviderRegistry()
            tie_reg.register(tie_provider)
            tie_res = resolve_session(tie_reg, "latest", cwd=str(repo))
            self.assertEqual(tie_res.status, "matched")
            self.assertEqual(tie_res.detail.summary.session_id, "g-exact")

            # Empty repo with > 8 cross-repo sessions returns no_sessions_for_cwd + up to 8 candidates
            many_sessions = [
                _make_summary(
                    provider="gemini_cli",
                    session_id=f"other-{i:02d}",
                    cwd=str(other_repo),
                    repo_name="other",
                    updated_at=f"2026-11-20T00:{i:02d}:00Z",
                )
                for i in range(12)
            ]
            many_reg = ProviderRegistry()
            many_reg.register(MockSearchProvider("gemini_cli", sessions=many_sessions))
            no_res = resolve_session(many_reg, "", cwd=str(empty_repo))
            self.assertEqual(no_res.status, "no_match")
            self.assertEqual(no_res.reason, "no_sessions_for_cwd")
            self.assertIsNone(no_res.detail)
            self.assertEqual(len(no_res.candidates), 8)
            self.assertEqual(no_res.candidates[0].session_id, "other-11")

    def test_cross_repo_id_confirmation_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo_a = base / "repo_a"
            repo_b = base / "repo_b"
            (repo_a / ".git").mkdir(parents=True)
            (repo_b / ".git").mkdir(parents=True)

            explicit_file = repo_b / "session_explicit.gem"
            explicit_file.write_text("dummy", encoding="utf-8")
            unrelated_file = base / "notes.txt"
            unrelated_file.write_text("notes", encoding="utf-8")

            sid_cross = "550e8400-e29b-41d4-a716-446655440000"
            sid_ambig_1 = "abcdef12-0000-4000-8000-000000000001"
            sid_ambig_2 = "abcdef12-0000-4000-8000-000000000002"
            sid_same = "deadbeef-1111-4111-8111-111111111111"

            gem = MockSearchProvider(
                "gemini_cli",
                aliases=("gemini",),
                suffix=".gem",
                sessions=[
                    _make_summary(
                        provider="gemini_cli",
                        session_id=sid_cross,
                        cwd=str(repo_b),
                        repo_name="repo_b",
                        source_path=str(explicit_file),
                        title="Cross repo session",
                    ),
                    _make_summary(
                        provider="gemini_cli",
                        session_id=sid_ambig_1,
                        cwd=str(repo_a),
                        repo_name="repo_a",
                    ),
                    _make_summary(
                        provider="gemini_cli",
                        session_id=sid_ambig_2,
                        cwd=str(repo_a),
                        repo_name="repo_a",
                    ),
                    _make_summary(
                        provider="gemini_cli",
                        session_id=sid_same,
                        cwd=str(repo_a),
                        repo_name="repo_a",
                    ),
                ],
            )
            registry = ProviderRegistry()
            registry.register(gem)

            # Exact UUID in another repo -> ambiguous confirmation gate
            res_uuid = resolve_session(registry, sid_cross, cwd=str(repo_a))
            self.assertEqual(res_uuid.status, "ambiguous")
            self.assertEqual(res_uuid.reason, "cross_repo_confirmation_required")
            self.assertTrue(res_uuid.cross_repo)
            self.assertIsNone(res_uuid.detail)
            self.assertEqual([c.session_id for c in res_uuid.candidates], [sid_cross])

            # Confirm with allow_cross_repo=True -> matched
            res_uuid_ok = resolve_session(
                registry, sid_cross, cwd=str(repo_a), allow_cross_repo=True
            )
            self.assertEqual(res_uuid_ok.status, "matched")
            self.assertTrue(res_uuid_ok.cross_repo)
            self.assertEqual(res_uuid_ok.provider, "gemini_cli")
            self.assertIsNotNone(res_uuid_ok.detail)
            self.assertEqual(res_uuid_ok.detail.summary.session_id, sid_cross)

            # Unique >=8-char prefix (uppercase) in another repo
            res_prefix = resolve_session(registry, "550E8400", cwd=str(repo_a))
            self.assertEqual(res_prefix.status, "ambiguous")
            self.assertEqual(res_prefix.reason, "cross_repo_confirmation_required")
            self.assertTrue(res_prefix.cross_repo)

            res_prefix_ok = resolve_session(
                registry, "550E8400", cwd=str(repo_a), allow_cross_repo=True
            )
            self.assertEqual(res_prefix_ok.status, "matched")
            self.assertTrue(res_prefix_ok.cross_repo)
            self.assertEqual(res_prefix_ok.detail.summary.session_id, sid_cross)

            # Same-repo >=8-char prefix loads immediately without cross-repo gate
            res_same = resolve_session(registry, "deadbeef", cwd=str(repo_a))
            self.assertEqual(res_same.status, "matched")
            self.assertFalse(res_same.cross_repo)
            self.assertEqual(res_same.detail.summary.session_id, sid_same)

            # Non-unique >=8-char prefix -> ambiguous_session_id
            res_ambig = resolve_session(registry, "abcdef12", cwd=str(repo_a))
            self.assertEqual(res_ambig.status, "ambiguous")
            self.assertEqual(len(res_ambig.candidates), 2)
            self.assertIsNone(res_ambig.detail)

            # Explicit file path follows same cross-repo gate
            res_path = resolve_session(registry, str(explicit_file), cwd=str(repo_a))
            self.assertEqual(res_path.status, "ambiguous")
            self.assertEqual(res_path.reason, "cross_repo_confirmation_required")
            self.assertTrue(res_path.cross_repo)
            self.assertIsNone(res_path.detail)

            res_path_ok = resolve_session(
                registry, str(explicit_file), cwd=str(repo_a), allow_cross_repo=True
            )
            self.assertEqual(res_path_ok.status, "matched")
            self.assertTrue(res_path_ok.cross_repo)
            self.assertEqual(res_path_ok.detail.summary.session_id, sid_cross)

            # Explicit file path in same repo loads immediately
            res_path_same = resolve_session(registry, str(explicit_file), cwd=str(repo_b))
            self.assertEqual(res_path_same.status, "matched")
            self.assertFalse(res_path_same.cross_repo)

            # Unrecognized explicit file path -> no_match
            res_unknown_path = resolve_session(registry, str(unrelated_file), cwd=str(repo_a))
            self.assertEqual(res_unknown_path.status, "no_match")
            self.assertIsNone(res_unknown_path.detail)

    def test_natural_language_scoring_and_stage_b_body_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "app"
            (repo / ".git").mkdir(parents=True)
            other = base / "other"
            (other / ".git").mkdir(parents=True)

            # 1. Stage A weights & confidence (>=1.5x with all tokens matched)
            prov = MockSearchProvider(
                "codex",
                sessions=[
                    _make_summary(
                        provider="codex",
                        session_id="s-title",
                        cwd=str(repo),
                        title="OAuth refresh race",
                        updated_at="2026-11-20T01:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="s-prompt",
                        cwd=str(repo),
                        first_user_prompt="Investigate OAuth refresh",
                        updated_at="2026-11-20T02:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="s-other-repo",
                        cwd=str(other),
                        title="Billing export",
                        updated_at="2026-11-20T03:00:00Z",
                    ),
                ],
                bodies={"s-title": "details", "s-prompt": "details", "s-other-repo": "details"},
            )
            reg = ProviderRegistry()
            reg.register(prov)

            # title (4.0*2*1.3=10.4) vs prompt (2.5*2*1.3=6.5): 10.4 >= 1.5*6.5 (9.75) and all tokens matched
            res = resolve_session(reg, "oauth refresh", cwd=str(repo))
            self.assertEqual(res.status, "matched")
            self.assertEqual(res.detail.summary.session_id, "s-title")
            # Stage B must NOT be scanned when Stage A matched
            self.assertEqual(prov.extract_calls, [])

            # 2. Close Stage A competition -> ambiguous, max 8 candidates ordered by score then recency
            close_sessions = [
                _make_summary(
                    provider="codex",
                    session_id="m-title",
                    cwd=str(repo),
                    title="Token parser",
                    updated_at="2026-11-20T01:00:00Z",
                ),
                _make_summary(
                    provider="codex",
                    session_id="m-branch",
                    cwd=str(repo),
                    git_branch="fix-token-parser",
                    updated_at="2026-11-20T02:00:00Z",
                ),
            ] + [
                _make_summary(
                    provider="codex",
                    session_id=f"m-extra-{i}",
                    cwd=str(repo),
                    first_user_prompt="token parser",
                    updated_at=f"2026-11-20T00:{10 + i:02d}:00Z",
                )
                for i in range(8)
            ]
            close_reg = ProviderRegistry()
            close_reg.register(MockSearchProvider("codex", sessions=close_sessions))
            # 4.0 vs 3.0 -> 4.0 < 1.5 * 3.0 (4.5), so ambiguous with max 8 candidates
            ambig_res = resolve_session(close_reg, "token parser", cwd=str(repo))
            self.assertEqual(ambig_res.status, "ambiguous")
            self.assertIsNone(ambig_res.detail)
            self.assertEqual(len(ambig_res.candidates), 8)
            self.assertEqual(ambig_res.candidates[0].session_id, "m-title")
            self.assertEqual(ambig_res.candidates[1].session_id, "m-branch")

            # 3. Same-repo x1.3 boost beats cross-repo when metadata fields equal
            boost_reg = ProviderRegistry()
            boost_reg.register(
                MockSearchProvider(
                    "gemini_cli",
                    sessions=[
                        _make_summary(
                            provider="gemini_cli",
                            session_id="b-cross",
                            cwd=str(other),
                            first_user_prompt="zephyr protocol",
                            updated_at="2026-11-20T05:00:00Z",
                        ),
                        _make_summary(
                            provider="gemini_cli",
                            session_id="b-same",
                            cwd=str(repo),
                            first_user_prompt="zephyr",
                            updated_at="2026-11-20T01:00:00Z",
                        ),
                    ],
                )
            )
            # Only cross-repo matched "protocol" -> b-cross wins Stage A (5.0) over b-same (3.25) -> cross-repo top => ambiguous
            cross_top = resolve_session(boost_reg, "zephyr protocol", cwd=str(repo))
            self.assertEqual(cross_top.status, "ambiguous")
            self.assertTrue(cross_top.cross_repo)
            self.assertEqual(cross_top.candidates[0].session_id, "b-cross")

            # Cross-repo sole match is always ambiguous (requires same-repo for matched)
            sole_cross_reg = ProviderRegistry()
            sole_cross_reg.register(
                MockSearchProvider(
                    "gemini_cli",
                    sessions=[
                        _make_summary(
                            provider="gemini_cli",
                            session_id="only-cross",
                            cwd=str(other),
                            title="Quasar migration",
                        )
                    ],
                )
            )
            sole_cross_res = resolve_session(sole_cross_reg, "quasar migration", cwd=str(repo))
            self.assertEqual(sole_cross_res.status, "ambiguous")
            self.assertTrue(sole_cross_res.cross_repo)

            # 4. Stage B body fallback: 0 Stage A matches -> scans up to 40 recent sessions (current repo first)
            stage_b_sessions = [
                _make_summary(
                    provider="claude",
                    session_id="sb-same",
                    cwd=str(repo),
                    title="Unrelated work",
                    updated_at="2026-11-19T00:00:00Z",
                )
            ] + [
                _make_summary(
                    provider="claude",
                    session_id=f"sb-other-{i:02d}",
                    cwd=str(other),
                    title=f"Other {i}",
                    updated_at=f"2026-11-20T00:{i:02d}:00Z",
                )
                for i in range(45)
            ]
            stage_b_bodies = {
                "sb-same": "Investigated kubernetes pod autoscaling failure",
                # Oldest cross-repo session (45th) would fall outside the 40-session cap
                "sb-other-00": "kubernetes autoscaling in oldest session",
            }
            sb_provider = MockSearchProvider("claude", sessions=stage_b_sessions, bodies=stage_b_bodies)
            sb_reg = ProviderRegistry()
            sb_reg.register(sb_provider)

            # 2-token body hit in same repo: 2 * 1.5 * 1.3 = 3.9 >= 2.0 -> matched!
            sb_res = resolve_session(sb_reg, "kubernetes autoscaling", cwd=str(repo))
            self.assertEqual(sb_res.status, "matched")
            self.assertEqual(sb_res.detail.summary.session_id, "sb-same")
            self.assertEqual(len(sb_provider.extract_calls), 40)
            self.assertEqual(sb_provider.extract_calls[0], ("sb-same", 262_144))
            scanned_ids = {sid for sid, _ in sb_provider.extract_calls}
            self.assertNotIn("sb-other-00", scanned_ids)

            # 1-token Stage B body hit: 1.5 * 1.3 = 1.95 < 2.0 -> ambiguous (no false auto-match)
            sb_provider.extract_calls.clear()
            sb_weak = resolve_session(sb_reg, "kubernetes", cwd=str(repo))
            self.assertEqual(sb_weak.status, "ambiguous")
            self.assertIsNone(sb_weak.detail)
            self.assertEqual(sb_weak.candidates[0].session_id, "sb-same")

    def test_cjk_query_and_stopword_only_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            (repo / ".git").mkdir(parents=True)
            other = base / "other"
            (other / ".git").mkdir(parents=True)

            cjk_tokens = tokenize_query("修复登录问题")
            self.assertTrue(len(cjk_tokens) > 0)
            self.assertIn("修复", cjk_tokens)
            self.assertIn("登录", cjk_tokens)
            self.assertIn("问题", cjk_tokens)
            self.assertEqual(tokenize_query("锁"), ["锁"])

            codex = MockSearchProvider(
                "codex",
                sessions=[
                    _make_summary(
                        provider="codex",
                        session_id="cjk-target",
                        cwd=str(repo),
                        title="修复用户登录问题并补充单测",
                        updated_at="2026-11-20T02:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="cjk-noise",
                        cwd=str(repo),
                        title="修改 README 文档",
                        updated_at="2026-11-20T03:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="lock-target",
                        cwd=str(repo),
                        title="并发死锁排查",
                        updated_at="2026-11-20T01:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="cross-recent",
                        cwd=str(other),
                        title="Other repo latest",
                        updated_at="2026-11-20T09:00:00Z",
                    ),
                ],
            )
            gemini = MockSearchProvider(
                "gemini_cli",
                aliases=("gemini",),
                sessions=[
                    _make_summary(
                        provider="gemini_cli",
                        session_id="g-same",
                        cwd=str(repo),
                        title="Add metrics",
                        updated_at="2026-11-20T04:00:00Z",
                    )
                ],
            )
            reg = ProviderRegistry()
            reg.register(codex)
            reg.register(gemini)

            # CJK query matches without whitespace
            cjk_res = resolve_session(reg, "修复登录问题", cwd=str(repo))
            self.assertEqual(cjk_res.status, "matched")
            self.assertEqual(cjk_res.detail.summary.session_id, "cjk-target")

            # Single CJK character query
            lock_res = resolve_session(reg, "锁", cwd=str(repo))
            self.assertEqual(lock_res.status, "matched")
            self.assertEqual(lock_res.detail.summary.session_id, "lock-target")

            # Stop-word-only query using dynamic registry tokens
            self.assertEqual(
                tokenize_query("resume my codex session", extra_stop_words=reg.all_tokens()),
                [],
            )
            self.assertEqual(
                tokenize_query("continue gemini_cli session", extra_stop_words=reg.all_tokens()),
                [],
            )

            stop_res = resolve_session(reg, "resume my codex session", cwd=str(repo))
            self.assertEqual(stop_res.status, "no_match")
            self.assertIsNone(stop_res.detail)
            self.assertTrue(len(stop_res.candidates) > 0)
            # Current-repo candidates appear before cross-repo candidates on no_match
            self.assertEqual(stop_res.candidates[0].session_id, "g-same")
            self.assertEqual(stop_res.candidates[-1].session_id, "cross-recent")

            # Unmatched query also returns no_match with current-repo-first candidates
            miss_res = resolve_session(reg, "nonexistent_topic_xyz", cwd=str(repo))
            self.assertEqual(miss_res.status, "no_match")
            self.assertEqual(miss_res.candidates[0].session_id, "g-same")

    def test_list_sessions_filters_within_min_and_all_cwds(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            (repo / ".git").mkdir(parents=True)
            other = base / "other"
            (other / ".git").mkdir(parents=True)

            codex = MockSearchProvider(
                "codex",
                sessions=[
                    _make_summary(
                        provider="codex",
                        session_id="c-recent",
                        cwd=str(repo),
                        title="Recent codex",
                        updated_at=_iso_minutes_ago(5),
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="c-old",
                        cwd=str(repo),
                        title="Old codex",
                        updated_at=_iso_minutes_ago(120),
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="c-archived",
                        cwd=str(repo),
                        title="Archived codex",
                        updated_at=_iso_minutes_ago(2),
                        archived=True,
                    ),
                ],
            )
            gemini = MockSearchProvider(
                "gemini_cli",
                aliases=("gemini",),
                sessions=[
                    _make_summary(
                        provider="gemini_cli",
                        session_id="g-same",
                        cwd=str(repo),
                        title="Recent gemini auth",
                        updated_at=_iso_minutes_ago(3),
                    ),
                    _make_summary(
                        provider="gemini_cli",
                        session_id="g-other",
                        cwd=str(other),
                        title="Other repo gemini auth",
                        updated_at=_iso_minutes_ago(1),
                    ),
                ],
            )
            broken = MockSearchProvider("broken_cli", raise_on_list=True)

            reg = ProviderRegistry()
            reg.register(codex)
            reg.register(gemini)
            reg.register(broken)

            # Default repo filter + within_min
            recent_same, warnings = list_sessions(
                reg, cwd=str(repo), within_min=30, limit=10
            )
            self.assertEqual([s.session_id for s in recent_same], ["g-same", "c-recent"])
            self.assertTrue(any("broken_cli" in w for w in warnings))

            # all_cwds=True + limit
            all_recent, _ = list_sessions(reg, cwd=str(repo), all_cwds=True, limit=2)
            self.assertEqual([s.session_id for s in all_recent], ["g-other", "g-same"])

            # 3rd provider selector + query (same-repo 1.3x boost ranks g-same ahead of g-other)
            gem_only, _ = list_sessions(
                reg, provider="gemini", cwd=str(repo), all_cwds=True, query="auth"
            )
            self.assertEqual([s.session_id for s in gem_only], ["g-same", "g-other"])

            # Default cwd=None uses os.getcwd() and forwards max_tool_chars on resolve
            with mock.patch("importer.search.os.getcwd", return_value=str(repo)):
                default_list, _ = list_sessions(reg, provider="gemini")
                self.assertEqual([s.session_id for s in default_list], ["g-same"])
                default_res = resolve_session(reg, "latest", provider="gemini", max_tool_chars=250)
                self.assertEqual(default_res.status, "matched")
                self.assertEqual(default_res.detail.summary.session_id, "g-same")
                self.assertEqual(gemini.load_calls[-1], ("g-same", 250))

            # NFKC normalization matches full-width/NFD Unicode queries
            self.assertEqual(tokenize_query("ＯＡｕｔｈ cafe\u0301"), ["oauth", "café"])

            with_arch, _ = list_sessions(
                reg, provider="codex", cwd=str(repo), include_archived=True
            )
            self.assertEqual(
                [s.session_id for s in with_arch], ["c-archived", "c-recent", "c-old"]
            )

            # Stop-word-only query in list_sessions returns no matches
            stop_list, _ = list_sessions(
                reg, cwd=str(repo), query="resume my gemini_cli session"
            )
            self.assertEqual(stop_list, [])

    def test_mixed_cjk_ascii_fields_match_short_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)

            prov = MockSearchProvider(
                "codex",
                sessions=[
                    _make_summary(
                        provider="codex",
                        session_id="sess-1",
                        cwd=str(repo),
                        title="修复API问题",
                        updated_at="2026-11-20T05:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="sess-2",
                        cwd=str(repo),
                        title="修复接口问题",
                        updated_at="2026-11-20T04:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="sess-3",
                        cwd=str(repo),
                        title="Build release pipeline",
                        updated_at="2026-11-20T03:00:00Z",
                    ),
                ],
            )
            reg = ProviderRegistry()
            reg.register(prov)

            # Short non-CJK token ("api", len 3) inside mixed CJK+ASCII title ("修复API问题")
            api_res = resolve_session(reg, "api", cwd=str(repo))
            self.assertEqual(api_res.status, "matched")
            self.assertEqual(api_res.detail.summary.session_id, "sess-1")

            # Mixed query against identical title achieves matched_all_tokens=True despite CJK bigram runner-up
            # Use equal weights across title ("修复API问题") vs title ("修复页面样式" + prompt "修复")
            full_res = resolve_session(reg, "修复API问题", cwd=str(repo))
            self.assertEqual(full_res.status, "matched")
            self.assertEqual(full_res.detail.summary.session_id, "sess-1")

            # 2-char token "ui" must still not false-match inside "build"
            ui_res = resolve_session(reg, "ui", cwd=str(repo))
            self.assertEqual(ui_res.status, "no_match")

    def test_load_session_provider_read_error_degrades_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)

            bad_file = repo / "corrupt.mock"
            bad_file.write_text("corrupt", encoding="utf-8")

            sid_broken = "11111111-2222-4333-8444-555555555555"
            sid_ok = "99999999-aaaa-4bbb-8ccc-dddddddddddd"

            prov = MockSearchProvider(
                "codex",
                suffix=".mock",
                sessions=[
                    _make_summary(
                        provider="codex",
                        session_id=sid_broken,
                        cwd=str(repo),
                        title="Unreadable rollout session",
                        source_path=str(bad_file),
                        updated_at="2026-11-20T06:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id=sid_ok,
                        cwd=str(repo),
                        title="Healthy fallback session",
                        updated_at="2026-11-20T05:00:00Z",
                    ),
                ],
                raise_on_load_refs={sid_broken, str(bad_file)},
            )
            reg = ProviderRegistry()
            reg.register(prov)

            for ref_val in ("latest", sid_broken, "unreadable rollout"):
                res = resolve_session(reg, ref_val, cwd=str(repo))
                self.assertEqual(res.status, "no_match")
                self.assertEqual(res.reason, "provider_read_error")
                self.assertEqual(res.provider, "codex")
                self.assertIsNone(res.detail)
                self.assertTrue(any("codex_read_error:" in w for w in res.warnings))
                self.assertTrue(len(res.candidates) >= 1)

            explicit_res = resolve_session(reg, str(bad_file), cwd=str(repo))
            self.assertEqual(explicit_res.status, "no_match")
            self.assertEqual(explicit_res.reason, "provider_read_error")
            self.assertEqual(explicit_res.provider, "codex")
            self.assertIsNone(explicit_res.detail)
            self.assertEqual(explicit_res.candidates, [])
            self.assertTrue(any("codex_read_error:" in w for w in explicit_res.warnings))

    def test_stage_b_body_search_avoids_inner_substring_false_positives(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".git").mkdir(parents=True)

            prov = MockSearchProvider(
                "codex",
                sessions=[
                    _make_summary(
                        provider="codex",
                        session_id="sess-one",
                        cwd=str(repo),
                        title="Refactor module layout",
                        first_user_prompt="Clean up dependencies",
                        updated_at="2026-11-20T06:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="sess-two",
                        cwd=str(repo),
                        title="Fix server binding",
                        first_user_prompt="Update config",
                        updated_at="2026-11-20T05:00:00Z",
                    ),
                    _make_summary(
                        provider="codex",
                        session_id="sess-three",
                        cwd=str(repo),
                        title="Database notes",
                        first_user_prompt="Check plan",
                        updated_at="2026-11-20T04:00:00Z",
                    ),
                ],
                bodies={
                    "sess-one": "We use important transport and import statements everywhere.",
                    "sess-two": "Bind service to port 8080 and expose portal route.",
                    "sess-three": "讨论数据库迁移方案与回滚步骤",
                },
            )
            reg = ProviderRegistry()
            reg.register(prov)

            # "port" must match only "sess-two" via word/prefix, not "sess-one" via "import"/"transport"
            port_listed, _ = list_sessions(reg, cwd=str(repo), query="port")
            self.assertEqual([s.session_id for s in port_listed], ["sess-two"])

            port_res = resolve_session(reg, "port route", cwd=str(repo))
            self.assertEqual(port_res.status, "matched")
            self.assertEqual(port_res.reason, "body_match")
            self.assertEqual(port_res.detail.summary.session_id, "sess-two")

            # Verify "sess-one" alone does not match "port" in Stage B
            import_only_reg = ProviderRegistry()
            import_only_reg.register(
                MockSearchProvider(
                    "codex",
                    sessions=[prov._sessions[0]],
                    bodies={"sess-one": prov._bodies["sess-one"]},
                )
            )
            no_port = resolve_session(import_only_reg, "port", cwd=str(repo))
            self.assertEqual(no_port.status, "no_match")

            # CJK substring matching in Stage B must remain intact
            cjk_listed, _ = list_sessions(reg, cwd=str(repo), query="迁移")
            self.assertEqual([s.session_id for s in cjk_listed], ["sess-three"])


if __name__ == "__main__":
    unittest.main()
