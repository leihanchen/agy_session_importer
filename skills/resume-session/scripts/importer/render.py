"""Two-mode session rendering, context bounding, turn drill-down, and text formatting."""

from __future__ import annotations

from typing import Any

from importer.models import NormalizedTurn, SessionDetail, SessionSummary
from importer.safety import TRUST_BOUNDARY, truncate_text
from importer.search import ResolutionResult

DEFAULT_FULL_MAX_CHARS = 60_000
DEFAULT_COMPACT_MAX_CHARS = 12_000

_COMPACT_TAIL_TURNS = 3
_COMPACT_OUTLINE_USER_CHARS = 140
_COMPACT_OUTLINE_ASSISTANT_CHARS = 140
_COMPACT_PROMPT_CHARS = 600
_COMPACT_TAIL_FIELD_CHARS = 1_500
_COMPACT_LIST_ITEMS = 30
_COMPACT_ITEM_CHARS = 160


def _merge_warnings(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for warning in group:
            if warning and warning not in seen:
                seen.add(warning)
                merged.append(warning)
    return merged


def _hard_bound(text: str, budget: int) -> tuple[str, bool]:
    """Strictly bound text to at most `budget` characters."""
    if budget <= 0:
        return "", bool(text)
    if len(text) <= budget:
        return text, False
    if budget <= 3:
        return text[:budget], True
    bounded, _ = truncate_text(text, budget - 3)
    return bounded, True


def _render_header(detail: SessionDetail, warnings: list[str], *, mode: str) -> str:
    s = detail.summary
    turn_count = len(detail.turns) if detail.turns else s.turn_count
    lines = [
        f"=== Session Handoff ({mode} mode | trust_boundary={TRUST_BOUNDARY}) ===",
        f"Provider: {s.provider or '-'} | Session ID: {s.session_id or '-'} | Source: {s.source_kind or '-'}",
        f"Repo: {s.repo_name or '-'} | CWD: {s.cwd or '-'} | Branch: {s.git_branch or '-'}",
        f"Created: {s.created_at or '-'} | Updated: {s.updated_at or '-'} | Turns: {turn_count}",
    ]
    if s.title:
        lines.append(f"Title: {s.title}")
    if detail.compaction_count > 0:
        lines.append(f"Compactions: {detail.compaction_count}")
    lines.append(f"Warnings: {', '.join(warnings) if warnings else 'none'}")
    return "\n".join(lines)


def _format_tool_event(ev: Any) -> str:
    parts = [f"- {ev.tool_name} [{ev.status}]"]
    if ev.arguments_summary:
        parts.append(f"args: {ev.arguments_summary}")
    if ev.output_summary:
        parts.append(f"out: {ev.output_summary}")
    return " | ".join(parts)


def _render_turn_full(turn: NormalizedTurn, *, field_limit: int | None = None) -> str:
    header_parts = [f"### Turn {turn.turn_index}"]
    if turn.timestamp:
        header_parts.append(f"({turn.timestamp})")
    if turn.compaction_before:
        header_parts.append("[compaction before]")

    lines = [" ".join(header_parts)]
    if turn.user_text:
        user_text = turn.user_text
        if field_limit is not None:
            user_text, _ = truncate_text(user_text, field_limit)
        lines.append(f"User:\n{user_text}")
    if turn.assistant_text:
        assistant_text = turn.assistant_text
        if field_limit is not None:
            assistant_text, _ = truncate_text(assistant_text, field_limit)
        lines.append(f"Assistant:\n{assistant_text}")
    if turn.tool_events:
        lines.append("Tools:")
        for ev in turn.tool_events:
            lines.append(_format_tool_event(ev))
    if turn.subagent_notes:
        lines.append("Subagent Notes: " + "; ".join(turn.subagent_notes))
    if turn.warnings:
        lines.append("Turn Warnings: " + ", ".join(turn.warnings))
    return "\n".join(lines)


def _render_turn_outline(turn: NormalizedTurn) -> str:
    user_snip, _ = truncate_text(turn.user_text, _COMPACT_OUTLINE_USER_CHARS, single_line=True)
    asst_snip, _ = truncate_text(
        turn.assistant_text, _COMPACT_OUTLINE_ASSISTANT_CHARS, single_line=True
    )
    meta = f"Turn {turn.turn_index}"
    if turn.tool_events:
        tool_names = ", ".join(ev.tool_name for ev in turn.tool_events[:4])
        if len(turn.tool_events) > 4:
            tool_names += f", +{len(turn.tool_events) - 4}"
        meta += f" [tools: {tool_names}]"
    parts = [f"- {meta}:"]
    if user_snip:
        parts.append(f"U: {user_snip}")
    if asst_snip:
        parts.append(f"A: {asst_snip}")
    return " ".join(parts)


def _drilldown_marker(session_id: str, omitted_end: int) -> str:
    ref = session_id or "latest"
    return (
        f"[Turns 2..{omitted_end} omitted due to max_chars budget; "
        f"call turns {ref} --range 2:{omitted_end} to inspect]"
    )


def _render_full(
    detail: SessionDetail,
    base_warnings: list[str],
    *,
    budget: int,
    tail_turns: int | None,
) -> tuple[str, list[str]]:
    if budget <= 0:
        has_content = bool(detail.turns or detail.summary.title)
        warnings = _merge_warnings(base_warnings, ["bounded_history"] if has_content else [])
        return "", warnings

    turn_blocks = [_render_turn_full(turn) for turn in detail.turns]
    n = len(turn_blocks)

    if n == 0:
        header = _render_header(detail, base_warnings, mode="full")
        body = f"{header}\n\n[No transcript turns recorded]"
        bounded, truncated = _hard_bound(body, budget)
        warnings = _merge_warnings(base_warnings, ["bounded_history"] if truncated else [])
        return bounded, warnings

    header_unbounded = _render_header(detail, base_warnings, mode="full")
    full_candidate = f"{header_unbounded}\n\n" + "\n\n".join(turn_blocks)
    tail_limited = tail_turns is not None and tail_turns < (n - 1)
    if len(full_candidate) <= budget and not tail_limited:
        return full_candidate, list(base_warnings)

    warnings = _merge_warnings(base_warnings, ["bounded_history"])
    header = _render_header(detail, warnings, mode="full")

    if n == 1:
        bounded, _ = _hard_bound(f"{header}\n\n{turn_blocks[0]}", budget)
        return bounded, warnings

    min_tail_start_idx = 1 if tail_turns is None else max(1, n - tail_turns)
    effective_min_start = max(2, min_tail_start_idx)

    prefix = f"{header}\n\n{turn_blocks[0]}"
    best_k_idx = n
    running_tail_len = 0

    for k_idx in range(n - 1, effective_min_start - 1, -1):
        running_tail_len += len(turn_blocks[k_idx]) + 2
        marker = _drilldown_marker(detail.summary.session_id, k_idx)
        candidate_len = len(prefix) + 2 + len(marker) + running_tail_len
        if candidate_len <= budget:
            best_k_idx = k_idx
        elif running_tail_len + len(header) + len(marker) > budget:
            break

    if best_k_idx < n:
        marker = _drilldown_marker(detail.summary.session_id, best_k_idx)
        assembled = "\n\n".join([prefix, marker, *turn_blocks[best_k_idx:]])
        if len(assembled) <= budget:
            return assembled, warnings

    marker = _drilldown_marker(detail.summary.session_id, n)
    suffix = f"\n\n{marker}"
    if len(prefix) + len(suffix) <= budget:
        return prefix + suffix, warnings

    avail_for_prefix = budget - len(suffix)
    if avail_for_prefix > len(header) + 10:
        bounded_prefix, _ = _hard_bound(prefix, avail_for_prefix)
        return bounded_prefix + suffix, warnings

    bounded, _ = _hard_bound(prefix + suffix, budget)
    return bounded, warnings


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _render_compact_body(
    detail: SessionDetail,
    warnings: list[str],
    *,
    prompt_limit: int,
    tail_count: int,
    tail_field_limit: int,
    include_outlines: bool,
    list_limit: int,
) -> str:
    turns = detail.turns
    header = _render_header(detail, warnings, mode="compact")

    first_goal = turns[0].user_text if turns and turns[0].user_text else detail.summary.first_user_prompt
    final_prompt = ""
    for turn in reversed(turns):
        if turn.user_text:
            final_prompt = turn.user_text
            break
    if not final_prompt:
        final_prompt = detail.summary.last_user_prompt

    goal_text, _ = truncate_text(first_goal or "-", prompt_limit)
    final_text, _ = truncate_text(final_prompt or "-", prompt_limit)

    sections = [
        header,
        f"## User Goal & Latest Request\n- Turn 1 Goal: {goal_text}\n- Final User Prompt: {final_text}",
    ]

    if not turns:
        sections.append("## Turns\n[No full turn transcript recorded]")
    else:
        n = len(turns)
        actual_tail = min(tail_count, n)
        tail_start = max(0, n - actual_tail)
        turn_lines: list[str] = []

        if tail_start > 0:
            turn_lines.append("## Earlier Turn Outline")
            if include_outlines:
                for turn in turns[:tail_start]:
                    turn_lines.append(_render_turn_outline(turn))
            else:
                turn_lines.append(_drilldown_marker(detail.summary.session_id, tail_start))

        if actual_tail > 0:
            turn_lines.append(f"## Recent Turns (last {actual_tail})")
            for turn in turns[tail_start:]:
                turn_lines.append(_render_turn_full(turn, field_limit=tail_field_limit))

        sections.append("\n".join(turn_lines))

    files = _dedupe_preserve(detail.files_mentioned)
    commands = _dedupe_preserve(detail.commands_run)

    artifact_lines = ["## Files & Commands"]
    if files:
        shown_files = [truncate_text(f, _COMPACT_ITEM_CHARS, single_line=True)[0] for f in files[:list_limit]]
        extra_f = f" (+{len(files) - list_limit} more)" if len(files) > list_limit else ""
        artifact_lines.append(f"- files_mentioned ({len(files)}): " + ", ".join(shown_files) + extra_f)
    else:
        artifact_lines.append("- files_mentioned: none")

    if commands:
        shown_cmds = [truncate_text(c, _COMPACT_ITEM_CHARS, single_line=True)[0] for c in commands[:list_limit]]
        extra_c = f" (+{len(commands) - list_limit} more)" if len(commands) > list_limit else ""
        artifact_lines.append(f"- commands_run ({len(commands)}): " + " ; ".join(shown_cmds) + extra_c)
    else:
        artifact_lines.append("- commands_run: none")

    sections.append("\n".join(artifact_lines))
    return "\n\n".join(sections)


def _render_compact(
    detail: SessionDetail,
    base_warnings: list[str],
    *,
    budget: int,
) -> tuple[str, list[str]]:
    if budget <= 0:
        has_content = bool(detail.turns or detail.summary.title or detail.summary.first_user_prompt)
        warnings = _merge_warnings(base_warnings, ["bounded_history"] if has_content else [])
        return "", warnings

    candidate = _render_compact_body(
        detail,
        base_warnings,
        prompt_limit=_COMPACT_PROMPT_CHARS,
        tail_count=_COMPACT_TAIL_TURNS,
        tail_field_limit=_COMPACT_TAIL_FIELD_CHARS,
        include_outlines=True,
        list_limit=_COMPACT_LIST_ITEMS,
    )
    if len(candidate) <= budget:
        return candidate, list(base_warnings)

    warnings = _merge_warnings(base_warnings, ["bounded_history"])
    tight_profiles = (
        (300, 2, 600, True, 15),
        (200, 2, 400, False, 10),
        (160, 1, 250, False, 5),
    )
    for prompt_limit, tail_count, tail_field_limit, include_outlines, list_limit in tight_profiles:
        attempt = _render_compact_body(
            detail,
            warnings,
            prompt_limit=prompt_limit,
            tail_count=tail_count,
            tail_field_limit=tail_field_limit,
            include_outlines=include_outlines,
            list_limit=list_limit,
        )
        if len(attempt) <= budget:
            return attempt, warnings

    bounded, _ = _hard_bound(attempt, budget)
    return bounded, warnings


def render_show_envelope(
    result: ResolutionResult,
    *,
    mode: str = "full",
    max_chars: int | None = None,
    tail_turns: int | None = None,
) -> dict[str, Any]:
    """Build the standardized `show` JSON envelope in `full` or `compact` mode."""
    if mode not in ("full", "compact"):
        raise ValueError(f"Unsupported render mode: {mode!r} (expected 'full' or 'compact')")
    if tail_turns is not None and tail_turns < 0:
        raise ValueError(f"tail_turns must be non-negative, got {tail_turns}")

    budget = (
        max_chars
        if max_chars is not None
        else (DEFAULT_FULL_MAX_CHARS if mode == "full" else DEFAULT_COMPACT_MAX_CHARS)
    )

    detail = result.detail
    base_warnings = _merge_warnings(
        result.warnings,
        detail.warnings if detail is not None else [],
    )
    provider = result.provider or (detail.summary.provider if detail is not None else "")

    if result.status == "matched" and detail is not None:
        if mode == "full":
            rendered, warnings = _render_full(
                detail,
                base_warnings,
                budget=budget,
                tail_turns=tail_turns,
            )
        else:
            rendered, warnings = _render_compact(
                detail,
                base_warnings,
                budget=budget,
            )
        session_dict: dict[str, Any] | None = detail.summary.to_dict()
    else:
        rendered = ""
        warnings = base_warnings
        session_dict = None

    return {
        "schema_version": 1,
        "status": result.status,
        "reason": result.reason,
        "trust_boundary": TRUST_BOUNDARY,
        "mode": mode,
        "provider": provider,
        "cross_repo": result.cross_repo,
        "session": session_dict,
        "candidates": [candidate.to_dict() for candidate in result.candidates],
        "rendered": rendered,
        "warnings": warnings,
    }


def render_turns_envelope(detail: SessionDetail, start: int, end: int) -> dict[str, Any]:
    """Return a 1-based inclusive turn slice envelope for on-demand drill-down."""
    total_turns = len(detail.turns)
    if start < 1:
        raise ValueError(f"start must be >= 1, got {start}")
    if end < start:
        raise ValueError(f"end ({end}) must be >= start ({start})")
    if total_turns > 0 and start > total_turns:
        raise ValueError(f"start ({start}) exceeds total_turns ({total_turns})")
    if total_turns == 0 and start > 1:
        raise ValueError(f"start ({start}) is out of range for an empty session")

    if total_turns == 0:
        actual_end = 0
        selected: list[NormalizedTurn] = []
    else:
        actual_end = min(end, total_turns)
        selected = detail.turns[start - 1 : actual_end]

    return {
        "schema_version": 1,
        "trust_boundary": TRUST_BOUNDARY,
        "provider": detail.summary.provider,
        "session_id": detail.summary.session_id,
        "start": start,
        "end": actual_end,
        "total_turns": total_turns,
        "turns": [turn.to_dict() for turn in selected],
        "warnings": list(detail.warnings),
    }


def _format_candidate_lines(summaries: list[SessionSummary]) -> list[str]:
    lines: list[str] = []
    for idx, summary in enumerate(summaries, start=1):
        title = summary.title or summary.last_user_prompt or summary.first_user_prompt or "(untitled)"
        title_snip, _ = truncate_text(title, 80, single_line=True)
        repo_part = summary.repo_name or summary.cwd or "-"
        branch_part = summary.git_branch or "-"
        updated_part = summary.updated_at or summary.created_at or "-"
        lines.append(
            f"{idx}. [{summary.provider}] {summary.session_id} | {updated_part} | "
            f"{repo_part} ({branch_part}) | turns={summary.turn_count} | {title_snip}"
        )
    return lines


def format_list_text(sessions: list[SessionSummary], warnings: list[str]) -> str:
    """Format discovered sessions and warnings as human-readable text."""
    lines: list[str] = []
    if not sessions:
        lines.append("No matching sessions found.")
    else:
        lines.extend(_format_candidate_lines(sessions))

    if warnings:
        lines.append("")
        lines.append("Warnings: " + ", ".join(_merge_warnings(warnings)))
    return "\n".join(lines)


def format_show_text(envelope: dict[str, Any]) -> str:
    """Format a `show` envelope as human-readable text."""
    status = envelope.get("status", "")
    rendered = envelope.get("rendered", "")
    if status == "matched" and rendered:
        return str(rendered)

    lines = [
        f"Status: {status or 'unknown'} (reason: {envelope.get('reason') or '-'})",
        f"Trust Boundary: {envelope.get('trust_boundary', TRUST_BOUNDARY)}",
        f"Provider: {envelope.get('provider') or '-'} | Cross-Repo: {bool(envelope.get('cross_repo'))}",
    ]

    candidates_raw = envelope.get("candidates") or []
    if candidates_raw:
        lines.append("")
        lines.append("Candidates:")
        candidates = [
            SessionSummary(**c) if isinstance(c, dict) else c
            for c in candidates_raw
        ]
        lines.extend(_format_candidate_lines(candidates))

    warnings = envelope.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Warnings: " + ", ".join(str(w) for w in warnings))
    return "\n".join(lines)
