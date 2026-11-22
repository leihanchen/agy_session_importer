# Antigravity Session Importer (`agy-session-importer`)

A zero-dependency, read-only Antigravity MCP App plugin and unified `/resume-session` skill that lets you visually discover, inspect complete `Turn 1..N` histories, and import prior CLI coding agent sessions (starting with **OpenAI Codex** and **Claude Code**) as native conversations in Antigravity with full inline history and working context.

## Features

- **Interactive MCP App UI (`ui://session-importer/app.html`):** Single-file, CSP-compliant (`default-src 'none'`) UI embedded directly in Antigravity across four surfaces:
  - **Inline Conversation Timeline (`render_imported_conversation`):** Automatically embeds a scrollable `Turn 1..N` history widget directly inside every imported Antigravity conversation (auto-loading all turns on mount, with turn search, **Expand All Tools / Collapse All Tools**, and clickable file chips).
  - **Imported Conversations Tab:** Manage previously imported sessions, status badges, matched Antigravity workspace projects, and quick re-opening or removal.
  - **Discover Sessions Tab:** Search and filter prior agent sessions across providers and repositories with repo/worktree awareness and archived-session toggle.
  - **Full History Viewer Tab:** Paginated `Turn 1..N` transcript inspector with expandable tool inputs/outputs, file links (`com.google.antigravity/toggle-aux-pane`), command history, and snapshot persistence.
- **Native project conversation creation:** Matches the session's working directory (or git root) to Antigravity workspace projects (`~/.gemini/config/projects/*.json`) and creates a real Antigravity conversation via `sendMessage(target: "new")` in the side pane or background `agy -p=...` CLI spawn from the global sidebar—never mutating Antigravity's internal SQLite/protobuf databases directly.
- **Durable `0600` local snapshots:** Imported session details are persisted atomically with `0600` permissions under `${PLUGIN_DATA}` (`~/.gemini/antigravity/plugin_data/agy-session-importer` by default) so full `Turn 1..N` history remains viewable even if the original provider rollout file is later pruned or deleted.
- **Read-only local discovery:** Reads Codex rollout logs (`.jsonl` / `.jsonl.zst` + WAL-safe read-only SQLite fallback across `state_<N>.sqlite` and `thread_history_<N>.sqlite`) and Claude Code project DAG logs (`~/.claude/projects/` + `history.jsonl`) without mutating provider state.
- **Cross-provider natural-language search & disambiguation:** Resolves `latest`, exact/prefix session IDs, explicit file paths, and weighted keyword queries (including Unicode/CJK text), prompting with concise summaries and **None — cancel without importing** on ambiguity or cross-repo matches.
- **Untrusted-history & XSS safety boundary:** Marks payloads with `trust_boundary: "untrusted_inert_history"`, strips foreign system/wrapper envelopes, drops reasoning/thinking/signatures/image blobs at parse time, redacts high-confidence secrets, and renders all UI content strictly via safe DOM APIs (`textContent` / `createElement`).
- **Pluggable provider architecture:** Add new agent session formats by subclassing `SessionProvider` and registering them in `create_default_registry()`.

## How Context & History Are Inherited

When you import a Codex or Claude Code session into Antigravity, both **you** and the **Antigravity agent** inherit the prior session:

1. **Visual `Turn 1..N` History Inside the Chat:**
   - On UI imports (`sendMessage(target: "new")`) and `/resume-session`, Turn 1 automatically calls `render_imported_conversation` to embed the full interactive `Turn 1..N` timeline inline inside the conversation above a concise **Continuation Summary**.
   - On headless CLI imports (`agy -p`, where MCP calls are auto-denied), the new conversation renders the chronological `### Turn 1..N` User/Assistant/Tool transcript directly in Markdown inside the reply.
2. **Model Working Context (`full` vs. `compact` mode):**
   - **`full` mode (default, `60,000`-char prompt budget):** Normal sessions (`<= 60,000` chars) inject **100% of `Turn 1..N`** (every User message, Assistant reply, condensed tool call/output, touched files, and commands) into the new conversation's initial prompt. Massive sessions (`> 60,000` chars, e.g. 50+ turns) retain **Turn 1 (initial goal) + the most recent trailing `Turn K..N` turns** + full deduplicated files/commands, along with an explicit `turns <id> --range 2:K-1` marker so the agent can pull any omitted middle turns on demand.
   - **`compact` mode (`12,000`-char prompt budget):** Seeds a tight structured handoff (goal, condensed turn outline, recent turns, files, commands, and stopping point).
