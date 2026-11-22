import {
  App,
  McpUiMessageResultSchema,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
  type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps";

type TabName = "imported" | "discover" | "history";
type ImportMode = "full" | "compact";

interface ProviderInfo {
  name: string;
  display_name: string;
  aliases: string[];
  available: boolean;
}

interface ImportedSessionRecord {
  import_id: string;
  provider: string;
  provider_display_name: string;
  session_id: string;
  source_kind: string;
  source_path: string;
  cwd: string;
  repo_name: string;
  git_branch: string;
  title: string;
  turn_count: number;
  mode: string;
  imported_at: string;
  status: "creating" | "ready" | "in_session" | "error" | string;
  agy_conversation_id: string;
  agy_project_id: string;
  agy_project_name: string;
  snapshot_path: string;
  error_message: string;
  warnings: string[];
}

interface SessionSummary {
  provider: string;
  session_id: string;
  source_kind: string;
  source_path: string;
  cwd: string;
  repo_name: string;
  git_branch: string;
  title: string;
  first_user_prompt: string;
  last_user_prompt: string;
  created_at: string;
  updated_at: string;
  turn_count: number;
  archived?: boolean;
  warnings?: string[];
  relative_time?: string;
  imported_record?: ImportedSessionRecord | null;
}

interface ToolEvent {
  call_id: string;
  tool_name: string;
  arguments_summary: string;
  output_summary: string;
  status: string;
  files_touched: string[];
  truncated: boolean;
}

interface NormalizedTurn {
  turn_index: number;
  timestamp: string;
  user_text: string;
  assistant_text: string;
  tool_events: ToolEvent[];
  compaction_before: boolean;
  subagent_notes: string[];
  warnings: string[];
}

interface SessionDetailPayload {
  schema_version: number;
  trust_boundary: string;
  summary: SessionSummary;
  turns: NormalizedTurn[];
  total_turns: number;
  turn_start: number;
  turn_end: number;
  has_more: boolean;
  has_more_turns: boolean;
  files_mentioned: string[];
  commands_run: string[];
  compaction_count: number;
  source: "snapshot" | "live" | string;
  import_record: ImportedSessionRecord | null;
  warnings: string[];
}

interface OpenSessionImporterPayload {
  schema_version: number;
  trust_boundary: string;
  inline_mode?: boolean;
  tab: TabName;
  cwd?: string;
  all_cwds?: boolean;
  providers: ProviderInfo[];
  imports: ImportedSessionRecord[];
  candidates: SessionSummary[];
  selected_session: SessionDetailPayload | null;
  has_more_turns: boolean;
  total_turns: number;
  warnings: string[];
}

interface StartImportPayload {
  schema_version: number;
  status: "creating" | "ambiguous" | "no_match" | "error" | string;
  import_record: ImportedSessionRecord | null;
  seed_prompt: string;
  spawn_via: string;
  candidates: SessionSummary[];
  warnings: string[];
}

interface StatusNotice {
  kind: "info" | "success" | "warning" | "danger";
  text: string;
}

interface AppState {
  tab: TabName;
  inlineMode: boolean;
  showFullTabsInInline: boolean;
  trustBoundary: string;
  providers: ProviderInfo[];
  imports: ImportedSessionRecord[];
  candidates: SessionSummary[];
  selectedSession: SessionDetailPayload | null;
  warnings: string[];
  loading: boolean;
  loadingLabel: string;
  notice: StatusNotice | null;
  discoverQuery: string;
  discoverProvider: string;
  discoverAllCwds: boolean;
  discoverCwd: string;
  discoverIncludeArchived: boolean;
  importMode: ImportMode;
  historyFilter: string;
  openToolDetails: Record<string, boolean>;
  busyKeys: Record<string, boolean>;
}

const POLL_INTERVAL_MS = 2000;
const INITIAL_FETCH_TIMEOUT_MS = 150;
const PAGE_SIZE = 50;

const state: AppState = {
  tab: "discover",
  inlineMode: false,
  showFullTabsInInline: false,
  trustBoundary: "untrusted_inert_history",
  providers: [],
  imports: [],
  candidates: [],
  selectedSession: null,
  warnings: [],
  loading: true,
  loadingLabel: "Connecting to Session Importer...",
  notice: null,
  discoverQuery: "",
  discoverProvider: "all",
  discoverAllCwds: true,
  discoverCwd: "",
  discoverIncludeArchived: false,
  importMode: "full",
  historyFilter: "",
  openToolDetails: {},
  busyKeys: {},
};

let initialResultReceived = false;
let pollTimer: number | null = null;
let pollInFlight = false;
let searchDebounceTimer: number | null = null;
let discoverRequestGeneration = 0;
let detailRequestGeneration = 0;

const app = new App({ name: "Session Importer", version: "0.2.0" }, {});

type ChildNode = Node | string | number | null | undefined | false;

interface ElementAttrs {
  className?: string;
  id?: string;
  type?: string;
  value?: string;
  placeholder?: string;
  title?: string;
  disabled?: boolean;
  checked?: boolean;
  open?: boolean;
  htmlFor?: string;
  ariaLabel?: string;
  onClick?: (ev: MouseEvent) => void;
  onInput?: (ev: Event) => void;
  onChange?: (ev: Event) => void;
  onToggle?: (ev: Event) => void;
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs?: ElementAttrs | null,
  ...children: ChildNode[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (attrs) {
    if (attrs.className) node.className = attrs.className;
    if (attrs.id) node.id = attrs.id;
    if (attrs.title) node.title = attrs.title;
    if (attrs.ariaLabel) node.setAttribute("aria-label", attrs.ariaLabel);
    if (attrs.htmlFor && node instanceof HTMLLabelElement) {
      node.htmlFor = attrs.htmlFor;
    }
    if (attrs.type && "type" in node) {
      (node as HTMLInputElement | HTMLButtonElement).type = attrs.type;
    }
    if (attrs.value !== undefined && "value" in node) {
      (node as HTMLInputElement | HTMLSelectElement).value = attrs.value;
    }
    if (attrs.placeholder && "placeholder" in node) {
      (node as HTMLInputElement).placeholder = attrs.placeholder;
    }
    if (attrs.disabled !== undefined && "disabled" in node) {
      (node as HTMLInputElement | HTMLButtonElement | HTMLSelectElement).disabled = attrs.disabled;
    }
    if (attrs.checked !== undefined && node instanceof HTMLInputElement) {
      node.checked = attrs.checked;
    }
    if (attrs.open !== undefined && node instanceof HTMLDetailsElement) {
      node.open = attrs.open;
    }
    if (attrs.onClick) node.addEventListener("click", attrs.onClick);
    if (attrs.onInput) node.addEventListener("input", attrs.onInput);
    if (attrs.onChange) node.addEventListener("change", attrs.onChange);
    if (attrs.onToggle) node.addEventListener("toggle", attrs.onToggle);
  }
  if (children.length === 1 && (typeof children[0] === "string" || typeof children[0] === "number")) {
    node.textContent = String(children[0]);
  } else {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      if (typeof child === "string" || typeof child === "number") {
        node.appendChild(document.createTextNode(String(child)));
      } else {
        node.appendChild(child);
      }
    }
  }
  return node;
}

function applyHostContext(ctx: McpUiHostContext | undefined): void {
  if (!ctx) return;
  if (ctx.theme) {
    applyDocumentTheme(ctx.theme);
  }
  if (ctx.styles?.variables) {
    applyHostStyleVariables(ctx.styles.variables);
  }
  if (ctx.styles?.css?.fonts) {
    applyHostFonts(ctx.styles.css.fonts);
  }
}

function setNotice(kind: StatusNotice["kind"], text: string): void {
  state.notice = { kind, text };
  render();
}

function clearNotice(): void {
  state.notice = null;
  render();
}

function providerDisplayName(providerName: string): string {
  const found = state.providers.find((p) => p.name === providerName);
  return found ? found.display_name : providerName || "Unknown";
}

function formatTimestamp(ts: string): string {
  if (!ts) return "-";
  const parsed = Date.parse(ts);
  if (Number.isNaN(parsed)) return ts;
  try {
    return new Date(parsed).toLocaleString();
  } catch {
    return ts;
  }
}

function badgeClassForStatus(status: string): string {
  switch (status) {
    case "ready":
      return "si-badge success";
    case "creating":
      return "si-badge warning";
    case "error":
      return "si-badge danger";
    case "in_session":
      return "si-badge info";
    default:
      return "si-badge";
  }
}

async function copyToClipboard(text: string, label: string): Promise<void> {
  if (!text) return;
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(text);
      setNotice("success", `Copied ${label} to clipboard.`);
      return;
    }
  } catch {
    // Fall through to execCommand fallback
  }

  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (ok) {
      setNotice("success", `Copied ${label} to clipboard.`);
      return;
    }
  } catch {
    // Ignore fallback failure
  }
  setNotice("warning", `${label}: ${text}`);
}

