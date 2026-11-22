"""Pluggable session provider interfaces, helpers, and registry."""

from importer.providers.base import (
    ProviderReadError,
    SessionProvider,
    derive_repo_name,
    find_git_root,
    highest_numbered_file,
    match_repo,
    normalize_path,
    open_sqlite_readonly,
)
from importer.providers.registry import ProviderRegistry, create_default_registry

__all__ = [
    "ProviderReadError",
    "ProviderRegistry",
    "SessionProvider",
    "create_default_registry",
    "derive_repo_name",
    "find_git_root",
    "highest_numbered_file",
    "match_repo",
    "normalize_path",
    "open_sqlite_readonly",
]