3. **Safety Filtering & Live Verification:**
   - Foreign system prompts, internal `thinking`/`reasoning` blocks, abandoned Claude retry branches, binary blobs, and detected API secrets (`[REDACTED]`) are stripped, and the agent verifies live `git status` and files on disk before making new edits.

## Requirements

- Python 3.10+ (standard library only at runtime; zero `pip` dependencies and no Node.js required at runtime)
- Antigravity CLI (`agy`) for plugin installation/validation and background conversation spawning
- Optional `zstd` CLI on `PATH` only if reading compressed `.jsonl.zst` Codex rollouts
- Node.js 18+ only if rebuilding `skills/resume-session/ui/app.html` from `ui_src/`

## Installation

### Option 1: Install as an Antigravity plugin (Recommended — includes MCP App UI)

```bash
agy plugin install /path/to/agy_session_importer
```

Or copy/symlink the repository into your global Antigravity plugins directory:

```bash
ln -s /path/to/agy_session_importer ~/.gemini/config/plugins/agy-session-importer
```

> **Note on upgrading from a standalone skill install:** If you previously copied or symlinked the skill directly at `~/.gemini/config/skills/resume-session`, remove that standalone copy/symlink when installing the full plugin so Antigravity loads the plugin's unified skill and MCP App server cleanly without duplicate-skill collisions:
>
> ```bash
> rm -rf ~/.gemini/config/skills/resume-session
> ```

### Option 2: Install as a standalone skill via `npx skills` (CLI-only fallback, no MCP UI)