function resolveFileUri(filePath: string, baseCwd: string): string | null {
  const raw = filePath.trim();
  if (!raw) return null;
  if (raw.startsWith("file://")) return raw;

  let absPath = raw;
  const isAbsPosix = raw.startsWith("/");
  const isAbsWin = /^[A-Za-z]:[\\/]/.test(raw);
  if (!isAbsPosix && !isAbsWin) {
    const cwdClean = (baseCwd || "").trim().replace(/[\\/]+$/, "");
    if (!cwdClean) return null;
    absPath = `${cwdClean}/${raw.replace(/^\.[\\/]/, "")}`;
  }

  const normalized = absPath.replace(/\\/g, "/");
  const encoded = normalized
    .split("/")
    .map((segment, idx) => {
      if (idx === 1 && /^[A-Za-z]:$/.test(segment)) return segment;
      return encodeURIComponent(segment);
    })
    .join("/");

  return encoded.startsWith("/") ? `file://${encoded}` : `file:///${encoded}`;
}

async function openFileInAuxPane(filePath: string, baseCwd: string): Promise<void> {
  const uri = resolveFileUri(filePath, baseCwd);
  if (!uri) {
    await copyToClipboard(filePath, "file path");
    return;
  }
  try {
    const res = await app.request(
      {
        method: "com.google.antigravity/toggle-aux-pane",
        params: { tabType: "fileView", uri },
      },
      McpUiMessageResultSchema
    );
    if (res && res.isError) {
      await copyToClipboard(filePath, "file path (side pane unavailable)");
    }
  } catch {
    await copyToClipboard(filePath, "file path (copied)");
  }
}

function syncPolling(): void {
  const hasCreating = state.imports.some((rec) => rec.status === "creating");
  if (hasCreating && pollTimer === null) {
    pollTimer = window.setInterval(() => {
      void pollImports();
    }, POLL_INTERVAL_MS);
  } else if (!hasCreating && pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollImports(): Promise<void> {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const res = await app.callServerTool({
      name: "app_list_imports",
      arguments: {},
    });
    if (res.isError || !res.structuredContent) return;
    const sc = res.structuredContent as { imports?: ImportedSessionRecord[] };
    if (Array.isArray(sc.imports)) {
      applyUpdatedImports(sc.imports, true);
    }
  } catch {
    // Keep polling quietly on transient errors
  } finally {
    pollInFlight = false;
  }
}

function matchImportRecord(
  nextImports: ImportedSessionRecord[],
  byId: Map<string, ImportedSessionRecord>,
  provider: string,
  sessionId: string,
  sourcePath: string,
  existingImportId?: string
): ImportedSessionRecord | null {
  if (existingImportId) {
    return byId.get(existingImportId) ?? null;
  }
  return (
    nextImports.find(
      (r) =>
        r.provider === provider &&
        (sessionId
          ? r.session_id === sessionId
          : Boolean(sourcePath && !r.session_id && r.source_path === sourcePath))
    ) ?? null
  );
}

function applyUpdatedImports(nextImports: ImportedSessionRecord[], fromPoll = false): void {
  const prevImportsJson = JSON.stringify(state.imports);
  const nextImportsJson = JSON.stringify(nextImports);
  const importsChanged = prevImportsJson !== nextImportsJson;

  state.imports = nextImports;
  const byId = new Map<string, ImportedSessionRecord>();
  for (const rec of nextImports) {
    byId.set(rec.import_id, rec);
  }

  state.candidates = state.candidates.map((cand) => {
    const matched = matchImportRecord(
      nextImports,
      byId,
      cand.provider,
      cand.session_id,
      cand.source_path,
      cand.imported_record?.import_id
    );
    return matched === cand.imported_record ? cand : { ...cand, imported_record: matched };
  });

  let selectedImportChanged = false;
  if (state.selectedSession) {
    const prevSelectedRecJson = JSON.stringify(state.selectedSession.import_record);
    const selSummary = state.selectedSession.summary;
    const updated = matchImportRecord(
      nextImports,
      byId,
      selSummary.provider,
      selSummary.session_id,
      selSummary.source_path,
      state.selectedSession.import_record?.import_id
    );
    selectedImportChanged = prevSelectedRecJson !== JSON.stringify(updated);
    state.selectedSession = {
      ...state.selectedSession,
      import_record: updated,
    };
  }

  syncPolling();
  if (fromPoll) {
    if (!importsChanged) return;
    if (state.tab === "history" && !selectedImportChanged) return;
  }
  render();
}

function hydrateFromOpenResult(raw: unknown): void {
  if (!raw || typeof raw !== "object") return;
  const sc = raw as Partial<OpenSessionImporterPayload>;
  state.inlineMode = Boolean(sc.inline_mode);
  if (state.inlineMode) {
    state.showFullTabsInInline = false;
  }
  if (sc.trust_boundary) state.trustBoundary = sc.trust_boundary;
  if (typeof sc.cwd === "string") state.discoverCwd = sc.cwd;
  if (typeof sc.all_cwds === "boolean") state.discoverAllCwds = sc.all_cwds;
  if (Array.isArray(sc.providers)) state.providers = sc.providers;
  if (Array.isArray(sc.imports)) state.imports = sc.imports;
  if (Array.isArray(sc.candidates)) state.candidates = sc.candidates;
  if (Array.isArray(sc.warnings)) state.warnings = sc.warnings;
  if (sc.selected_session !== undefined) {
    const prevId = state.selectedSession?.summary?.session_id;
    const nextId = sc.selected_session?.summary?.session_id;
    state.selectedSession = sc.selected_session ?? null;
    state.historyFilter = "";
    if (prevId !== nextId) {
      state.openToolDetails = {};
    }
  }
  if (sc.tab === "imported" || sc.tab === "discover" || sc.tab === "history") {
    state.tab = sc.tab;
  }
  state.loading = false;
  syncPolling();
  render();
  if (
    state.inlineMode &&
    state.selectedSession &&
    Boolean(state.selectedSession.has_more_turns || state.selectedSession.has_more)
  ) {
    void loadMoreTurns(true);
  }
}

async function fetchInitialOpen(force = false): Promise<void> {
  state.loading = true;
  state.loadingLabel = "Loading sessions...";
  render();
  try {
    const res = await app.callServerTool({
      name: "open_session_importer",
      arguments: {},
    });
    if (!force && initialResultReceived) return;
    if (res.isError) {
      const errText = extractResultText(res) || "Failed to load Session Importer.";
      state.loading = false;
      setNotice("danger", errText);
      return;
    }
    if (res.structuredContent) {
      hydrateFromOpenResult(res.structuredContent);
    } else {
      state.loading = false;
      render();
    }
  } catch (err) {
    if (!force && initialResultReceived) return;
    state.loading = false;
    setNotice("danger", `Failed to load initial state: ${errorMessage(err)}`);
  }
}

async function refreshImportsList(): Promise<void> {
  const res = await app.callServerTool({
    name: "app_list_imports",
    arguments: {},
  });
  if (res.isError) {
    setNotice("danger", extractResultText(res) || "Failed to refresh imports.");
    return;
  }
  const sc = (res.structuredContent ?? {}) as { imports?: ImportedSessionRecord[] };
  if (Array.isArray(sc.imports)) {
    applyUpdatedImports(sc.imports);
  }
}

async function refreshHistoryDetail(): Promise<void> {
  const cur = state.selectedSession;
  if (!cur) return;
  const gen = ++detailRequestGeneration;
  delete state.busyKeys["load-next"];
  delete state.busyKeys["load-all"];
  const loadedEnd =
    cur.turns.length > 0
      ? cur.turns[cur.turns.length - 1].turn_index
      : cur.turn_end || PAGE_SIZE;
  const args: Record<string, unknown> = {
    from_snapshot: cur.source === "snapshot",
    turn_start: 1,
    turn_end: Math.max(PAGE_SIZE, loadedEnd),
  };
  if (cur.import_record?.import_id) {
    args.import_id = cur.import_record.import_id;
  } else {
    args.ref = cur.summary.session_id || cur.summary.source_path;
    args.provider = cur.summary.provider;
  }
  const res = await app.callServerTool({
    name: "app_get_session_detail",
    arguments: args,
  });
  if (gen !== detailRequestGeneration) return;
  if (res.isError) {
    setNotice("danger", extractResultText(res) || "Failed to refresh session history.");
    return;
  }
  if (res.structuredContent) {
    state.selectedSession = res.structuredContent as unknown as SessionDetailPayload;
    render();
  }
}

async function refreshActiveView(): Promise<void> {
  if (state.busyKeys["refresh"]) return;
  state.busyKeys["refresh"] = true;
  render();
  try {
    if (state.tab === "history") {
      await refreshHistoryDetail();
      await refreshImportsList();
    } else {
      await Promise.all([refreshImportsList(), refreshDiscoverSessions()]);
    }
  } catch (err) {
    setNotice("danger", `Refresh failed: ${errorMessage(err)}`);
  } finally {
    delete state.busyKeys["refresh"];
    render();
  }
}

function extractResultText(res: { content?: Array<{ type?: string; text?: string }> }): string {
  if (!Array.isArray(res.content)) return "";
  return res.content
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text ?? "")
    .join("\n")
    .trim();
}

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err || "Unknown error");
}

