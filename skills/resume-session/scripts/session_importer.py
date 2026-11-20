#!/usr/bin/env python3
"""CLI entrypoint for unified cross-provider session discovery and handoff rendering."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, NoReturn

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from importer.providers.registry import ProviderRegistry, create_default_registry
from importer.render import (
    format_list_text,
    format_show_text,
    render_show_envelope,
    render_turns_envelope,
)
from importer.safety import TRUST_BOUNDARY
from importer.search import list_sessions, resolve_session


class _CliValidationError(ValueError):
    """Raised on invalid CLI arguments or user-facing validation failures (exit code 2)."""


class _CliArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises _CliValidationError instead of exiting directly."""

    def error(self, message: str) -> NoReturn:
        raise _CliValidationError(message)


def _provider_choices_hint(registry: ProviderRegistry) -> str:
    names = registry.names()
    if not names:
        return "all"
    return "all, " + ", ".join(names)


def _validate_non_negative(flag: str, value: int | None) -> None:
    if value is not None and value < 0:
        raise _CliValidationError(f"{flag} must be >= 0, got {value}")


def _validate_positive(flag: str, value: int | None) -> None:
    if value is not None and value <= 0:
        raise _CliValidationError(f"{flag} must be > 0, got {value}")


def _parse_range(raw: str) -> tuple[int, int]:
    parts = raw.strip().split(":")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise _CliValidationError(f"Invalid --range {raw!r}; expected START:END (1-based inclusive)")
    try:
        start = int(parts[0].strip())
        end = int(parts[1].strip())
    except ValueError as exc:
        raise _CliValidationError(
            f"Invalid --range {raw!r}; START and END must be integers"
        ) from exc
    if start < 1:
        raise _CliValidationError(f"Invalid --range {raw!r}; START must be >= 1")
    if end < start:
        raise _CliValidationError(f"Invalid --range {raw!r}; END ({end}) must be >= START ({start})")
    return start, end


