"""Unit tests for SessionProvider, path/SQLite utilities, and ProviderRegistry."""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "skills" / "resume-session" / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.models import NormalizedTurn, SessionDetail, SessionSummary
from importer.providers import (
    ProviderReadError,
    ProviderRegistry,
    SessionProvider,
    create_default_registry,
)
from importer.providers.base import (
    derive_repo_name,
    find_git_root,
    highest_numbered_file,
    match_repo,
    normalize_path,
    open_sqlite_readonly,
)


def _dummy_summary(provider: str = "cursor", session_id: str = "s1") -> SessionSummary:
    return SessionSummary(
        provider=provider,
        session_id=session_id,
        source_kind="cursor_log",
        source_path="/tmp/x.cursor",
        cwd="/tmp/repo",
        repo_name="repo",
        git_branch="main",
        title="Mock session",
        first_user_prompt="hello",
        last_user_prompt="world",
        created_at="2026-11-20T00:00:00Z",
        updated_at="2026-11-20T00:01:00Z",
        turn_count=1,
    )


class CursorMockProvider(SessionProvider):
    """Minimal mock provider demonstrating third-party extensibility."""

    name = "cursor"
    display_name = "Cursor"
    aliases = ("cursor-cli",)

    def __init__(self, *, available: bool = True) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def list_sessions(self, *, include_archived: bool = False) -> tuple[list[SessionSummary], list[str]]:
        return ([_dummy_summary(self.name)], [])

    def can_handle_path(self, path: Path) -> bool:
        return path.suffix == ".cursor"

    def load_session(self, ref: str, *, max_tool_chars: int = 600) -> SessionDetail:
        return SessionDetail(
            summary=_dummy_summary(self.name, ref),
            turns=[
                NormalizedTurn(
                    turn_index=1,
                    timestamp="2026-11-20T00:00:00Z",
                    user_text="hello",
                    assistant_text="hi",
                )
            ],
        )


class CollidingAliasProvider(CursorMockProvider):
    name = "other"
    display_name = "Other"
    aliases = ("cursor-cli",)


class DuplicateNameProvider(CursorMockProvider):
    name = "cursor"
    display_name = "Cursor Dup"
    aliases = ()


class GeminiMockProvider(CursorMockProvider):
    name = "gemini"
    display_name = "Gemini CLI"
    aliases = ("gemini-cli",)

    def can_handle_path(self, path: Path) -> bool:
        return path.suffix == ".gemini"