async function refreshDiscoverSessions(): Promise<void> {
  const gen = ++discoverRequestGeneration;
  state.busyKeys["discover"] = true;
  render();
  try {
    const args: Record<string, unknown> = {
      provider: state.discoverProvider || "all",
      query: state.discoverQuery,
      all_cwds: state.discoverAllCwds,
      include_archived: state.discoverIncludeArchived,
      limit: 30,
    };
    if (!state.discoverAllCwds && state.discoverCwd.trim()) {
      args.cwd = state.discoverCwd.trim();
    }
    const res = await app.callServerTool({
      name: "app_list_sessions",
      arguments: args,
    });
    if (gen !== discoverRequestGeneration) return;
    if (res.isError) {
      setNotice("danger", extractResultText(res) || "Failed to list sessions.");
      return;
    }
    const sc = (res.structuredContent ?? {}) as {
      providers?: ProviderInfo[];
      candidates?: SessionSummary[];
      warnings?: string[];
    };
    if (Array.isArray(sc.providers)) state.providers = sc.providers;
    if (Array.isArray(sc.candidates)) state.candidates = sc.candidates;
    if (Array.isArray(sc.warnings)) state.warnings = sc.warnings;
  } catch (err) {
    if (gen !== discoverRequestGeneration) return;
    setNotice("danger", `Search failed: ${errorMessage(err)}`);
  } finally {
    if (gen === discoverRequestGeneration) {
      delete state.busyKeys["discover"];
      render();
    }
  }
}

async function openSessionDetail(params: {
  ref?: string;
  provider?: string;
  importId?: string;
  fromSnapshot?: boolean;
}): Promise<void> {
  const gen = ++detailRequestGeneration;
  delete state.busyKeys["load-next"];
  delete state.busyKeys["load-all"];
  state.tab = "history";
  state.historyFilter = "";
  state.openToolDetails = {};
  state.loading = true;
  state.loadingLabel = "Loading session history...";
  render();

  try {
    const args: Record<string, unknown> = {
      from_snapshot: params.fromSnapshot ?? true,
      turn_start: 1,
      turn_end: PAGE_SIZE,
    };
    if (params.importId) {
      args.import_id = params.importId;
    } else if (params.ref) {
      args.ref = params.ref;
    }
    if (params.provider) {
      args.provider = params.provider;
    }
    const res = await app.callServerTool({
      name: "app_get_session_detail",
      arguments: args,
    });
    if (gen !== detailRequestGeneration) return;
    state.loading = false;
    if (res.isError) {
      setNotice("danger", extractResultText(res) || "Failed to load session history.");
      return;
    }
    if (res.structuredContent) {
      state.selectedSession = res.structuredContent as unknown as SessionDetailPayload;
    }
    render();
  } catch (err) {
    if (gen !== detailRequestGeneration) return;
    state.loading = false;
    setNotice("danger", `Failed to load session detail: ${errorMessage(err)}`);
  }
}

