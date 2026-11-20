"""Dynamic provider registry and default provider wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from importer.providers.base import SessionProvider


class ProviderRegistry:
    """Registry for dynamically discovering and resolving SessionProvider instances."""

    def __init__(self) -> None:
        self._providers: dict[str, SessionProvider] = {}
        self._by_token: dict[str, SessionProvider] = {}

    def register(self, provider: SessionProvider) -> None:
        """Register a provider and its aliases, rejecting empty or colliding tokens."""
        canonical = provider.name.strip().lower()
        if not canonical or canonical == "all":
            raise ValueError(f"Invalid provider name: {provider.name!r}")

        tokens = [canonical]
        for alias in provider.aliases:
            normalized_alias = alias.strip().lower()
            if not normalized_alias:
                continue
            if normalized_alias == "all":
                raise ValueError(f"Reserved alias 'all' cannot be used by {provider.name!r}")
            tokens.append(normalized_alias)

        seen_local: set[str] = set()
        for token in tokens:
            if token in self._by_token or token in seen_local:
                raise ValueError(f"Duplicate or colliding provider token: {token!r}")
            seen_local.add(token)

        self._providers[canonical] = provider
        for token in tokens:
            self._by_token[token] = provider

    def get(self, name_or_alias: str) -> SessionProvider:
        """Return the provider matching a canonical name or alias."""
        token = name_or_alias.strip().lower()
        provider = self._by_token.get(token)
        if provider is None:
            registered = ", ".join(self.names()) or "(none)"
            raise KeyError(f"Unknown provider {name_or_alias!r}; registered providers: {registered}")
        return provider

    def resolve(self, selector: str = "all") -> list[SessionProvider]:
        """Resolve 'all', a single token, or comma-separated tokens to providers."""
        raw = selector.strip() if selector is not None else ""
        if not raw or raw.lower() == "all":
            return list(self._providers.values())

        parts = [part.strip() for part in raw.split(",") if part.strip()]
        if not parts:
            registered = ", ".join(self.names()) or "(none)"
            raise ValueError(f"Empty provider selector {selector!r}; registered providers: {registered}")

        if any(part.lower() == "all" for part in parts):
            return list(self._providers.values())

        resolved: list[SessionProvider] = []
        seen_ids: set[int] = set()
        for part in parts:
            try:
                provider = self.get(part)
            except KeyError as exc:
                raise ValueError(str(exc)) from exc
            if id(provider) not in seen_ids:
                seen_ids.add(id(provider))
                resolved.append(provider)
        return resolved

    def detect_for_path(self, path: Path) -> SessionProvider | None:
        """Return the first registered provider that can handle the given file path."""
        target = Path(path)
        for provider in self._providers.values():
            if provider.can_handle_path(target):
                return provider
        return None

    def names(self) -> tuple[str, ...]:
        """Return canonical provider names in registration order."""
        return tuple(provider.name for provider in self._providers.values())

    def all_tokens(self) -> set[str]:
        """Return all lowercase canonical names and aliases."""
        return set(self._by_token.keys())

    def describe(self) -> list[dict[str, Any]]:
        """Return JSON-serializable metadata for all registered providers."""
        return [
            {
                "name": provider.name,
                "display_name": provider.display_name,
                "aliases": list(provider.aliases),
                "available": bool(provider.is_available()),
            }
            for provider in self._providers.values()
        ]


def create_default_registry(roots: dict[str, Path] | None = None) -> ProviderRegistry:
    """Create the default ProviderRegistry with built-in providers registered."""
    from importer.providers.claude import ClaudeProvider
    from importer.providers.codex import CodexProvider

    registry = ProviderRegistry()
    registry.register(CodexProvider(root=roots.get("codex") if roots else None))
    registry.register(ClaudeProvider(root=roots.get("claude") if roots else None))
    return registry