Install directly from GitHub with the [Skills CLI](https://skills.sh):

```bash
npx skills add leihanchen/agy_session_importer
# Or install globally:
npx skills add leihanchen/agy_session_importer -g
```

Because all Python runtime scripts live inside `skills/resume-session/scripts/`, you can also copy or symlink just the skill folder manually:

```bash
ln -s /path/to/agy_session_importer/skills/resume-session ~/.gemini/config/skills/resume-session
```

### Environment Overrides

- `CODEX_HOME`: Override Codex storage root (defaults to `~/.codex`).
- `CLAUDE_CONFIG_DIR` or `CLAUDE_HOME`: Override Claude Code storage root (`CLAUDE_CONFIG_DIR` takes precedence; defaults to `~/.claude`).
- `AGY_SESSION_IMPORTER_DATA_DIR`: Override the persistent snapshot/index directory outside MCP `${PLUGIN_DATA}` (defaults to `~/.gemini/antigravity/plugin_data/agy-session-importer`).
- `AGY_PROJECTS_DIR`: Override Antigravity workspace project definitions directory (defaults to `~/.gemini/config/projects`).
- `AGY_BIN`: Override path to the `agy` CLI executable for background conversation creation.
- `AGY_SESSION_IMPORTER_UI_HTML`: Override path to the bundled `app.html` UI document during UI development.

## UI Entrypoints & Usage in Antigravity

When installed as a plugin, `mcp_config.json` launches `skills/resume-session/scripts/mcp_server.py` automatically with `--data-dir ${PLUGIN_DATA}`. You can access Session Importer from three entrypoints:

1. **Left Sidebar → Session Importer:** Opens the full-page global view to browse imported sessions, discover across repositories, inspect `Turn 1..N` histories, and launch imported Antigravity conversations in the matching project.
2. **Side Pane `+` → Session Importer:** Opens the conversation-aware auxiliary pane alongside your active chat, supporting 1-click `sendMessage(target: "new")` native conversation creation (with automatic inline `Turn 1..N` history embedding), `updateModelContext` context attachment, and clicking file chips to open workspace files in the side pane.
3. **In-chat `/resume-session` or natural-language prompts:**
   - `/resume-session` — Imports the latest session for the current repo in `full` mode (`import_session_context`) and embeds the interactive `Turn 1..N` conversation inline (`render_imported_conversation`).
   - `/resume-session codex --compact` — Imports the latest Codex session in `compact` mode and presents a 6-part verified handoff.
   - `/resume-session claude "OAuth refresh"` — Searches Claude Code sessions matching `"OAuth refresh"`.
   - `/resume-session 019d1234 --full` — Loads a session by ID or ID prefix.
   - Natural language: `"Open Session Importer"`, `"Resume my latest Codex session"`, `"Continue my Claude Code session on this repo"`, `"Pick up where I left off on the auth bug"`.

When multiple sessions match, no keywords match, or the top match belongs to another repository, Antigravity presents numbered candidate summaries alongside **None — cancel without importing** before loading anything.

### MCP Tools Exposed (`agy-session-importer_session_importer`)

- **Model + App tools:**
  - `open_session_importer`: Opens or updates the Left Sidebar / Side Pane Session Importer UI (`aux_pane` + `global` entrypoints).
  - `render_imported_conversation`: Embeds the interactive `Turn 1..N` conversation timeline directly inline inside a chat message.
  - `import_session_context`: Resolves a session, writes a `0600` snapshot, and returns bounded `untrusted_inert_history` handoff context.
- **App-only UI tools (`visibility: ["app"]`, hidden from model context):**
  - `app_list_sessions`, `app_get_session_detail`, `app_list_imports`, `app_start_import`, `app_complete_import`, `app_delete_import`.

## Standalone CLI Usage

You can also run the underlying CLI directly:

```bash
# List registered providers, aliases, and local availability
python3 skills/resume-session/scripts/session_importer.py providers --json

# List recent sessions for the current repo (or pass --all-cwds)
python3 skills/resume-session/scripts/session_importer.py list --all-cwds --limit 10 --json

# Resolve and render an ephemeral handoff without saving (--mode full|compact)
python3 skills/resume-session/scripts/session_importer.py show latest --provider codex --mode compact --json

# Drill down into omitted turns when bounded_history is reported
python3 skills/resume-session/scripts/session_importer.py turns <session_id> --range 2:10 --json

# Save a durable 0600 snapshot and spawn an Antigravity conversation (or pass --no-spawn)
python3 skills/resume-session/scripts/session_importer.py import latest --provider codex --mode full --json
python3 skills/resume-session/scripts/session_importer.py import latest --no-spawn --json

# List or delete persisted session imports
python3 skills/resume-session/scripts/session_importer.py imports --json
python3 skills/resume-session/scripts/session_importer.py imports --delete <import_id> --json
```

## Architecture

```text
agy_session_importer/
├── plugin.json                              # Antigravity plugin manifest (v0.2.0)
├── mcp_config.json                          # Plugin MCP server definition (session_importer)
├── assets/
│   └── logo.svg                             # 128x128 plugin icon
├── ui_src/                                  # MCP App UI TypeScript/CSS sources & build script
│   ├── package.json
│   ├── build.mjs                            # Bundles app.ts + styles.css -> ui/app.html (--check)
│   ├── styles.css
│   └── app.ts
├── skills/resume-session/
│   ├── SKILL.md                             # Unified /resume-session workflow (MCP-first + CLI fallback)
│   ├── ui/
│   │   └── app.html                         # Pre-bundled single-file MCP App UI (~691 KB, <10 MiB limit)
│   └── scripts/
│       ├── session_importer.py              # CLI entrypoint (providers, list, show, turns, import, imports)
│       ├── mcp_server.py                    # Pure-stdlib stdio JSON-RPC 2.0 MCP server & MCP App tools
│       └── importer/
│           ├── models.py                    # SessionSummary, NormalizedTurn, SessionDetail, ImportedSessionRecord
│           ├── safety.py                    # Trust boundary, tag stripping, secret redaction
│           ├── search.py                    # Tokenizer, weighted scoring, disambiguation
│           ├── render.py                    # full/compact rendering, bounding, turns drill-down
│           ├── store.py                     # Atomic 0600 snapshot store & import index
│           ├── agy_bridge.py                # Antigravity project matcher, seed prompt & agy CLI bridge
│           └── providers/
│               ├── base.py                  # SessionProvider ABC, git root/worktree, RO SQLite helpers
│               ├── registry.py              # ProviderRegistry & create_default_registry()
│               ├── codex.py                 # Codex rollout JSONL/.zst + SQLite fallback
│               └── claude.py                # Claude project DAG JSONL + history.jsonl fallback
└── tests/                                   # unittest suite (91 tests on synthetic temp-dir fixtures)
```

## Adding a New Agent Provider

The CLI, MCP server, UI provider filter, and `SKILL.md` all discover providers dynamically from `ProviderRegistry`, so adding a new agent requires no changes to `session_importer.py`, `mcp_server.py`, `ui_src/`, or `SKILL.md`:

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

## Development & Verification

```bash
# Rebuild the single-file UI bundle (only needed after editing ui_src/)
npm --prefix ui_src install
node ui_src/build.mjs

# Verify bundle reproducibility, full test suite, and strict plugin schema
node ui_src/build.mjs --check
python3 -m unittest discover -s tests -v
agy plugin validate . --strict
```