async function loadMoreTurns(loadAll: boolean): Promise<void> {
  const detail = state.selectedSession;
  if (!detail) return;
  const gen = detailRequestGeneration;
  const busyKey = loadAll ? "load-all" : "load-next";
  state.busyKeys[busyKey] = true;
  render();

  try {
    let current = detail;
    do {
      const loadedEnd =
        current.turns.length > 0
          ? current.turns[current.turns.length - 1].turn_index
          : current.turn_end || 0;
      const nextStart = loadedEnd + 1;
      if (nextStart > current.total_turns) break;
      const nextEnd = Math.min(current.total_turns, nextStart + PAGE_SIZE - 1);

      const args: Record<string, unknown> = {
        from_snapshot: current.source === "snapshot",
        turn_start: nextStart,
        turn_end: nextEnd,
      };
      if (current.import_record?.import_id) {
        args.import_id = current.import_record.import_id;
      } else {
        args.ref = current.summary.session_id || current.summary.source_path;
        args.provider = current.summary.provider;
      }

      const res = await app.callServerTool({
        name: "app_get_session_detail",
        arguments: args,
      });
      if (gen !== detailRequestGeneration) return;
      if (res.isError || !res.structuredContent) {
        setNotice("danger", extractResultText(res) || "Failed to load additional turns.");
        break;
      }

      const page = res.structuredContent as unknown as SessionDetailPayload;
      if (!page.turns || page.turns.length === 0) {
        current = { ...page, turns: current.turns };
        state.selectedSession = current;
        render();
        break;
      }
      const seen = new Set(current.turns.map((t) => t.turn_index));
      const mergedTurns = [...current.turns];
      for (const turn of page.turns || []) {
        if (!seen.has(turn.turn_index)) {
          seen.add(turn.turn_index);
          mergedTurns.push(turn);
        }
      }
      current = {
        ...page,
        turns: mergedTurns,
        turn_start: current.turn_start || 1,
        turn_end: page.turn_end,
      };
      state.selectedSession = current;
      render();
    } while (loadAll && (current.has_more_turns || current.has_more));
  } catch (err) {
    if (gen === detailRequestGeneration) {
      setNotice("danger", `Failed loading turns: ${errorMessage(err)}`);
    }
  } finally {
    if (gen === detailRequestGeneration) {
      delete state.busyKeys[busyKey];
      render();
    }
  }
}

async function startImportFlow(
  ref: string,
  provider: string,
  mode: ImportMode
): Promise<void> {
  const key = `import:${ref}`;
  if (state.busyKeys[key]) return;
  state.busyKeys[key] = true;
  render();

  try {
    const caps = app.getHostCapabilities();
    const useClientSend = Boolean(caps?.message);
    const spawnVia = useClientSend ? "client_send_message" : "cli_background";

    const startRes = await app.callServerTool({
      name: "app_start_import",
      arguments: {
        ref,
        provider: provider || "all",
        mode,
        spawn_via: spawnVia,
      },
    });

    if (startRes.isError && !startRes.structuredContent) {
      setNotice("danger", extractResultText(startRes) || "Import failed to start.");
      return;
    }

    const startSc = (startRes.structuredContent ?? {}) as Partial<StartImportPayload>;
    const record = startSc.import_record ?? null;
    if (record) {
      upsertImportRecord(record);
    }

    if (startSc.status === "ambiguous" || startSc.status === "no_match") {
      setNotice(
        "warning",
        extractResultText(startRes) || `Import could not start (status: ${startSc.status}).`
      );
      return;
    }

    if (startSc.status === "error" || record?.status === "error") {
      if (record) {
        state.tab = "imported";
      }
      setNotice(
        "danger",
        record?.error_message || extractResultText(startRes) || "Import failed."
      );
      return;
    }

    if (useClientSend && startSc.status === "creating" && record && startSc.seed_prompt) {
      let sentConversationId = "";
      let sendFailed = false;
      try {
        const msgParams = {
          role: "user" as const,
          content: [{ type: "text" as const, text: startSc.seed_prompt }],
          _meta: { "com.google.antigravity/message": { target: "new" } },
        };
        const msgRes = await app.sendMessage(
          msgParams as unknown as Parameters<App["sendMessage"]>[0]
        );
        const meta = (msgRes as { _meta?: Record<string, unknown> })?._meta;
        const agyMeta = meta?.["com.google.antigravity/message"] as
          | { conversationId?: unknown }
          | undefined;
        if (!msgRes?.isError && typeof agyMeta?.conversationId === "string" && agyMeta.conversationId.trim()) {
          sentConversationId = agyMeta.conversationId.trim();
        } else {
          sendFailed = true;
        }
      } catch {
        sendFailed = true;
      }

      if (!sendFailed && sentConversationId) {
        const completeRes = await app.callServerTool({
          name: "app_complete_import",
          arguments: {
            import_id: record.import_id,
            agy_conversation_id: sentConversationId,
          },
        });
        const compSc = (completeRes.structuredContent ?? {}) as {
          status?: string;
          import_record?: ImportedSessionRecord;
        };
        if (compSc.import_record) {
          upsertImportRecord(compSc.import_record);
        }
        state.tab = "imported";
        if (completeRes.isError || compSc.status === "error" || compSc.import_record?.status === "error") {
          setNotice(
            "danger",
            compSc.import_record?.error_message ||
              extractResultText(completeRes) ||
              "Failed to finalize import."
          );
        } else {
          setNotice(
            "success",
            `Imported into Antigravity conversation ${sentConversationId}.`
          );
        }
      } else {
        const fbRes = await app.callServerTool({
          name: "app_complete_import",
          arguments: {
            import_id: record.import_id,
            fallback_to_cli: true,
          },
        });
        const fbSc = (fbRes.structuredContent ?? {}) as {
          status?: string;
          import_record?: ImportedSessionRecord;
        };
        if (fbSc.import_record) {
          upsertImportRecord(fbSc.import_record);
        }
        state.tab = "imported";
        if (fbRes.isError || fbSc.status === "error" || fbSc.import_record?.status === "error") {
          setNotice(
            "danger",
            fbSc.import_record?.error_message ||
              extractResultText(fbRes) ||
              "Background CLI import failed."
          );
        } else {
          setNotice(
            "info",
            "Creating Antigravity conversation in the background via CLI..."
          );
        }
      }
    } else {
      state.tab = "imported";
      if (record?.status === "ready") {
        setNotice("success", `Imported session ready (${record.import_id}).`);
      } else {
        setNotice("info", "Import started in background. Tracking progress...");
      }
    }
  } catch (err) {
    setNotice("danger", `Import failed: ${errorMessage(err)}`);
  } finally {
    delete state.busyKeys[key];
    syncPolling();
    render();
  }
}

function upsertImportRecord(record: ImportedSessionRecord): void {
  const existingIdx = state.imports.findIndex((r) => r.import_id === record.import_id);
  const next = [...state.imports];
  if (existingIdx >= 0) {
    next[existingIdx] = record;
  } else {
    next.unshift(record);
  }
  applyUpdatedImports(next);
}

async function deleteImport(importId: string): Promise<void> {
  const key = `delete:${importId}`;
  state.busyKeys[key] = true;
  render();
  try {
    const res = await app.callServerTool({
      name: "app_delete_import",
      arguments: { import_id: importId },
    });
    if (res.isError) {
      setNotice("danger", extractResultText(res) || "Failed to delete import.");
      return;
    }
    const nextImports = state.imports.filter((r) => r.import_id !== importId);
    applyUpdatedImports(nextImports);
    setNotice("info", "Removed imported session record.");
  } catch (err) {
    setNotice("danger", `Delete failed: ${errorMessage(err)}`);
  } finally {
    delete state.busyKeys[key];
    render();
  }
}

