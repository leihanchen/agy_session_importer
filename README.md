# Antigravity Session Importer (`agy-session-importer`)

A zero-dependency, read-only Antigravity plugin and unified `/resume-session` skill that discovers, searches, and imports prior CLI coding agent sessions (starting with **OpenAI Codex** and **Claude Code**) into Antigravity.

## Features

- **Read-only local discovery:** Reads Codex rollout logs (`.jsonl` / `.jsonl.zst` + read-only SQLite WAL fallback) and Claude Code project DAG logs (`~/.claude/projects/` + `history.jsonl`) without mutating provider state.
- **Cross-provider natural-language search:** Resolves `latest`, exact/prefix session IDs, explicit file paths, and weighted keyword queries (including Unicode/CJK text) with repo/worktree awareness.
- **Interactive disambiguation & cross-repo guard:** Never silently guesses on ambiguous matches or cross-repository sessions; prompts with concise summaries and an explicit cancel option.
- **Two context-safe handoff modes:**
  - `full` (default): Absorbs Turn 1 + full or tail-pinned turns within a 60,000-character budget, with `turns --range START:END` drill-down when bounded.
  - `compact`: Generates a tight 12,000-character structured handoff (goal, outline, recent turns, files, commands).
- **Untrusted-history safety boundary:** Marks payloads with `trust_boundary: "untrusted_inert_history"`, strips foreign system/wrapper envelopes, drops reasoning/thinking/signatures/image blobs at parse time, and redacts high-confidence secrets.
- **Pluggable provider architecture:** Add new agent session formats by subclassing `SessionProvider` and registering them in `create_default_registry()`.

## Requirements

- Python 3.10+ (standard library only; no `pip` dependencies)
- Antigravity CLI (`agy`) for plugin installation/validation
- Optional `zstd` CLI on `PATH` only if reading compressed `.jsonl.zst` Codex rollouts

## Installation

### Option 1: Install as an Antigravity plugin

```bash
agy plugin install /path/to/agy_session_importer
```

Or symlink the repository into your Antigravity plugins directory:

```bash
ln -s /path/to/agy_session_importer ~/.gemini/config/plugins/agy-session-importer
```

### Option 2: Install as a standalone skill

Because all runtime scripts live inside `skills/resume-session/scripts/`, you can also copy or symlink just the skill folder:

```bash
ln -s /path/to/agy_session_importer/skills/resume-session ~/.gemini/config/skills/resume-session
```

### Environment Overrides

- `CODEX_HOME`: Override Codex storage root (defaults to `~/.codex`).
- `CLAUDE_CONFIG_DIR` or `CLAUDE_HOME`: Override Claude Code storage root (`CLAUDE_CONFIG_DIR` takes precedence; defaults to `~/.claude`).

## Usage in Antigravity

Invoke `/resume-session` or ask naturally in chat:

- `/resume-session` — Load the latest session for the current repo in `full` mode.
- `/resume-session codex --compact` — Build a concise 6-part handoff from the latest Codex session in the current repo.
- `/resume-session claude "OAuth refresh"` — Search Claude Code sessions matching `"OAuth refresh"`.
- `/resume-session 019d1234 --full` — Load a session by ID or ID prefix.
- Natural language: `"Resume my latest Codex session"`, `"Continue my Claude Code session on this repo"`, `"Pick up where I left off on the auth bug"`.

When multiple sessions match, no keywords match, or the top match belongs to another repository, Antigravity presents numbered candidate summaries alongside **None — cancel without importing** before loading anything.

## Standalone CLI Usage

You can also run the underlying CLI directly (read-only):

```bash
# List registered providers, aliases, and local availability
python3 skills/resume-session/scripts/session_importer.py providers --json

# List recent sessions for the current repo (or pass --all-cwds)
python3 skills/resume-session/scripts/session_importer.py list --all-cwds --limit 10 --json

# Resolve and render a session handoff (--mode full|compact)
python3 skills/resume-session/scripts/session_importer.py show latest --provider codex --mode compact --json

# Drill down into omitted turns when bounded_history is reported
python3 skills/resume-session/scripts/session_importer.py turns <session_id> --range 2:10 --json
```

## Architecture

```text
agy_session_importer/
├── plugin.json                              # Antigravity plugin manifest
├── skills/resume-session/
│   ├── SKILL.md                             # Unified /resume-session instructions
│   └── scripts/
│       ├── session_importer.py              # CLI entrypoint (providers, list, show, turns)
│       └── importer/
│           ├── models.py                    # SessionSummary, NormalizedTurn, SessionDetail, ToolEvent
│           ├── safety.py                    # Trust boundary, tag stripping, secret redaction
│           ├── search.py                    # Tokenizer, weighted scoring, disambiguation
│           ├── render.py                    # full/compact rendering, bounding, turns drill-down
│           └── providers/
│               ├── base.py                  # SessionProvider ABC, git root/worktree, RO SQLite helpers
│               ├── registry.py              # ProviderRegistry & create_default_registry()
│               ├── codex.py                 # Codex rollout JSONL/.zst + SQLite fallback
│               └── claude.py                # Claude project DAG JSONL + history.jsonl fallback
└── tests/                                   # unittest suite (synthetic temp-dir fixtures)
```

## Adding a New Agent Provider

The CLI and `SKILL.md` discover providers dynamically from `ProviderRegistry`, so adding a new agent requires no changes to `session_importer.py` or `SKILL.md`:

1. **Create `skills/resume-session/scripts/importer/providers/<agent>.py`:**
   Subclass `SessionProvider` from `importer.providers.base`:
   - Set class attributes: `name` (canonical lowercase slug, e.g. `"gemini"`), `display_name`, and optional `aliases` tuple.
   - Implement `is_available(self) -> bool` to check whether the agent's local storage directory exists.
   - Implement `list_sessions(self, *, include_archived: bool = False) -> tuple[list[SessionSummary], list[str]]` returning `(summaries, warnings)`.
   - Implement `can_handle_path(self, path: Path) -> bool` to recognize explicit transcript files for that agent.
   - Implement `load_session(self, ref: str, *, max_tool_chars: int = 600) -> SessionDetail` to parse and normalize turns and tool events.
   - Optionally override `extract_searchable_text(self, summary: SessionSummary, *, max_bytes: int = 262_144) -> str` for Stage-B body keyword search.
2. **Apply safety & read-only conventions:**
   - Use `clean_text()` and `truncate_text()` from `importer.safety` on all user, assistant, and tool text.
   - Use `normalize_timestamp()` from `importer.models` and `derive_repo_name()` / `open_sqlite_readonly()` from `importer.providers.base`.
   - Drop foreign system prompts, reasoning/thinking blocks, cryptographic signatures, and binary/image blobs at parse time.
   - Never write to the agent's data directory.
3. **Register the provider:**
   Update `create_default_registry(roots: dict[str, Path] | None = None)` in `skills/resume-session/scripts/importer/providers/registry.py` to instantiate and `registry.register(...)` your provider (forwarding `roots.get("<agent>")` if provided so tests stay isolated).
4. **Add unit tests:**
   Create `tests/test_<agent>_provider.py` using synthetic fixtures in `tempfile.TemporaryDirectory()` (never real home directories or personal transcripts) and verify the full suite passes.

## Verification

```bash
python3 -m unittest discover -s tests -v
agy plugin validate . --strict
```