class TestProviderRegistryAndUtilities(unittest.TestCase):
    """Tests for Task 2 provider abstraction, registry, and filesystem/SQLite helpers."""

    def test_registry_extensibility_with_custom_future_provider(self):
        self.assertIsInstance(create_default_registry(), ProviderRegistry)

        registry = ProviderRegistry()
        cursor = CursorMockProvider(available=True)
        registry.register(cursor)

        self.assertIs(registry.get("cursor"), cursor)
        self.assertIs(registry.get("cursor-cli"), cursor)
        self.assertIs(registry.get("CURSOR-CLI"), cursor)
        self.assertEqual(registry.resolve("all"), [cursor])
        self.assertEqual(registry.resolve("cursor-cli"), [cursor])
        self.assertEqual(registry.resolve("cursor,cursor-cli"), [cursor])
        self.assertIs(registry.detect_for_path(Path("/tmp/x.cursor")), cursor)
        self.assertIsNone(registry.detect_for_path(Path("/tmp/x.txt")))
        self.assertEqual(registry.names(), ("cursor",))
        self.assertEqual(registry.all_tokens(), {"cursor", "cursor-cli"})
        self.assertEqual(
            registry.describe(),
            [
                {
                    "name": "cursor",
                    "display_name": "Cursor",
                    "aliases": ["cursor-cli"],
                    "available": True,
                }
            ],
        )
        self.assertEqual(cursor.extract_searchable_text(_dummy_summary()), "")

        with self.assertRaises(ValueError):
            registry.register(DuplicateNameProvider())
        with self.assertRaises(ValueError):
            registry.register(CollidingAliasProvider())
        with self.assertRaises(KeyError) as ctx:
            registry.get("nonexistent")
        self.assertIn("cursor", str(ctx.exception))
        with self.assertRaises(ValueError) as rctx:
            registry.resolve("nonexistent")
        self.assertIn("cursor", str(rctx.exception))

        gemini = GeminiMockProvider(available=False)
        registry.register(gemini)
        self.assertEqual(registry.resolve("all"), [cursor, gemini])
        self.assertEqual(registry.resolve("gemini-cli,cursor"), [gemini, cursor])
        self.assertIs(registry.detect_for_path(Path("/tmp/y.gemini")), gemini)
        self.assertEqual(registry.names(), ("cursor", "gemini"))

    def test_match_repo_worktree_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            (repo / ".git" / "worktrees" / "wt").mkdir(parents=True)
            (repo / "src").mkdir()

            repo_link = base / "repo_link"
            os.symlink(repo, repo_link)

            worktree = base / "wt"
            (worktree / "nested").mkdir(parents=True)
            (repo / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n", encoding="utf-8")
            (worktree / ".git").write_text(
                f"gitdir: {repo.resolve() / '.git' / 'worktrees' / 'wt'}\n",
                encoding="utf-8",
            )

            unrelated = base / "unrelated"
            (unrelated / ".git").mkdir(parents=True)
            plain_a = base / "plain_a"
            (plain_a / "sub").mkdir(parents=True)

            self.assertEqual(normalize_path(""), "")
            self.assertEqual(find_git_root(repo / "src"), str(repo.resolve()))
            self.assertEqual(find_git_root(worktree / "nested"), str(worktree.resolve()))
            self.assertEqual(find_git_root(plain_a / "sub"), "")

            self.assertEqual(derive_repo_name(str(repo / "src")), "repo")
            self.assertEqual(derive_repo_name(str(worktree)), "repo")
            self.assertEqual(derive_repo_name(str(plain_a / "sub")), "sub")
            self.assertEqual(derive_repo_name(""), "")

            self.assertEqual(match_repo(str(repo), str(repo)), "exact")
            self.assertEqual(match_repo(str(repo_link), str(repo)), "exact")
            self.assertEqual(match_repo(str(repo_link / "src"), str(repo)), "same_repo")
            self.assertEqual(match_repo(str(repo / "src"), str(repo)), "same_repo")
            self.assertEqual(match_repo(str(worktree), str(repo)), "same_repo")
            self.assertEqual(match_repo(str(worktree / "nested"), str(repo / "src")), "same_repo")
            self.assertEqual(match_repo(str(unrelated), str(repo)), "different")
            self.assertEqual(match_repo(str(plain_a), str(plain_a / "sub")), "different")
            self.assertEqual(match_repo("", str(repo)), "different")

    def test_highest_numbered_file_sorts_numerically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("state_5.sqlite", "state_9.sqlite", "state_10.sqlite", "state_bad.sqlite"):
                (root / name).write_text("x", encoding="utf-8")
            (root / "state_99.sqlite").mkdir()

            picked = highest_numbered_file(root, "state_", ".sqlite")
            self.assertIsNotNone(picked)
            self.assertEqual(picked.name, "state_10.sqlite")
            self.assertIsNone(highest_numbered_file(root / "missing", "state_", ".sqlite"))

    def test_open_sqlite_readonly_reads_wal_and_blocks_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite"
            writer = sqlite3.connect(str(db_path))
            try:
                writer.execute("PRAGMA journal_mode = WAL")
                writer.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
                writer.execute("INSERT INTO items (id, name) VALUES (1, 'alpha')")
                writer.commit()

                reader = open_sqlite_readonly(db_path)
                try:
                    rows = reader.execute("SELECT id, name FROM items").fetchall()
                    self.assertEqual(rows, [(1, "alpha")])
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.execute("INSERT INTO items (id, name) VALUES (2, 'beta')")
                finally:
                    reader.close()
            finally:
                writer.close()

            with self.assertRaises(ProviderReadError):
                open_sqlite_readonly(Path(tmp) / "does_not_exist.sqlite")


if __name__ == "__main__":
    unittest.main()