async function attachContextToNextMessage(detail: SessionDetailPayload): Promise<void> {
  const caps = app.getHostCapabilities();
  if (!caps?.updateModelContext) return;
  const s = detail.summary;
  const tailTurns = detail.turns.slice(-5).map((t) => {
    const u = t.user_text ? `User: ${t.user_text.slice(0, 300)}` : "";
    const a = t.assistant_text ? `Assistant: ${t.assistant_text.slice(0, 300)}` : "";
    return `Turn ${t.turn_index}: ${[u, a].filter(Boolean).join(" | ")}`;
  });

  const summaryMarkdown = [
    `[UNTRUSTED_INERT_HISTORY | trust_boundary=${detail.trust_boundary}]`,
    `Provider: ${providerDisplayName(s.provider)} (${s.provider})`,
    `Session ID: ${s.session_id}`,
    `Title: ${s.title || s.first_user_prompt || "-"}`,
    `Repo / CWD: ${s.repo_name || "-"} (${s.cwd || "-"})`,
    `Branch: ${s.git_branch || "-"} | Turns: ${detail.total_turns}`,
    detail.files_mentioned.length > 0
      ? `Files Mentioned: ${detail.files_mentioned.slice(0, 20).join(", ")}`
      : "",
    detail.commands_run.length > 0
      ? `Commands Run: ${detail.commands_run.slice(0, 10).join(" ; ")}`
      : "",
    tailTurns.length > 0 ? `Recent Loaded Turns:\n${tailTurns.join("\n")}` : "",
  ]
    .filter(Boolean)
    .join("\n");

  try {
    await app.updateModelContext({
      content: [{ type: "text", text: summaryMarkdown }],
    });
    setNotice("success", "Attached session context to your next message.");
  } catch (err) {
    setNotice("danger", `Failed to attach context: ${errorMessage(err)}`);
  }
}

function buildCliResumeSnippet(provider: string, sessionId: string): string {
  return `python3 skills/resume-session/scripts/session_importer.py show ${sessionId} --provider ${provider}`;
}

function renderTopbar(): HTMLElement {
  const tabs: Array<{ id: TabName; label: string; count?: number }> = [
    { id: "imported", label: "Imported Conversations", count: state.imports.length },
    { id: "discover", label: "Discover Sessions", count: state.candidates.length },
    { id: "history", label: "History Viewer" },
  ];

  return el(
    "header",
    { className: "si-topbar" },
    el(
      "div",
      { className: "si-topbar-header" },
      el(
        "div",
        { className: "si-title-row" },
        el("h1", { className: "si-title" }, "Session Importer"),
        el("span", { className: "si-trust-badge" }, state.trustBoundary)
      ),
      el(
        "div",
        { className: "si-badges" },
        el(
          "button",
          {
            className: "si-btn",
            type: "button",
            disabled: Boolean(state.busyKeys["refresh"]),
            onClick: () => {
              void refreshActiveView();
            },
          },
          state.busyKeys["refresh"] ? "Refreshing..." : "Refresh"
        )
      )
    ),
    el(
      "nav",
      { className: "si-tabs", ariaLabel: "Session Importer Views" },
      ...tabs.map((tabInfo) =>
        el(
          "button",
          {
            type: "button",
            className: state.tab === tabInfo.id ? "si-tab-btn active" : "si-tab-btn",
            onClick: () => {
              state.tab = tabInfo.id;
              render();
            },
          },
          tabInfo.label,
          tabInfo.count !== undefined
            ? el("span", { className: "si-tab-count" }, tabInfo.count)
            : null
        )
      )
    )
  );
}

function renderModeToggle(): HTMLElement {
  return el(
    "div",
    { className: "si-segmented", ariaLabel: "Import mode" },
    el(
      "button",
      {
        type: "button",
        className: state.importMode === "compact" ? "active" : "",
        onClick: () => {
          state.importMode = "compact";
          render();
        },
      },
      "Compact"
    ),
    el(
      "button",
      {
        type: "button",
        className: state.importMode === "full" ? "active" : "",
        onClick: () => {
          state.importMode = "full";
          render();
        },
      },
      "Full"
    )
  );
}

function renderImportedTab(): HTMLElement {
  if (state.imports.length === 0) {
    return el(
      "div",
      { className: "si-empty" },
      el("p", null, "No imported sessions yet."),
      el(
        "button",
        {
          type: "button",
          className: "si-btn primary",
          onClick: () => {
            state.tab = "discover";
            render();
          },
        },
        "Discover Sessions"
      )
    );
  }

  return el(
    "div",
    { className: "si-list" },
    ...state.imports.map((rec) => {
      const displayTitle = rec.title || rec.session_id || rec.import_id;
      const busyImport = Boolean(state.busyKeys[`import:${rec.import_id}`]);
      const busyDelete = Boolean(state.busyKeys[`delete:${rec.import_id}`]);
      return el(
        "article",
        { className: "si-card" },
        el(
          "div",
          { className: "si-card-header" },
          el("h2", { className: "si-card-title" }, displayTitle),
          el(
            "div",
            { className: "si-badges" },
            el("span", { className: "si-badge info" }, rec.provider_display_name || providerDisplayName(rec.provider)),
            el("span", { className: badgeClassForStatus(rec.status) }, rec.status),
            el("span", { className: "si-badge" }, rec.mode || "full"),
            el("span", { className: "si-badge" }, `${rec.turn_count} turns`)
          )
        ),
        el(
          "div",
          { className: "si-meta-row" },
          rec.repo_name ? el("span", null, `Repo: ${rec.repo_name}`) : null,
          rec.git_branch ? el("span", null, `Branch: ${rec.git_branch}`) : null,
          rec.imported_at ? el("span", null, `Imported: ${formatTimestamp(rec.imported_at)}`) : null,
          rec.agy_project_name ? el("span", null, `Project: ${rec.agy_project_name}`) : null
        ),
        rec.cwd ? el("div", { className: "si-meta-row si-mono" }, rec.cwd) : null,
        rec.agy_conversation_id
          ? el("div", { className: "si-meta-row si-mono" }, `Conversation ID: ${rec.agy_conversation_id}`)
          : null,
        rec.error_message
          ? el("div", { className: "si-banner danger" }, rec.error_message)
          : null,
        el(
          "div",
          { className: "si-actions" },
          el(
            "button",
            {
              type: "button",
              className: "si-btn primary",
              onClick: () => {
                void openSessionDetail({
                  importId: rec.import_id,
                  provider: rec.provider,
                  fromSnapshot: true,
                });
              },
            },
            "View Full History"
          ),
          el(
            "button",
            {
              type: "button",
              className: "si-btn",
              onClick: () => {
                const payload = rec.agy_conversation_id
                  ? rec.agy_conversation_id
                  : buildCliResumeSnippet(rec.provider, rec.session_id);
                const label = rec.agy_conversation_id ? "Conversation ID" : "Resume CLI";
                void copyToClipboard(payload, label);
              },
            },
            rec.agy_conversation_id ? "Copy Conversation ID" : "Copy Resume CLI"
          ),
          el(
            "button",
            {
              type: "button",
              className: "si-btn",
              disabled: busyImport,
              onClick: () => {
                const mode: ImportMode = rec.mode === "compact" ? "compact" : state.importMode;
                void startImportFlow(rec.import_id, rec.provider, mode);
              },
            },
            busyImport ? "Starting..." : "New Conversation"
          ),
          el(
            "button",
            {
              type: "button",
              className: "si-btn danger",
              disabled: busyDelete,
              onClick: () => {
                void deleteImport(rec.import_id);
              },
            },
            busyDelete ? "Removing..." : "Remove"
          )
        )
      );
    })
  );
}

