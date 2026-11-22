"""Base provider interface and shared path, repository, and SQLite helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
from pathlib import Path
import sqlite3

from importer.models import SessionDetail, SessionSummary


class ProviderReadError(RuntimeError):
    """Raised when a session provider cannot read or parse its storage."""


class SessionProvider(ABC):
    """Abstract base class for pluggable agent session providers."""

    name: str = ""
    display_name: str = ""
    aliases: tuple[str, ...] = ()

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider's local storage exists and is readable."""

    @abstractmethod
    def list_sessions(
        self, *, include_archived: bool = False
    ) -> tuple[list[SessionSummary], list[str]]:
        """Discover sessions and return (summaries, warnings)."""

    @abstractmethod
    def can_handle_path(self, path: Path) -> bool:
        """Return True if this provider recognizes the given transcript path."""

    @abstractmethod
    def load_session(self, ref: str, *, max_tool_chars: int = 600) -> SessionDetail:
        """Load and normalize a full session by ID or path."""

    def extract_searchable_text(
        self, summary: SessionSummary, *, max_bytes: int = 262_144
    ) -> str:
        """Return bounded searchable text for a session (optional override)."""
        _ = (summary, max_bytes)
        return ""


def normalize_path(path: str | Path) -> str:
    """Return the canonical resolved path string, or '' for empty inputs."""
    if not path:
        return ""
    if isinstance(path, str) and not path.strip():
        return ""
    return os.path.realpath(os.fspath(path))


def find_git_root(path: str | Path) -> str:
    """Walk parent directories for a .git directory or file (linked worktree)."""
    norm = normalize_path(path)
    if not norm:
        return ""
    current = Path(norm)
    for candidate in (current, *current.parents):
        dot_git = candidate / ".git"
        try:
            if dot_git.is_dir() or dot_git.is_file():
                return str(candidate)
        except OSError:
            continue
    return ""


def _resolve_common_git_root(path: str | Path) -> str:
    """Resolve a git checkout or linked worktree to the main repository root."""
    git_root = find_git_root(path)
    if not git_root:
        return ""
    root_path = Path(git_root)
    dot_git = root_path / ".git"
    if not dot_git.is_file():
        return git_root

    try:
        content = dot_git.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return git_root

    gitdir_raw = ""
    for line in content.splitlines():
        if line.lower().startswith("gitdir:"):
            gitdir_raw = line.split(":", 1)[1].strip()
            break
    if not gitdir_raw:
        return git_root

    gitdir_path = Path(gitdir_raw)
    if not gitdir_path.is_absolute():
        gitdir_path = root_path / gitdir_path
    gitdir_real = Path(os.path.realpath(gitdir_path))

    commondir_file = gitdir_real / "commondir"
    if commondir_file.is_file():
        try:
            common_raw = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            common_raw = ""
        if common_raw:
            common_path = Path(common_raw)
            if not common_path.is_absolute():
                common_path = gitdir_real / common_path
            common_real = Path(os.path.realpath(common_path))
            if common_real.name == ".git" and common_real.parent != common_real:
                return str(common_real.parent)
            return str(common_real)

    # Support synthetic linked worktree .git files without a commondir file.
    if gitdir_real.parent.name == "worktrees" and gitdir_real.parent.parent.name == ".git":
        return str(gitdir_real.parent.parent.parent)
    return git_root


def derive_repo_name(cwd: str) -> str:
    """Derive a concise repository or working directory name from cwd."""
    norm = normalize_path(cwd)
    if not norm:
        return ""
    common_root = _resolve_common_git_root(norm)
    target = Path(common_root or norm)
    return target.name or str(target)


def match_repo(session_cwd: str, target_cwd: str) -> str:
    """Return 'exact', 'same_repo', or 'different' for two working directories."""
    norm_session = normalize_path(session_cwd)
    norm_target = normalize_path(target_cwd)
    if not norm_session or not norm_target:
        return "different"
    if norm_session == norm_target:
        return "exact"
    root_session = _resolve_common_git_root(norm_session)
    root_target = _resolve_common_git_root(norm_target)
    if root_session and root_session == root_target:
        return "same_repo"
    return "different"


def open_sqlite_readonly(path: Path, *, timeout: float = 2.0) -> sqlite3.Connection:
    """Open a SQLite database in strict read-only mode preserving WAL visibility."""
    try:
        resolved = Path(path).resolve()
        is_file = resolved.is_file()
        uri = f"{resolved.as_uri()}?mode=ro"
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProviderReadError(f"Unable to resolve SQLite database '{path}': {exc}") from exc
    if not is_file:
        raise ProviderReadError(f"SQLite database not found: {path}")
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ProviderReadError(f"Unable to open SQLite database '{path}': {exc}") from exc
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        conn.close()
        raise ProviderReadError(f"Unable to enforce read-only pragma for '{path}': {exc}") from exc
    return conn


def highest_numbered_file(directory: Path, prefix: str, suffix: str) -> Path | None:
    """Select the regular file in directory with the largest integer between prefix and suffix."""
    if not directory.is_dir():
        return None
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None

    min_len = len(prefix) + len(suffix)
    best_key: tuple[int, str] | None = None
    best_path: Path | None = None
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        name = entry.name
        if len(name) <= min_len or not name.startswith(prefix) or not name.endswith(suffix):
            continue
        end_idx = len(name) - len(suffix)
        middle = name[len(prefix):end_idx]
        if not middle.isascii() or not middle.isdigit():
            continue
        key = (int(middle), name)
        if best_key is None or key > best_key:
            best_key = key
            best_path = entry
    return best_path