def build_parser(registry: ProviderRegistry) -> argparse.ArgumentParser:
    """Build the CLI argument parser using providers registered in `registry`."""
    provider_hint = _provider_choices_hint(registry)
    parser = _CliArgumentParser(
        prog="session_importer.py",
        description="Read-only cross-provider session discovery and handoff renderer.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_providers = subparsers.add_parser("providers", help="List registered session providers.")
    p_providers.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_list = subparsers.add_parser("list", help="List recent sessions.")
    p_list.add_argument(
        "--provider",
        default="all",
        help=f"Provider selector ({provider_hint}). Default: all.",
    )
    p_list.add_argument("--cwd", default=None, help="Target working directory (default: current directory).")
    p_list.add_argument("--all-cwds", action="store_true", help="Disable repo filtering and list across all CWDs.")
    p_list.add_argument("--query", default="", help="Optional keyword filter.")
    p_list.add_argument("--within-min", type=int, default=0, help="Only include sessions updated within N minutes.")
    p_list.add_argument("--limit", type=int, default=20, help="Maximum sessions to return (default: 20).")
    p_list.add_argument("--include-archived", action="store_true", help="Include archived sessions if supported.")
    p_list.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_show = subparsers.add_parser("show", help="Resolve and render a session handoff.")
    p_show.add_argument("ref", nargs="?", default=None, help="Session ID/prefix, file path, 'latest', or query.")
    p_show.add_argument(
        "--provider",
        default="all",
        help=f"Provider selector ({provider_hint}). Default: all.",
    )
    p_show.add_argument("--cwd", default=None, help="Target working directory (default: current directory).")
    p_show.add_argument(
        "--allow-cross-repo",
        action="store_true",
        help="Allow direct loading of a matched session from another repository.",
    )
    p_show.add_argument("--mode", default="full", help="Render mode: full or compact (default: full).")
    p_show.add_argument("--max-chars", type=int, default=None, help="Maximum rendered character budget.")
    p_show.add_argument("--max-tool-chars", type=int, default=600, help="Maximum characters per tool argument/output.")
    p_show.add_argument("--tail-turns", type=int, default=None, help="Pin at most N trailing turns in full mode.")
    p_show.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_turns = subparsers.add_parser("turns", help="Inspect a 1-based inclusive slice of turns from a session.")
    p_turns.add_argument("ref", help="Session ID/prefix, file path, or query resolving to a single session.")
    p_turns.add_argument("--range", required=True, dest="turn_range", help="1-based inclusive turn slice START:END.")
    p_turns.add_argument(
        "--provider",
        default="all",
        help=f"Provider selector ({provider_hint}). Default: all.",
    )
    p_turns.add_argument("--max-tool-chars", type=int, default=600, help="Maximum characters per tool argument/output.")
    p_turns.add_argument("--json", action="store_true", help="Emit JSON output.")

    return parser


def _format_providers_text(providers: list[dict[str, Any]]) -> str:
    if not providers:
        return "No providers registered."
    lines: list[str] = []
    for entry in providers:
        status = "available" if entry.get("available") else "unavailable"
        aliases = entry.get("aliases") or []
        alias_str = f" (aliases: {', '.join(aliases)})" if aliases else ""
        lines.append(f"- {entry['name']} [{entry.get('display_name', entry['name'])}]: {status}{alias_str}")
    return "\n".join(lines)


def _format_turns_text(envelope: dict[str, Any]) -> str:
    lines = [
        f"=== Turns {envelope['start']}..{envelope['end']} of {envelope['total_turns']} "
        f"(trust_boundary={envelope['trust_boundary']}) ===",
        f"Provider: {envelope.get('provider') or '-'} | Session ID: {envelope.get('session_id') or '-'}",
    ]
    for turn in envelope.get("turns") or []:
        header = f"\n### Turn {turn['turn_index']}"
        if turn.get("timestamp"):
            header += f" ({turn['timestamp']})"
        if turn.get("compaction_before"):
            header += " [compaction before]"
        lines.append(header)
        if turn.get("user_text"):
            lines.append(f"User:\n{turn['user_text']}")
        if turn.get("assistant_text"):
            lines.append(f"Assistant:\n{turn['assistant_text']}")
        tool_events = turn.get("tool_events") or []
        if tool_events:
            lines.append("Tools:")
            for ev in tool_events:
                parts = [f"- {ev['tool_name']} [{ev.get('status', 'unknown')}]"]
                if ev.get("arguments_summary"):
                    parts.append(f"args: {ev['arguments_summary']}")
                if ev.get("output_summary"):
                    parts.append(f"out: {ev['output_summary']}")
                lines.append(" | ".join(parts))
        if turn.get("subagent_notes"):
            lines.append("Subagent Notes: " + "; ".join(turn["subagent_notes"]))
        if turn.get("warnings"):
            lines.append("Turn Warnings: " + ", ".join(turn["warnings"]))

    warnings = envelope.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Warnings: " + ", ".join(str(w) for w in warnings))
    return "\n".join(lines)


def _emit_output(payload: dict[str, Any], text: str, *, use_json: bool) -> None:
    if use_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(text)


def _emit_error(message: str, *, use_json: bool) -> None:
    if use_json:
        print(json.dumps({"error": message}, ensure_ascii=False))
    else:
        print(f"error: {message}", file=sys.stderr)


def _resolve_providers(registry: ProviderRegistry, selector: str) -> None:
    try:
        registry.resolve(selector)
    except ValueError as exc:
        raise _CliValidationError(str(exc)) from exc


def _handle_providers(registry: ProviderRegistry, args: argparse.Namespace) -> None:
    providers = registry.describe()
    payload = {"schema_version": 1, "providers": providers}
    _emit_output(payload, _format_providers_text(providers), use_json=args.json)


def _handle_list(registry: ProviderRegistry, args: argparse.Namespace) -> None:
    _validate_non_negative("--within-min", args.within_min)
    _validate_non_negative("--limit", args.limit)
    _resolve_providers(registry, args.provider)

    sessions, warnings = list_sessions(
        registry,
        provider=args.provider,
        cwd=args.cwd,
        all_cwds=args.all_cwds,
        query=args.query,
        within_min=args.within_min,
        limit=args.limit,
        include_archived=args.include_archived,
    )
    payload = {
        "schema_version": 1,
        "trust_boundary": TRUST_BOUNDARY,
        "sessions": [s.to_dict() for s in sessions],
        "warnings": warnings,
    }
    _emit_output(payload, format_list_text(sessions, warnings), use_json=args.json)


def _handle_show(registry: ProviderRegistry, args: argparse.Namespace) -> None:
    if args.mode not in ("full", "compact"):
        raise _CliValidationError(f"Unsupported render mode: {args.mode!r} (expected 'full' or 'compact')")
    _validate_positive("--max-chars", args.max_chars)
    _validate_non_negative("--max-tool-chars", args.max_tool_chars)
    _validate_non_negative("--tail-turns", args.tail_turns)
    _resolve_providers(registry, args.provider)

    result = resolve_session(
        registry,
        args.ref,
        provider=args.provider,
        cwd=args.cwd,
        allow_cross_repo=args.allow_cross_repo,
        max_tool_chars=args.max_tool_chars,
    )
    envelope = render_show_envelope(
        result,
        mode=args.mode,
        max_chars=args.max_chars,
        tail_turns=args.tail_turns,
    )
    _emit_output(envelope, format_show_text(envelope), use_json=args.json)


def _handle_turns(registry: ProviderRegistry, args: argparse.Namespace) -> None:
    _validate_non_negative("--max-tool-chars", args.max_tool_chars)
    _resolve_providers(registry, args.provider)
    start, end = _parse_range(args.turn_range)

    result = resolve_session(
        registry,
        args.ref,
        provider=args.provider,
        cwd=os.getcwd(),
        allow_cross_repo=True,
        max_tool_chars=args.max_tool_chars,
    )
    if result.status != "matched" or result.detail is None:
        raise _CliValidationError(
            f"Could not resolve a single session for {args.ref!r} "
            f"(status={result.status}, reason={result.reason})"
        )

    try:
        envelope = render_turns_envelope(result.detail, start, end)
    except ValueError as exc:
        raise _CliValidationError(str(exc)) from exc
    _emit_output(envelope, _format_turns_text(envelope), use_json=args.json)


def main(argv: list[str] | None = None, *, registry: ProviderRegistry | None = None) -> int:
    """Execute the session_importer CLI and return a process exit code."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    use_json = "--json" in raw_argv

    try:
        active_registry = create_default_registry() if registry is None else registry
        parser = build_parser(active_registry)
        args = parser.parse_args(raw_argv)
        use_json = bool(getattr(args, "json", False)) or use_json

        if args.command == "providers":
            _handle_providers(active_registry, args)
        elif args.command == "list":
            _handle_list(active_registry, args)
        elif args.command == "show":
            _handle_show(active_registry, args)
        elif args.command == "turns":
            _handle_turns(active_registry, args)
        else:
            raise _CliValidationError(f"Unknown command: {args.command!r}")
        return 0
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0 if exc.code is None else 1
    except _CliValidationError as exc:
        _emit_error(str(exc), use_json=use_json)
        return 2
    except Exception as exc:
        _emit_error(str(exc) or exc.__class__.__name__, use_json=use_json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