function renderDiscoverTab(): HTMLElement {
  const providerSelect = el(
    "select",
    {
      className: "si-select",
      ariaLabel: "Filter by provider",
      value: state.discoverProvider,
      onChange: (ev) => {
        const target = ev.target as HTMLSelectElement;
        state.discoverProvider = target.value;
        void refreshDiscoverSessions();
      },
    },
    el("option", { value: "all" }, "All Providers"),
    ...state.providers.map((p) =>
      el(
        "option",
        { value: p.name },
        p.available ? p.display_name : `${p.display_name} (unavailable)`
      )
    )
  );
  providerSelect.value = state.discoverProvider;

  const toolbar = el(
    "div",
    { className: "si-toolbar" },
    el("input", {
      id: "si-discover-search",
      type: "search",
      className: "si-input",
      placeholder: "Search title, prompt, repo, branch, session ID...",
      value: state.discoverQuery,
      ariaLabel: "Search discovered sessions",
      onInput: (ev) => {
        state.discoverQuery = (ev.target as HTMLInputElement).value;
        if (searchDebounceTimer !== null) {
          window.clearTimeout(searchDebounceTimer);
        }
        searchDebounceTimer = window.setTimeout(() => {
          void refreshDiscoverSessions();
        }, 250);
      },
    }),
    providerSelect,
    el(
      "div",
      { className: "si-segmented", ariaLabel: "Workspace scope" },
      el(
        "button",
        {
          type: "button",
          className: state.discoverAllCwds ? "active" : "",
          onClick: () => {
            if (!state.discoverAllCwds) {
              state.discoverAllCwds = true;
              void refreshDiscoverSessions();
            }
          },
        },
        "All Repos"
      ),
      el(
        "button",
        {
          type: "button",
          className: !state.discoverAllCwds ? "active" : "",
          onClick: () => {
            if (state.discoverAllCwds) {
              state.discoverAllCwds = false;
              if (!state.discoverCwd && state.candidates.length > 0) {
                state.discoverCwd = state.candidates[0].cwd || "";
              }
              void refreshDiscoverSessions();
            }
          },
        },
        "Workspace"
      )
    ),
    !state.discoverAllCwds
      ? el("input", {
          id: "si-discover-cwd",
          type: "text",
          className: "si-input",
          placeholder: "Workspace path (optional cwd filter)...",
          value: state.discoverCwd,
          ariaLabel: "Workspace CWD filter",
          onInput: (ev) => {
            state.discoverCwd = (ev.target as HTMLInputElement).value;
          },
          onChange: (ev) => {
            state.discoverCwd = (ev.target as HTMLInputElement).value;
            void refreshDiscoverSessions();
          },
        })
      : null,
    el(
      "label",
      { className: "si-checkbox-label" },
      el("input", {
        type: "checkbox",
        checked: state.discoverIncludeArchived,
        onChange: (ev) => {
          state.discoverIncludeArchived = (ev.target as HTMLInputElement).checked;
          void refreshDiscoverSessions();
        },
      }),
      "Include archived"
    ),
    renderModeToggle(),
    el(
      "button",
      {
        type: "button",
        className: "si-btn",
        disabled: Boolean(state.busyKeys["discover"]),
        onClick: () => {
          void refreshDiscoverSessions();
        },
      },
      state.busyKeys["discover"] ? "Searching..." : "Search"
    )
  );

  const listNode =
    state.candidates.length === 0
      ? el(
          "div",
          { className: "si-empty" },
          el("p", null, "No matching external sessions found.")
        )
      : el(
          "div",
          { className: "si-list" },
          ...state.candidates.map((cand) => {
            const ref = cand.session_id || cand.source_path;
            const busyImport = Boolean(state.busyKeys[`import:${ref}`]);
            const displayTitle = cand.title || cand.first_user_prompt || cand.session_id;
            return el(
              "article",
              { className: "si-card" },
              el(
                "div",
                { className: "si-card-header" },
                el("h2", { className: "si-card-title" }, displayTitle),
                el(
                  "div",
                  { className: "si-badges" },
                  el("span", { className: "si-badge info" }, providerDisplayName(cand.provider)),
                  el("span", { className: "si-badge" }, `${cand.turn_count} turns`),
                  cand.relative_time
                    ? el("span", { className: "si-badge" }, cand.relative_time)
                    : cand.updated_at
                      ? el("span", { className: "si-badge" }, formatTimestamp(cand.updated_at))
                      : null,
                  cand.archived ? el("span", { className: "si-badge warning" }, "archived") : null,
                  cand.imported_record
                    ? el("span", { className: badgeClassForStatus(cand.imported_record.status) }, `Imported (${cand.imported_record.status})`)
                    : null
                )
              ),
              el(
                "div",
                { className: "si-meta-row" },
                cand.repo_name ? el("span", null, `Repo: ${cand.repo_name}`) : null,
                cand.git_branch ? el("span", null, `Branch: ${cand.git_branch}`) : null,
                el("span", { className: "si-mono" }, cand.session_id)
              ),
              cand.cwd ? el("div", { className: "si-meta-row si-mono" }, cand.cwd) : null,
              cand.last_user_prompt || cand.first_user_prompt
                ? el(
                    "p",
                    { className: "si-preview" },
                    cand.last_user_prompt || cand.first_user_prompt
                  )
                : null,
              el(
                "div",
                { className: "si-actions" },
                el(
                  "button",
                  {
                    type: "button",
                    className: "si-btn",
                    onClick: () => {
                      void openSessionDetail({
                        ref,
                        provider: cand.provider,
                        importId: cand.imported_record?.import_id,
                        fromSnapshot: true,
                      });
                    },
                  },
                  "Inspect History"
                ),
                el(
                  "button",
                  {
                    type: "button",
                    className: "si-btn primary",
                    disabled: busyImport,
                    onClick: () => {
                      void startImportFlow(ref, cand.provider, state.importMode);
                    },
                  },
                  busyImport ? "Importing..." : "Import to Antigravity"
                )
              )
            );
          })
        );

  return el("div", { className: "si-list" }, toolbar, listNode);
}

function turnMatchesFilter(turn: NormalizedTurn, queryLower: string): boolean {
  if (!queryLower) return true;
  if ((turn.user_text || "").toLowerCase().includes(queryLower)) return true;
  if ((turn.assistant_text || "").toLowerCase().includes(queryLower)) return true;
  for (const note of turn.subagent_notes || []) {
    if (note.toLowerCase().includes(queryLower)) return true;
  }
  for (const ev of turn.tool_events || []) {
    if ((ev.tool_name || "").toLowerCase().includes(queryLower)) return true;
    if ((ev.arguments_summary || "").toLowerCase().includes(queryLower)) return true;
    if ((ev.output_summary || "").toLowerCase().includes(queryLower)) return true;
  }
  return false;
}

