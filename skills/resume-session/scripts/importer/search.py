"""Unicode/CJK-aware search, weighted metadata/body scoring, and session resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import time
import unicodedata

from importer.models import SessionDetail, SessionSummary, timestamp_to_epoch_ms
from importer.providers.base import ProviderReadError, SessionProvider, match_repo
from importer.providers.registry import ProviderRegistry

_WEIGHT_TITLE = 4.0
_WEIGHT_REPO = 3.0
_WEIGHT_SESSION_ID = 3.0
_WEIGHT_PROMPT = 2.5
_WEIGHT_BODY = 1.5
_SAME_REPO_MULTIPLIER = 1.3
_CONFIDENT_MIN_SOLE_SCORE = 2.0
_CONFIDENT_RUNNER_UP_RATIO = 1.5
_MAX_CANDIDATES = 8
_STAGE_B_SCAN_LIMIT = 40
_STAGE_B_MAX_BYTES = 262_144
_RECENCY_BONUS_CAP = 0.01

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+")
_COMPOUND_CHUNK_RE = re.compile(r"[^\W_]+(?:[-_./][^\W_]+)*")
_ALNUM_SEGMENT_RE = re.compile(r"[^\W_]+")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_HEX_PREFIX_RE = re.compile(r"^[0-9a-f-]+$", re.IGNORECASE)

_ENGLISH_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "continue",
        "conversation",
        "conversations",
        "could",
        "d",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "if",
        "import",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "ll",
        "m",
        "may",
        "me",
        "might",
        "must",
        "my",
        "no",
        "not",
        "of",
        "on",
        "one",
        "or",
        "our",
        "please",
        "re",
        "resume",
        "s",
        "session",
        "sessions",
        "she",
        "should",
        "so",
        "some",
        "t",
        "than",
        "that",
        "the",
        "their",
        "then",
        "these",
        "they",
        "this",
        "those",
        "to",
        "ve",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "yes",
        "you",
        "your",
    }
)


@dataclass
class ResolutionResult:
    """Structured outcome of resolving a user session reference or search query."""

    status: str
    reason: str
    provider: str
    cross_repo: bool
    detail: SessionDetail | None
    candidates: list[SessionSummary] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _DiscoveredSession:
    provider: SessionProvider
    summary: SessionSummary
    repo_relation: str
    updated_ms: int
    created_ms: int

    @property
    def in_current_repo(self) -> bool:
        return self.repo_relation != "different"

    @property
    def is_exact_cwd(self) -> bool:
        return self.repo_relation == "exact"

    @property
    def effective_updated_ms(self) -> int:
        return self.updated_ms or self.created_ms


@dataclass(frozen=True)
class _ScoredSession:
    item: _DiscoveredSession
    score: float
    matched_tokens: int
    matched_all_tokens: bool


def _merge_warnings(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for warning in group:
            if warning and warning not in seen:
                seen.add(warning)
                merged.append(warning)
    return merged


def _append_token(tok: str, stop_words: set[str], out: list[str], seen: set[str]) -> None:
    if tok and tok not in stop_words and tok not in seen:
        seen.add(tok)
        out.append(tok)


def _emit_cjk_tokens(run: str, stop_words: set[str], out: list[str], seen: set[str]) -> None:
    candidates = (run,) if len(run) == 1 else tuple(run[idx : idx + 2] for idx in range(len(run) - 1))
    for tok in candidates:
        _append_token(tok, stop_words, out, seen)


def _split_cjk_boundaries(segment: str) -> list[tuple[bool, str]]:
    """Split an alphanumeric segment into alternating (is_cjk, substring) pieces."""
    parts: list[tuple[bool, str]] = []
    pos = 0
    for match in _CJK_RE.finditer(segment):
        if match.start() > pos:
            parts.append((False, segment[pos : match.start()]))
        parts.append((True, match.group(0)))
        pos = match.end()
    if pos < len(segment):
        parts.append((False, segment[pos:]))
    return parts


def tokenize_query(
    query: str, *, extra_stop_words: set[str] | None = None
) -> list[str]:
    """Split a query into lowercase Unicode alphanumeric and CJK character/bigram tokens."""
    if not query or not query.strip():
        return []

    stop_words = set(_ENGLISH_STOP_WORDS)
    if extra_stop_words:
        for word in extra_stop_words:
            cleaned = word.strip().lower()
            if cleaned:
                stop_words.add(cleaned)

    tokens: list[str] = []
    seen: set[str] = set()
    lowered = unicodedata.normalize("NFKC", query).lower()

    for chunk in _COMPOUND_CHUNK_RE.findall(lowered):
        if chunk in stop_words:
            continue
        for segment in _ALNUM_SEGMENT_RE.findall(chunk):
            if segment in stop_words:
                continue
            for is_cjk, part in _split_cjk_boundaries(segment):
                if is_cjk:
                    _emit_cjk_tokens(part, stop_words, tokens, seen)
                else:
                    _append_token(part, stop_words, tokens, seen)

    return tokens


def _prepare_field(text: str) -> tuple[str, set[str]]:
    if not text:
        return "", set()
    normalized = unicodedata.normalize("NFKC", text)
    lowered = normalized.lower()
    camel_split = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", normalized).lower()
    word_tokens: set[str] = set()
    for variant in (lowered, camel_split):
        for segment in _ALNUM_SEGMENT_RE.findall(variant):
            word_tokens.add(segment)
            for is_cjk, part in _split_cjk_boundaries(segment):
                if not is_cjk:
                    word_tokens.add(part)
    return lowered, word_tokens


def _field_matches_token(lowered: str, word_tokens: set[str], token: str) -> bool:
    if not lowered:
        return False
    if _CJK_RE.search(token):
        return token in lowered
    if token in word_tokens:
        return True
    if len(token) == 3:
        return any(wt.startswith(token) for wt in word_tokens)
    if len(token) >= 4:
        return token in lowered
    return False


def _collect_sessions(
    selected_providers: list[SessionProvider],
    *,
    target_cwd: str,
    include_archived: bool = False,
) -> tuple[list[_DiscoveredSession], list[str]]:
    discovered: list[_DiscoveredSession] = []
    warnings: list[str] = []

    for prov in selected_providers:
        if not prov.is_available():
            continue
        try:
            summaries, prov_warnings = prov.list_sessions(include_archived=include_archived)
        except ProviderReadError as exc:
            warnings = _merge_warnings(warnings, [f"{prov.name}_read_error: {exc}"])
            continue
        warnings = _merge_warnings(warnings, prov_warnings)
        for summary in summaries:
            discovered.append(
                _DiscoveredSession(
                    provider=prov,
                    summary=summary,
                    repo_relation=match_repo(summary.cwd, target_cwd),
                    updated_ms=timestamp_to_epoch_ms(summary.updated_at),
                    created_ms=timestamp_to_epoch_ms(summary.created_at),
                )
            )

    return discovered, warnings


def _recency_sort_key(item: _DiscoveredSession, *, repo_first: bool = False) -> tuple:
    base = (
        item.effective_updated_ms,
        item.created_ms,
        1 if item.is_exact_cwd else 0,
        item.summary.session_id,
    )
    if repo_first:
        return (1 if item.in_current_repo else 0, *base)
    return base


def _with_recency_bonus(raw_scored: list[tuple[_DiscoveredSession, float, int]], total_tokens: int) -> list[_ScoredSession]:
    if not raw_scored:
        return []
    positive_ts = [item.effective_updated_ms for item, _, _ in raw_scored if item.effective_updated_ms > 0]
    min_ts = min(positive_ts) if positive_ts else 0
    max_ts = max(positive_ts) if positive_ts else 0
    span = max_ts - min_ts

    scored: list[_ScoredSession] = []
    for item, base_score, matched_count in raw_scored:
        bonus = 0.0
        if span > 0 and item.effective_updated_ms > 0:
            bonus = _RECENCY_BONUS_CAP * ((item.effective_updated_ms - min_ts) / span)
        scored.append(
            _ScoredSession(
                item=item,
                score=base_score + bonus,
                matched_tokens=matched_count,
                matched_all_tokens=(matched_count == total_tokens and total_tokens > 0),
            )
        )

    scored.sort(
        key=lambda entry: (
            entry.score,
            entry.item.effective_updated_ms,
            entry.item.created_ms,
            1 if entry.item.is_exact_cwd else 0,
            entry.item.summary.session_id,
        ),
        reverse=True,
    )
    return scored


def _score_stage_a(
    items: list[_DiscoveredSession], tokens: list[str]
) -> list[_ScoredSession]:
    raw_scored: list[tuple[_DiscoveredSession, float, int]] = []
    for item in items:
        s = item.summary
        weighted_fields = (
            (_WEIGHT_TITLE, *_prepare_field(s.title)),
            (_WEIGHT_REPO, *_prepare_field(s.repo_name)),
            (_WEIGHT_REPO, *_prepare_field(s.cwd)),
            (_WEIGHT_REPO, *_prepare_field(s.git_branch)),
            (_WEIGHT_SESSION_ID, *_prepare_field(s.session_id)),
            (_WEIGHT_PROMPT, *_prepare_field(s.first_user_prompt)),
            (_WEIGHT_PROMPT, *_prepare_field(s.last_user_prompt)),
        )

        token_sum = 0.0
        matched_count = 0
        for tok in tokens:
            best_weight = 0.0
            for weight, lowered, word_tokens in weighted_fields:
                if weight <= best_weight:
                    continue
                if _field_matches_token(lowered, word_tokens, tok):
                    best_weight = weight
            if best_weight > 0.0:
                token_sum += best_weight
                matched_count += 1

        if matched_count > 0:
            multiplier = _SAME_REPO_MULTIPLIER if item.in_current_repo else 1.0
            raw_scored.append((item, token_sum * multiplier, matched_count))

    return _with_recency_bonus(raw_scored, len(tokens))


def _score_stage_b(
    items: list[_DiscoveredSession], tokens: list[str]
) -> tuple[list[_ScoredSession], list[str]]:
    ordered = sorted(items, key=lambda it: _recency_sort_key(it, repo_first=True), reverse=True)
    raw_scored: list[tuple[_DiscoveredSession, float, int]] = []
    warnings: list[str] = []

    for item in ordered[:_STAGE_B_SCAN_LIMIT]:
        try:
            body_text = item.provider.extract_searchable_text(
                item.summary, max_bytes=_STAGE_B_MAX_BYTES
            )
        except ProviderReadError as exc:
            warnings = _merge_warnings(warnings, [f"{item.provider.name}_search_read_error: {exc}"])
            continue
        if not body_text:
            continue
        lowered, word_tokens = _prepare_field(body_text)
        matched_count = sum(
            1 for tok in tokens if _field_matches_token(lowered, word_tokens, tok)
        )
        if matched_count > 0:
            multiplier = _SAME_REPO_MULTIPLIER if item.in_current_repo else 1.0
            raw_scored.append((item, matched_count * _WEIGHT_BODY * multiplier, matched_count))

    return _with_recency_bonus(raw_scored, len(tokens)), warnings


def _score_items(
    items: list[_DiscoveredSession], tokens: list[str]
) -> tuple[list[_ScoredSession], str, list[str]]:
    if not tokens:
        return [], "no_tokens", []
    stage_a = _score_stage_a(items, tokens)
    if stage_a:
        return stage_a, "metadata_match", []
    stage_b, stage_b_warnings = _score_stage_b(items, tokens)
    return stage_b, "body_match", stage_b_warnings


def _result_provider(
    selected_providers: list[SessionProvider], candidates: list[SessionSummary]
) -> str:
    if len(selected_providers) == 1:
        return selected_providers[0].name
    if candidates:
        prov_names = {c.provider for c in candidates if c.provider}
        if len(prov_names) == 1:
            return next(iter(prov_names))
    return ""


def _recent_summaries(
    items: list[_DiscoveredSession], *, repo_first: bool = True, limit: int = _MAX_CANDIDATES
) -> list[SessionSummary]:
    ordered = sorted(items, key=lambda it: _recency_sort_key(it, repo_first=repo_first), reverse=True)
    return [it.summary for it in ordered[:max(0, limit)]]


def _try_load_session(
    provider: SessionProvider,
    load_ref: str,
    *,
    max_tool_chars: int,
    base_warnings: list[str],
    fallback_items: list[_DiscoveredSession] | None = None,
) -> tuple[SessionDetail | None, ResolutionResult | None]:
    try:
        return provider.load_session(load_ref, max_tool_chars=max_tool_chars), None
    except ProviderReadError as exc:
        candidates = (
            _recent_summaries(fallback_items, repo_first=True, limit=_MAX_CANDIDATES)
            if fallback_items
            else []
        )
        return None, ResolutionResult(
            status="no_match",
            reason="provider_read_error",
            provider=provider.name,
            cross_repo=False,
            detail=None,
            candidates=candidates,
            warnings=_merge_warnings(base_warnings, [f"{provider.name}_read_error: {exc}"]),
        )


def _load_matched_result(
    provider: SessionProvider,
    load_ref: str,
    *,
    cross_repo: bool,
    reason: str,
    max_tool_chars: int,
    base_warnings: list[str],
    fallback_items: list[_DiscoveredSession] | None = None,
) -> ResolutionResult:
    detail, err_result = _try_load_session(
        provider,
        load_ref,
        max_tool_chars=max_tool_chars,
        base_warnings=base_warnings,
        fallback_items=fallback_items,
    )
    if err_result is not None:
        return err_result
    assert detail is not None
    return ResolutionResult(
        status="matched",
        reason=reason,
        provider=provider.name,
        cross_repo=cross_repo,
        detail=detail,
        candidates=[],
        warnings=_merge_warnings(base_warnings, detail.warnings),
    )


def _resolve_explicit_path(
    registry: ProviderRegistry,
    selected_providers: list[SessionProvider],
    path: Path,
    *,
    provider_selector: str,
    target_cwd: str,
    allow_cross_repo: bool,
    max_tool_chars: int,
) -> ResolutionResult:
    selector_clean = (provider_selector or "all").strip().lower()
    detected: SessionProvider | None = None
    if selector_clean in ("", "all"):
        detected = registry.detect_for_path(path)
    else:
        candidate = registry.detect_for_path(path)
        if candidate is not None and candidate in selected_providers:
            detected = candidate
        else:
            detected = next((p for p in selected_providers if p.can_handle_path(path)), None)

    if detected is None:
        return ResolutionResult(
            status="no_match",
            reason="unsupported_path",
            provider=selected_providers[0].name if len(selected_providers) == 1 else "",
            cross_repo=False,
            detail=None,
            candidates=[],
            warnings=[f"No registered provider recognizes path: {path}"],
        )

    detail, err_result = _try_load_session(
        detected,
        str(path),
        max_tool_chars=max_tool_chars,
        base_warnings=[],
    )
    if err_result is not None:
        return err_result
    assert detail is not None
    cross_repo = match_repo(detail.summary.cwd, target_cwd) == "different"
    if cross_repo and not allow_cross_repo:
        return ResolutionResult(
            status="ambiguous",
            reason="cross_repo_confirmation_required",
            provider=detected.name,
            cross_repo=True,
            detail=None,
            candidates=[detail.summary],
            warnings=list(detail.warnings),
        )

    return ResolutionResult(
        status="matched",
        reason="explicit_path",
        provider=detected.name,
        cross_repo=cross_repo,
        detail=detail,
        candidates=[],
        warnings=list(detail.warnings),
    )


def _looks_like_id_prefix(ref_clean: str) -> bool:
    if not _HEX_PREFIX_RE.fullmatch(ref_clean):
        return False
    hex_chars = sum(1 for ch in ref_clean if ch != "-")
    return hex_chars >= 8


def _resolve_by_id(
    ref_clean: str,
    items: list[_DiscoveredSession],
    selected_providers: list[SessionProvider],
    *,
    allow_cross_repo: bool,
    max_tool_chars: int,
    warnings: list[str],
) -> ResolutionResult | None:
    if any(ch.isspace() for ch in ref_clean):
        return None
    ref_lower = ref_clean.lower()

    exact = [it for it in items if it.summary.session_id.lower() == ref_lower]
    matches = exact
    if not matches and _looks_like_id_prefix(ref_clean):
        matches = [it for it in items if it.summary.session_id.lower().startswith(ref_lower)]

    if not matches:
        return None

    if len(matches) == 1:
        chosen = matches[0]
        cross_repo = not chosen.in_current_repo
        if cross_repo and not allow_cross_repo:
            return ResolutionResult(
                status="ambiguous",
                reason="cross_repo_confirmation_required",
                provider=chosen.provider.name,
                cross_repo=True,
                detail=None,
                candidates=[chosen.summary],
                warnings=list(warnings),
            )
        load_ref = chosen.summary.session_id or chosen.summary.source_path
        return _load_matched_result(
            chosen.provider,
            load_ref,
            cross_repo=cross_repo,
            reason="session_id",
            max_tool_chars=max_tool_chars,
            base_warnings=warnings,
            fallback_items=items,
        )

    ordered = sorted(matches, key=lambda it: _recency_sort_key(it, repo_first=True), reverse=True)
    candidates = [it.summary for it in ordered[:_MAX_CANDIDATES]]
    return ResolutionResult(
        status="ambiguous",
        reason="ambiguous_session_id",
        provider=_result_provider(selected_providers, candidates),
        cross_repo=all(not it.in_current_repo for it in ordered[:_MAX_CANDIDATES]),
        detail=None,
        candidates=candidates,
        warnings=list(warnings),
    )


def _no_match_result(
    reason: str,
    items: list[_DiscoveredSession],
    selected_providers: list[SessionProvider],
    warnings: list[str],
    *,
    repo_first: bool = True,
) -> ResolutionResult:
    candidates = _recent_summaries(items, repo_first=repo_first, limit=_MAX_CANDIDATES)
    return ResolutionResult(
        status="no_match",
        reason=reason,
        provider=_result_provider(selected_providers, []),
        cross_repo=False,
        detail=None,
        candidates=candidates,
        warnings=list(warnings),
    )


def _resolve_latest(
    items: list[_DiscoveredSession],
    selected_providers: list[SessionProvider],
    *,
    max_tool_chars: int,
    warnings: list[str],
) -> ResolutionResult:
    same_repo = [it for it in items if it.in_current_repo]
    if not same_repo:
        return _no_match_result(
            "no_sessions_for_cwd",
            items,
            selected_providers,
            warnings,
            repo_first=False,
        )

    same_repo.sort(key=lambda it: _recency_sort_key(it, repo_first=False), reverse=True)
    chosen = same_repo[0]
    load_ref = chosen.summary.session_id or chosen.summary.source_path
    return _load_matched_result(
        chosen.provider,
        load_ref,
        cross_repo=False,
        reason="latest_same_repo",
        max_tool_chars=max_tool_chars,
        base_warnings=warnings,
        fallback_items=items,
    )


def _is_confident_match(scored: list[_ScoredSession]) -> bool:
    if not scored:
        return False
    top = scored[0]
    if not top.item.in_current_repo:
        return False
    if len(scored) == 1:
        return top.score >= _CONFIDENT_MIN_SOLE_SCORE
    runner_up = scored[1]
    return (
        top.matched_all_tokens
        and top.score >= _CONFIDENT_RUNNER_UP_RATIO * runner_up.score
    )


def list_sessions(
    registry: ProviderRegistry,
    *,
    provider: str = "all",
    cwd: str | None = None,
    all_cwds: bool = False,
    query: str = "",
    within_min: int = 0,
    limit: int = 20,
    include_archived: bool = False,
) -> tuple[list[SessionSummary], list[str]]:
    """List discovered sessions filtered by provider, repo/cwd, age, and optional query."""
    target_cwd = os.getcwd() if cwd is None else cwd
    selected_providers = registry.resolve(provider)
    discovered, warnings = _collect_sessions(
        selected_providers,
        target_cwd=target_cwd,
        include_archived=include_archived,
    )

    filtered = [
        item
        for item in discovered
        if all_cwds or item.in_current_repo
    ]

    if within_min > 0:
        cutoff_ms = int(time.time() * 1000) - within_min * 60_000
        filtered = [item for item in filtered if item.effective_updated_ms >= cutoff_ms]

    if query and query.strip():
        tokens = tokenize_query(query, extra_stop_words=registry.all_tokens())
        if not tokens:
            return [], warnings
        scored, _, stage_warnings = _score_items(filtered, tokens)
        warnings = _merge_warnings(warnings, stage_warnings)
        summaries = [entry.item.summary for entry in scored]
    else:
        ordered = sorted(filtered, key=lambda it: _recency_sort_key(it, repo_first=False), reverse=True)
        summaries = [item.summary for item in ordered]

    if limit <= 0:
        return [], warnings
    return summaries[:limit], warnings


def resolve_session(
    registry: ProviderRegistry,
    ref: str | None,
    *,
    provider: str = "all",
    cwd: str | None = None,
    allow_cross_repo: bool = False,
    max_tool_chars: int = 600,
    include_archived: bool = False,
) -> ResolutionResult:
    """Resolve a path, session ID/prefix, 'latest', or natural-language query to a session."""
    target_cwd = os.getcwd() if cwd is None else cwd
    selected_providers = registry.resolve(provider)
    ref_clean = (ref or "").strip()

    if ref_clean:
        candidate_path = Path(ref_clean).expanduser()
        if candidate_path.is_file():
            return _resolve_explicit_path(
                registry,
                selected_providers,
                candidate_path,
                provider_selector=provider,
                target_cwd=target_cwd,
                allow_cross_repo=allow_cross_repo,
                max_tool_chars=max_tool_chars,
            )

    discovered, warnings = _collect_sessions(
        selected_providers,
        target_cwd=target_cwd,
        include_archived=include_archived,
    )

    if not ref_clean or ref_clean.lower() == "latest":
        return _resolve_latest(
            discovered,
            selected_providers,
            max_tool_chars=max_tool_chars,
            warnings=warnings,
        )

    id_result = _resolve_by_id(
        ref_clean,
        discovered,
        selected_providers,
        allow_cross_repo=allow_cross_repo,
        max_tool_chars=max_tool_chars,
        warnings=warnings,
    )
    if id_result is not None:
        return id_result

    tokens = tokenize_query(ref_clean, extra_stop_words=registry.all_tokens())
    if not tokens:
        return _no_match_result(
            "empty_query_after_stop_words",
            discovered,
            selected_providers,
            warnings,
        )

    scored, match_reason, stage_warnings = _score_items(discovered, tokens)
    warnings = _merge_warnings(warnings, stage_warnings)
    if not scored:
        return _no_match_result(
            "no_keyword_matches",
            discovered,
            selected_providers,
            warnings,
        )

    if _is_confident_match(scored):
        top_item = scored[0].item
        load_ref = top_item.summary.session_id or top_item.summary.source_path
        return _load_matched_result(
            top_item.provider,
            load_ref,
            cross_repo=False,
            reason=match_reason,
            max_tool_chars=max_tool_chars,
            base_warnings=warnings,
            fallback_items=discovered,
        )

    top_cross_repo = not scored[0].item.in_current_repo
    candidates = [entry.item.summary for entry in scored[:_MAX_CANDIDATES]]
    return ResolutionResult(
        status="ambiguous",
        reason="cross_repo_top_candidate" if top_cross_repo else "multiple_candidates",
        provider=_result_provider(selected_providers, candidates),
        cross_repo=top_cross_repo,
        detail=None,
        candidates=candidates,
        warnings=warnings,
    )