function renderTurnCard(turn: NormalizedTurn, sessionKey: string): HTMLElement {
  const toolEvents = turn.tool_events || [];
  const subagentNotes = turn.subagent_notes || [];
  const detailKey = `${sessionKey}:turn:${turn.turn_index}`;

  return el(
    "article",
    { className: "si-turn-card" },
    el(
      "div",
      { className: "si-turn-header" },
      el("strong", null, `Turn ${turn.turn_index}`),
      el("span", null, turn.timestamp ? formatTimestamp(turn.timestamp) : "")
    ),
    turn.compaction_before
      ? el(
          "div",
          { className: "si-compaction-banner" },
          "Compaction occurred prior to this turn"
        )
      : null,
    turn.user_text
      ? el(
          "div",
          { className: "si-msg-block user" },
          el("div", { className: "si-msg-role" }, "User"),
          el("p", { className: "si-msg-text" }, turn.user_text)
        )
      : null,
    turn.assistant_text
      ? el(
          "div",
          { className: "si-msg-block assistant" },
          el("div", { className: "si-msg-role" }, "Assistant"),
          el("p", { className: "si-msg-text" }, turn.assistant_text)
        )
      : null,
    toolEvents.length > 0
      ? el(
          "details",
          {
            className: "si-details",
            open: Boolean(state.openToolDetails[detailKey]),
            onToggle: (ev) => {
              state.openToolDetails[detailKey] = (ev.currentTarget as HTMLDetailsElement).open;
            },
          },
          el("summary", null, `Tool Events (${toolEvents.length})`),
          el(
            "div",
            { className: "si-tool-list" },
            ...toolEvents.map((ev) =>
              el(
                "div",
                { className: "si-tool-item" },
                el(
                  "div",
                  { className: "si-badges" },
                  el("span", { className: "si-badge info si-mono" }, ev.tool_name || "tool"),
                  el("span", { className: "si-badge" }, ev.status || "unknown"),
                  ev.truncated ? el("span", { className: "si-badge warning" }, "truncated") : null
                ),
                ev.arguments_summary
                  ? el("pre", { className: "si-pre" }, `args: ${ev.arguments_summary}`)
                  : null,
                ev.output_summary
                  ? el("pre", { className: "si-pre" }, `out: ${ev.output_summary}`)
                  : null
              )
            )
          )
        )
      : null,
    subagentNotes.length > 0
      ? el(
          "div",
          { className: "si-banner info" },
          `Subagent notes: ${subagentNotes.join(" | ")}`
        )
      : null,
    turn.warnings && turn.warnings.length > 0
      ? el("div", { className: "si-banner warning" }, `Warnings: ${turn.warnings.join(", ")}`)
      : null
  );
}

function renderHistoryTab(): HTMLElement {
  const detail = state.selectedSession;
  if (!detail) {
    return el(
      "div",
      { className: "si-empty" },
      el("p", null, "Select a session from Imported Conversations or Discover Sessions to inspect its full turn history."),
      el(
        "div",
        { className: "si-actions" },
        el(
          "button",
          {
            type: "button",
            className: "si-btn primary",
            onClick: () => {
              state.tab = "discover";
              render();
            },
          },
          "Browse Discover Sessions"
        )
      )
    );
  }

  const s = detail.summary;
  const caps = app.getHostCapabilities();
  const canUpdateContext = Boolean(caps?.updateModelContext);
  const loadedEnd =
    detail.turns.length > 0
      ? detail.turns[detail.turns.length - 1].turn_index
      : detail.turn_end || 0;
  const loadedStart = detail.turns.length > 0 ? detail.turns[0].turn_index : detail.turn_start || 1;
  const rangeLabel =
    detail.total_turns <= 0
      ? "0 turns"
      : detail.turns.length > 0
        ? `Turn ${loadedStart} .. Turn ${loadedEnd} (of ${detail.total_turns})`
        : `0 of ${detail.total_turns} turns loaded`;

  const importRef = detail.import_record?.import_id || s.session_id || s.source_path;
  const busyImport = Boolean(state.busyKeys[`import:${importRef}`]);

  const headerCard = el(
    "section",
    { className: "si-detail-header" },
    el(
      "div",
      { className: "si-card-header" },
      el("h2", { className: "si-card-title" }, s.title || s.first_user_prompt || s.session_id),
      el(
        "div",
        { className: "si-badges" },
        el("span", { className: "si-badge info" }, providerDisplayName(s.provider)),
        el("span", { className: "si-badge" }, `source: ${detail.source}`),
        el("span", { className: "si-badge" }, rangeLabel),
        detail.compaction_count > 0
          ? el("span", { className: "si-badge warning" }, `${detail.compaction_count} compactions`)
          : null,
        detail.import_record
          ? el(
              "span",
              { className: badgeClassForStatus(detail.import_record.status) },
              `Imported (${detail.import_record.status})`
            )
          : null
      )
    ),
    el(
      "div",
      { className: "si-meta-row" },
      s.repo_name ? el("span", null, `Repo: ${s.repo_name}`) : null,
      s.git_branch ? el("span", null, `Branch: ${s.git_branch}`) : null,
      el("span", { className: "si-mono" }, s.session_id)
    ),
    s.cwd ? el("div", { className: "si-meta-row si-mono" }, s.cwd) : null,
    el(
      "div",
      { className: "si-actions" },
      ...(state.inlineMode && !state.showFullTabsInInline
        ? []
        : [
            renderModeToggle(),
            el(
              "button",
              {
                type: "button",
                className: "si-btn primary",
                disabled: busyImport,
                onClick: () => {
                  void startImportFlow(importRef, s.provider, state.importMode);
                },
              },
              busyImport ? "Importing..." : "Import as New Antigravity Conversation"
            ),
          ]),
      canUpdateContext
        ? el(
            "button",
            {
              type: "button",
              className: "si-btn",
              onClick: () => {
                void attachContextToNextMessage(detail);
              },
            },
            "Attach Context to Next Message"
          )
        : null,
      el(
        "button",
        {
          type: "button",
          className: "si-btn",
          onClick: () => {
            void copyToClipboard(s.session_id, "Session ID");
          },
        },
        "Copy Session ID"
      )
    ),
    detail.files_mentioned.length > 0
      ? el(
          "div",
          { className: "si-chips-section" },
          el("div", { className: "si-section-label" }, `Files Touched (${detail.files_mentioned.length})`),
          el(
            "div",
            { className: "si-chips" },
            ...detail.files_mentioned.map((filePath) =>
              el(
                "button",
                {
                  type: "button",
                  className: "si-chip",
                  title: `Open ${filePath}`,
                  onClick: () => {
                    void openFileInAuxPane(filePath, s.cwd);
                  },
                },
                filePath
              )
            )
          )
        )
      : null,
    detail.commands_run.length > 0
      ? el(
          "div",
          { className: "si-chips-section" },
          el("div", { className: "si-section-label" }, `Commands Run (${detail.commands_run.length})`),
          el(
            "div",
            { className: "si-chips" },
            ...detail.commands_run.map((cmd) =>
              el(
                "button",
                {
                  type: "button",
                  className: "si-chip",
                  title: "Copy command",
                  onClick: () => {
                    void copyToClipboard(cmd, "command");
                  },
                },
                cmd
              )
            )
          )
        )
      : null,
    detail.warnings.length > 0
      ? el("div", { className: "si-banner warning" }, `Warnings: ${detail.warnings.join(", ")}`)
      : null
  );

  const filterLower = state.historyFilter.trim().toLowerCase();
  const visibleTurns = (detail.turns || []).filter((t) => turnMatchesFilter(t, filterLower));
  const sessionKey = `${s.provider}:${s.session_id}`;
  const turnsWithTools = visibleTurns.filter((t) => (t.tool_events || []).length > 0);
  const allToolsExpanded =
    turnsWithTools.length > 0 &&
    turnsWithTools.every((t) => Boolean(state.openToolDetails[`${sessionKey}:turn:${t.turn_index}`]));

  const filterBar = el(
    "div",
    { className: "si-toolbar" },
    el("input", {
      id: "si-history-filter",
      type: "search",
      className: "si-input",
      placeholder: "Filter loaded turns by message or tool text...",
      value: state.historyFilter,
      ariaLabel: "Filter turns in session",
      onInput: (ev) => {
        state.historyFilter = (ev.target as HTMLInputElement).value;
        render();
      },
    }),
    turnsWithTools.length > 0
      ? el(
          "button",
          {
            type: "button",
            className: "si-btn",
            onClick: () => {
              const nextOpen = !allToolsExpanded;
              for (const t of turnsWithTools) {
                state.openToolDetails[`${sessionKey}:turn:${t.turn_index}`] = nextOpen;
              }
              render();
            },
          },
          allToolsExpanded ? "Collapse All Tools" : "Expand All Tools"
        )
      : null,
    el(
      "span",
      { className: "si-meta-row" },
      `Showing ${visibleTurns.length} of ${detail.turns.length} loaded turns`
    )
  );

  const timeline =
    visibleTurns.length === 0
      ? el("div", { className: "si-empty" }, "No loaded turns match this filter.")
      : el("div", { className: "si-timeline" }, ...visibleTurns.map((t) => renderTurnCard(t, sessionKey)));

  const hasMore = Boolean(detail.has_more_turns || detail.has_more);
  const paginationBar = el(
    "div",
    { className: "si-footer-actions" },
    el("span", { className: "si-meta-row" }, rangeLabel),
    hasMore
      ? el(
          "div",
          { className: "si-actions" },
          el(
            "button",
            {
              type: "button",
              className: "si-btn",
              disabled: Boolean(state.busyKeys["load-next"] || state.busyKeys["load-all"]),
              onClick: () => {
                void loadMoreTurns(false);
              },
            },
            state.busyKeys["load-next"] ? "Loading..." : `Load Next ${PAGE_SIZE} Turns`
          ),
          el(
            "button",
            {
              type: "button",
              className: "si-btn primary",
              disabled: Boolean(state.busyKeys["load-next"] || state.busyKeys["load-all"]),
              onClick: () => {
                void loadMoreTurns(true);
              },
            },
            state.busyKeys["load-all"] ? "Loading All..." : "Load All Turns"
          )
        )
      : null
  );

  return el("div", { className: "si-list" }, headerCard, filterBar, timeline, paginationBar);
}

function renderInlineBar(): HTMLElement {
  const total = state.selectedSession?.total_turns || 0;
  const loaded = state.selectedSession?.turns?.length || 0;
  const label =
    total > 0
      ? `Imported Conversation History (${loaded} of ${total} turns loaded)`
      : "Imported Conversation History";
  return el(
    "header",
    { className: "si-inline-bar" },
    el(
      "div",
      { className: "si-title-row" },
      el("strong", { className: "si-title" }, label),
      el("span", { className: "si-trust-badge" }, state.trustBoundary)
    ),
    el(
      "div",
      { className: "si-badges" },
      el(
        "button",
        {
          type: "button",
          className: "si-btn",
          onClick: () => {
            state.showFullTabsInInline = true;
            render();
          },
        },
        "All Importer Tabs"
      )
    )
  );
}

function render(): void {
  const root = document.getElementById("app");
  if (!root) return;

  const isInlineCompact = state.inlineMode && !state.showFullTabsInInline;
  const mainChildren: ChildNode[] = [];

  if (state.notice) {
    mainChildren.push(
      el(
        "div",
        { className: `si-banner ${state.notice.kind}` },
        el("span", null, state.notice.text),
        el(
          "button",
          {
            type: "button",
            className: "si-btn",
            onClick: clearNotice,
          },
          "Dismiss"
        )
      )
    );
  }

  if (state.warnings.length > 0 && state.tab !== "history") {
    mainChildren.push(
      el("div", { className: "si-banner warning" }, `Warnings: ${state.warnings.join(", ")}`)
    );
  }

  if (state.loading) {
    mainChildren.push(
      el("div", { className: "si-empty" }, state.loadingLabel || "Loading...")
    );
  } else if (isInlineCompact || state.tab === "history") {
    mainChildren.push(renderHistoryTab());
  } else if (state.tab === "imported") {
    mainChildren.push(renderImportedTab());
  } else {
    mainChildren.push(renderDiscoverTab());
  }

  const shell = el(
    "div",
    { className: isInlineCompact ? "si-shell inline" : "si-shell" },
    isInlineCompact ? renderInlineBar() : renderTopbar(),
    el("main", { className: "si-main" }, ...mainChildren)
  );

  const active = document.activeElement as HTMLInputElement | HTMLSelectElement | null;
  const activeId = active?.id || "";
  const selStart = active && "selectionStart" in active ? active.selectionStart : null;
  const selEnd = active && "selectionEnd" in active ? active.selectionEnd : null;

  root.replaceChildren(shell);

  if (activeId) {
    const nextActive = document.getElementById(activeId) as HTMLInputElement | HTMLSelectElement | null;
    if (nextActive) {
      nextActive.focus();
      if (selStart !== null && selEnd !== null && "setSelectionRange" in nextActive) {
        try {
          nextActive.setSelectionRange(selStart, selEnd);
        } catch {
          // Ignore selection restoration for unsupported input types
        }
      }
    }
  }
}

// Register all host event handlers before connecting so no notification is missed.
app.ontoolinput = (params) => {
  const args = params?.arguments as Record<string, unknown> | undefined;
  if (args && typeof args.query === "string") {
    state.discoverQuery = args.query;
  }
  if (args && typeof args.provider === "string" && args.provider) {
    state.discoverProvider = args.provider;
  }
  if (args && typeof args.cwd === "string") {
    const trimmedCwd = args.cwd.trim();
    state.discoverCwd = trimmedCwd;
    if (trimmedCwd) {
      state.discoverAllCwds = false;
    }
  }
  if (!initialResultReceived) {
    state.loading = true;
    state.loadingLabel = "Loading Session Importer...";
    render();
  }
};

app.ontoolresult = (result) => {
  initialResultReceived = true;
  if (result.isError && !result.structuredContent) {
    state.loading = false;
    setNotice("danger", extractResultText(result as { content?: Array<{ type?: string; text?: string }> }) || "Tool execution failed.");
    return;
  }
  if (result.structuredContent) {
    hydrateFromOpenResult(result.structuredContent);
  } else {
    state.loading = false;
    render();
  }
};

app.ontoolcancelled = (params) => {
  state.loading = false;
  const reason = params?.reason ? ` (${params.reason})` : "";
  setNotice("warning", `Session Importer tool call cancelled${reason}.`);
};

app.onhostcontextchanged = (ctx) => {
  applyHostContext(ctx);
};

async function boot(): Promise<void> {
  render();
  try {
    await app.connect();
    applyHostContext(app.getHostContext());
    window.setTimeout(() => {
      if (!initialResultReceived) {
        void fetchInitialOpen(false);
      }
    }, INITIAL_FETCH_TIMEOUT_MS);
  } catch (err) {
    state.loading = false;
    setNotice("danger", `Failed to connect to MCP host: ${errorMessage(err)}`);
  }
}

void boot();
