/**
 * Reflexes plugin for OpenClaw.
 *
 * Registers a `before_tool_call` hook that evaluates each tool call
 * against plain-English Reflexes policies via a policy-steerable
 * evaluator. Supports all four backends of the Python reference —
 * `zentropi` (CoPE, default and safety-tuned), `openai`, `anthropic`,
 * and `gemini` — selected by `evaluators.default.type` in reflexes.yaml.
 *
 * Deny semantics (verified against openclaw 2026.6.11 source,
 * tools-invoke-shared): a `{block: true, blockReason}` result surfaces
 * to the model as a `tool_call_blocked` error whose message is the
 * blockReason — so the policy text placed there drives self-correction,
 * matching the Reflexes self-remediation protocol (SPEC.md §6).
 *
 * Failure mode: fail-open with a warning (SPEC.md §4 normative), same
 * as the Python reference implementation.
 */
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { parse as parseYaml } from "yaml";
import { parseRef, resolveCriteria, type PolicyRef } from "./policy.js";

export { parseRef, resolveCriteria, stripFrontmatter, PolicyNotFound, _internal as policyInternal } from "./policy.js";
export type { PolicyRef } from "./policy.js";

export type ReflexAction = "deny" | "flag" | "log";

export interface ReflexBinding {
  policy: string;
  trigger: string;
  action: ReflexAction;
  criteria: string; // resolved eagerly for file refs; "" until first use for hosted refs
  name: string;
  ref: PolicyRef;
  baseDir?: string;
}

export interface ReflexesConfig {
  bindings: ReflexBinding[];
  evaluator: {
    // "zentropi" (default) | "openai" | "anthropic" | "gemini".
    type: string;
    model?: string;
    // Base URL for zentropi (evaluate() appends /v1/label) and the models
    // base for gemini; the full endpoint URL for openai/anthropic.
    apiBase: string;
    apiKeyEnv: string;
  };
}

// Shape of the `before_tool_call` event OpenClaw passes to the handler
// (verified against openclaw 2026.6.11 `PluginHookBeforeToolCallEvent`). It
// carries only the tool call — NOT the conversation. The user's request is
// obtained via the carry-over from `llm_input` (see rememberRequest /
// recallTaskContext).
export interface BeforeToolCallEvent {
  toolName: string;
  params: Record<string, unknown>;
  toolCallId?: string;
  runId?: string;
  derivedPaths?: readonly string[];
}

export interface BeforeToolCallResult {
  block?: boolean;
  blockReason?: string;
}

export interface Verdict {
  label: "0" | "1";
  confidence?: number;
}

const DEFAULT_CONFIG_PATHS = [
  join(homedir(), ".reflexes", "reflexes.yaml"),
  join(process.cwd(), "reflexes.yaml"),
];

/**
 * A policy IS its criteria: the whole document is sent to the evaluator. A
 * leading YAML frontmatter block, if present, is optional metadata and is
 * stripped. Mirrors reflexes.document.parse (SPEC §3).
 */
export function extractCriteria(markdown: string): string {
  return markdown.replace(/^\s*---\s*\n[\s\S]*?\n---\s*\n/, "").trim();
}

export function loadConfig(configPath?: string): ReflexesConfig | null {
  const candidates = configPath ? [configPath] : DEFAULT_CONFIG_PATHS;
  for (const path of candidates) {
    let raw: string;
    try {
      raw = readFileSync(path, "utf8");
    } catch {
      continue;
    }
    const doc = parseYaml(raw) as Record<string, unknown>;
    const groups = (doc?.reflexes ?? {}) as Record<
      string,
      Array<{ policy: string; trigger: string; action: ReflexAction; name?: string }>
    >;
    const bindings: ReflexBinding[] = [];
    for (const entries of Object.values(groups)) {
      for (const entry of entries ?? []) {
        // Keep the triggers OpenClaw can enforce: before_tool_call (tool hook)
        // and before_response (before_agent_finalize hook). Others are skipped.
        if (entry.trigger !== "before_tool_call" && entry.trigger !== "before_response") continue;
        const ref = parseRef(entry.policy);
        const baseDir = dirname(path);
        // `name` is a required binding field; fall back to the file basename for
        // local file refs (hosted refs have no filename to derive from).
        const name =
          entry.name ??
          (ref.scheme === "file" ? ref.locator.split("/").pop()?.replace(/\.md$/, "") : undefined);
        if (!name) {
          console.warn(`[reflexes] binding for '${entry.policy}' is missing 'name', skipping`);
          continue;
        }
        // Read local files eagerly (fail-fast, no async at load). Hosted refs
        // (zentropi:/github:) resolve lazily on first evaluation + cache — see
        // resolveBindingCriteria.
        let criteria = "";
        if (ref.scheme === "file") {
          const policyPath = resolve(baseDir, ref.locator);
          try {
            criteria = extractCriteria(readFileSync(policyPath, "utf8"));
          } catch {
            console.warn(`[reflexes] policy not readable, skipping: ${policyPath}`);
            continue;
          }
        }
        bindings.push({
          policy: ref.raw,
          trigger: entry.trigger,
          action: entry.action ?? "deny",
          criteria,
          name,
          ref,
          baseDir,
        });
      }
    }
    const evalCfg = (doc?.evaluators as Record<string, Record<string, string>>)?.default ?? {};
    const evalType = (evalCfg.type ?? "zentropi").toLowerCase();
    // Per-type defaults mirror the Python evaluator classes. `apiBase` is the
    // base URL for zentropi (evaluate() appends /v1/label) and the models base
    // for gemini; for openai/anthropic it is the full endpoint URL. `endpoint`
    // is accepted as an alias (the field name the Python reflexes.yaml uses).
    const EVAL_DEFAULTS: Record<string, { apiBase: string; keyEnv: string }> = {
      zentropi: { apiBase: "https://api.zentropi.ai", keyEnv: "ZENTROPI_API_KEY" },
      cope: { apiBase: "https://api.zentropi.ai", keyEnv: "ZENTROPI_API_KEY" },
      openai: { apiBase: "https://api.openai.com/v1/chat/completions", keyEnv: "OPENAI_API_KEY" },
      anthropic: { apiBase: "https://api.anthropic.com/v1/messages", keyEnv: "ANTHROPIC_API_KEY" },
      gemini: { apiBase: "https://generativelanguage.googleapis.com/v1beta/models", keyEnv: "GEMINI_API_KEY" },
    };
    const d = EVAL_DEFAULTS[evalType] ?? EVAL_DEFAULTS.zentropi;
    return {
      bindings,
      evaluator: {
        type: evalType,
        model: evalCfg.model,
        apiBase: evalCfg.api_base ?? evalCfg.endpoint ?? d.apiBase,
        apiKeyEnv: evalCfg.api_key_env ?? d.keyEnv,
      },
    };
  }
  return null;
}

/** Resolve a binding's criteria on demand, memoizing the result. File refs are
 *  already resolved at load; hosted refs (zentropi:/github:) fetch + cache on
 *  first use, then reuse the cached value (so denyReason/reviseInstruction, which
 *  read binding.criteria, see the resolved text). */
export async function resolveBindingCriteria(
  binding: ReflexBinding,
  fetchImpl?: typeof fetch,
): Promise<string> {
  if (binding.criteria) return binding.criteria;
  const criteria = await resolveCriteria(binding.ref, { baseDir: binding.baseDir, fetchImpl });
  binding.criteria = criteria; // memoize
  return criteria;
}

/** Serialize the pending tool call as the SPEC §5 JSON shape — matches the
 *  Python reference and every other host: `{"tool_name": ..., "args": ...}`. */
export function toolCallContent(event: BeforeToolCallEvent): string {
  return JSON.stringify({ tool_name: event.toolName, args: event.params ?? {} });
}

// --- Task-context assembly ------------------------------------------------
// Mirrors the Python reference (reflexes._context.assemble_task_context +
// reflexes.core._assemble_content) byte-for-byte, so the evaluator receives
// identical input on every host. Section labels per SPEC §5.1.

// Per-section char budgets, weighted so a large AGENTS.md can't starve the
// current request (matches reflexes._context).
const PRIMARY_PROMPT_MAX = 4000;
const INITIAL_PROMPT_MAX = 1500;
const STANDING_INSTRUCTIONS_MAX = 1500;

function truncate(text: string, maxChars: number): string {
  return text.length <= maxChars
    ? text
    : text.slice(0, maxChars).trimEnd() + "\n[...truncated]";
}

export interface TaskContextSources {
  currentRequest?: string | null;
  originalTask?: string | null;
  standingInstructions?: string | null;
}

/** Assemble the labeled task-context block (SPEC §5.1); "" if nothing given.
 *  Mirrors reflexes._context.assemble_task_context. */
export function assembleTaskContext(src: TaskContextSources): string {
  const sections: string[] = [];
  const current = src.currentRequest?.trim();
  const original = src.originalTask?.trim();
  const standing = src.standingInstructions?.trim();
  if (current) {
    sections.push(`Current request:\n${truncate(current, PRIMARY_PROMPT_MAX)}`);
  }
  if (original && original !== (current ?? "")) {
    sections.push(`Original task:\n${truncate(original, INITIAL_PROMPT_MAX)}`);
  }
  if (standing) {
    sections.push(`Standing instructions:\n${truncate(standing, STANDING_INSTRUCTIONS_MAX)}`);
  }
  return sections.join("\n\n");
}

/** Frame content + context for the evaluator. Mirrors
 *  reflexes.core._assemble_content byte-for-byte. */
export function assembleContent(
  content: string,
  context: string | null,
  checkType = "Content",
  contextLabel = "User request",
): string {
  if (!context) return content;
  return (
    `Evaluate the ${checkType} below against the policy. ` +
    `The ${contextLabel} is provided for reference — evaluate the ${checkType}, not the ${contextLabel}.\n\n` +
    `${contextLabel}: ${context}\n\n` +
    `${checkType}: ${content}`
  );
}

// --- OpenClaw carry-over -----------------------------------------------------
// The `before_tool_call` event has no conversation. But `llm_input` fires
// earlier in the same run (same `runId`) and carries both the user's request
// (`prompt`) and the assembled system prompt (`systemPrompt`). We stash those
// in OpenClaw's per-run store (`api.runContext`, keyed by `runId`, auto-cleared
// on run end) and read them back in `before_tool_call`. Signatures verified
// against openclaw 2026.6.11 (`OpenClawPluginApi`, `PluginHookLlmInputEvent`,
// `OpenClawPluginRunContextApi`).

/** runContext namespace for the stashed request. */
const REFLEXES_NS = "reflexes/current-request";

interface CapturedRequest {
  prompt: string;
  systemPrompt?: string;
}

/** Subset of OpenClaw's `PluginHookLlmInputEvent` we read. */
export interface LlmInputEvent {
  runId: string;
  prompt: string;
  systemPrompt?: string;
}

/** Subset of OpenClaw's `PluginHookToolContext` (`before_tool_call` ctx arg). */
export interface ToolContext {
  runId?: string;
  sessionKey?: string;
  sessionId?: string;
}

/** Subset of OpenClaw's `PluginHookMessageReceivedEvent` — an inbound message.
 *  The first per session is the "Original task". */
export interface MessageReceivedEvent {
  from?: string;
  content: string;
  sessionKey?: string;
  runId?: string;
}

/** Subset of OpenClaw's `PluginHookSessionEndEvent` (for original-task cleanup). */
export interface SessionEndEvent {
  sessionKey?: string;
  sessionId?: string;
}

/** Subset of OpenClaw's `PluginHookAfterToolCallEvent` (a completed tool call,
 *  observe-only). Used to accumulate the per-run trajectory. */
export interface AfterToolCallEvent {
  toolName: string;
  params?: Record<string, unknown>;
  runId?: string;
  result?: unknown;
  error?: string;
}

/** Subset of OpenClaw's `PluginHookBeforeAgentFinalizeEvent` (the Stop-hook
 *  analog — carries the agent's final reply). */
export interface FinalizeEvent {
  runId?: string;
  sessionId?: string;
  lastAssistantMessage?: string;
  stopHookActive?: boolean;
}

/** OpenClaw's `PluginHookBeforeAgentFinalizeResult`. `action: "revise"` blocks
 *  finalization and re-runs the model with `retry.instruction` (self-correct). */
export interface FinalizeResult {
  action?: "continue" | "revise" | "finalize";
  reason?: string;
  retry?: { instruction: string; maxAttempts?: number };
}

/** Subset of OpenClaw's `PluginHookMessageContext` (outbound). `sessionKey` is
 *  the same canonical value the tool/finalize hooks fire with, so it correlates
 *  to the run that produced this reply. Omitted when no session is resolvable. */
export interface MessageContext {
  sessionKey?: string;
}

/** OpenClaw's `MessagePresentationBlock` — the structured reply block types we
 *  touch. Other block types (buttons, select, divider) pass through unchanged
 *  via the index signature. `context` is "small context/footer text"; `text`
 *  is a normal paragraph (see message-presentation.md). */
export type PresentationBlock =
  | { type: "text"; text: string }
  | { type: "context"; text: string }
  | { type: string; [key: string]: unknown };

/** OpenClaw's `MessagePresentation` — a structured reply. `blocks` is required;
 *  `title`/`tone` are optional and left untouched when present. */
export interface ReplyPresentation {
  title?: string;
  tone?: string;
  blocks: PresentationBlock[];
  [key: string]: unknown;
}

/** Subset of OpenClaw's `PluginHookReplyPayload` (`Omit<ReplyPayload,
 *  "trustedLocalMedia">`). We read `text` + `presentation`; every other field
 *  (mediaUrls, channelData, replyToId, …) is preserved verbatim via the index
 *  signature. */
export interface ReplyPayload {
  text?: string;
  presentation?: ReplyPresentation;
  [key: string]: unknown;
}

/** Subset of OpenClaw's `PluginHookReplyPayloadSendingEvent`. */
export interface ReplyPayloadSendingEvent {
  payload: ReplyPayload;
  sessionKey?: string;
}

/** OpenClaw's `PluginHookReplyPayloadSendingResult`. Returning `payload`
 *  replaces the outbound payload; returning nothing leaves it untouched. */
export interface ReplyPayloadSendingResult {
  payload?: ReplyPayload;
}

interface RunContextApi {
  setRunContext(patch: { runId: string; namespace: string; value?: unknown; unset?: boolean }): boolean;
  getRunContext(params: { runId: string; namespace: string }): unknown;
}

/** Subset of OpenClaw's `PluginNextTurnInjection` — durable context that reaches
 *  the model on its next turn exactly once (deduped by idempotencyKey). */
interface NextTurnInjection {
  sessionKey: string;
  text: string;
  idempotencyKey?: string;
}

/** Subset of `api.session.workflow` — the sanctioned seam for a policy summary
 *  to reach the next model turn. Optional so older gateways / test mocks
 *  without it degrade gracefully (guarded at the call site). */
interface SessionApi {
  workflow?: {
    enqueueNextTurnInjection(injection: NextTurnInjection): Promise<unknown> | unknown;
  };
}

interface HookOpts {
  priority?: number;
  timeoutMs?: number;
}

/** Subset of OpenClaw's `OpenClawPluginApi` the plugin uses. */
export interface OpenClawPluginApi {
  on(hook: "llm_input", handler: (event: LlmInputEvent, ctx: ToolContext) => void, opts?: HookOpts): void;
  on(hook: "message_received", handler: (event: MessageReceivedEvent, ctx: ToolContext) => void, opts?: HookOpts): void;
  on(hook: "session_end", handler: (event: SessionEndEvent, ctx: ToolContext) => void, opts?: HookOpts): void;
  on(hook: "after_tool_call", handler: (event: AfterToolCallEvent, ctx: ToolContext) => void, opts?: HookOpts): void;
  on(
    hook: "reply_payload_sending",
    handler: (
      event: ReplyPayloadSendingEvent,
      ctx: MessageContext,
    ) => ReplyPayloadSendingResult | undefined,
    opts?: HookOpts,
  ): void;
  on(
    hook: "before_tool_call",
    handler: (
      event: BeforeToolCallEvent,
      ctx: ToolContext,
    ) => Promise<BeforeToolCallResult | undefined> | BeforeToolCallResult | undefined,
    opts?: HookOpts,
  ): void;
  on(
    hook: "before_agent_finalize",
    handler: (
      event: FinalizeEvent,
      ctx: ToolContext,
    ) => Promise<FinalizeResult | undefined> | FinalizeResult | undefined,
    opts?: HookOpts,
  ): void;
  runContext: RunContextApi;
  session?: SessionApi;
}

/** Capture hook: stash this run's request + system prompt (best-effort). */
export function rememberRequest(api: OpenClawPluginApi, event: LlmInputEvent): void {
  if (!event.runId || typeof event.prompt !== "string") return;
  const value: CapturedRequest = { prompt: event.prompt, systemPrompt: event.systemPrompt };
  try {
    api.runContext.setRunContext({ runId: event.runId, namespace: REFLEXES_NS, value });
  } catch {
    // best-effort; enforcement still runs with the disk standing-instructions fallback
  }
}

// --- Original task (session-scoped) -----------------------------------------
// The first inbound message of a session is the "Original task". Unlike the
// per-run request it persists across turns, so it lives in a module map keyed
// by sessionKey — captured from message_received (robust to compaction, which
// can drop the first message from historyMessages) and evicted on session_end.

const originalTaskBySession = new Map<string, string>();
const ORIGINAL_TASK_MAX_SESSIONS = 500; // backstop if session_end never fires

/** Capture hook: remember the first inbound message of a session. */
export function rememberOriginalTask(event: MessageReceivedEvent): void {
  const key = event.sessionKey;
  const content = typeof event.content === "string" ? event.content.trim() : "";
  if (!key || !content || originalTaskBySession.has(key)) return;
  if (originalTaskBySession.size >= ORIGINAL_TASK_MAX_SESSIONS) {
    const oldest = originalTaskBySession.keys().next().value; // Map is insertion-ordered
    if (oldest !== undefined) originalTaskBySession.delete(oldest);
  }
  originalTaskBySession.set(key, content);
}

/** Cleanup hook: drop a session's original task when it ends. */
// --- Pending fail-open upgrade notice (session-scoped) ----------------------
// When a policy can't be evaluated because of a sustained rate limit or a
// subscriber-only capability, the check fails open. OpenClaw's context/prompt
// channels are model-facing only, so to make the *user* aware we stash a
// one-line notice keyed by sessionKey and append it to the next outbound reply
// (message_sending hook), once per session.
interface UpgradeNoticeState {
  notice: string;
  surfaced: boolean;
}
const upgradeNoticeBySession = new Map<string, UpgradeNoticeState>();
const UPGRADE_NOTICE_MAX_SESSIONS = 500; // backstop if session_end never fires

/** Record a fail-open upgrade notice for this session (first one wins — once
 *  per session). No-op without a sessionKey (nothing to correlate on). */
export function rememberUpgradeNotice(sessionKey: string | undefined, notice: string): void {
  if (!sessionKey || upgradeNoticeBySession.has(sessionKey)) return;
  if (upgradeNoticeBySession.size >= UPGRADE_NOTICE_MAX_SESSIONS) {
    const oldest = upgradeNoticeBySession.keys().next().value; // insertion-ordered
    if (oldest !== undefined) upgradeNoticeBySession.delete(oldest);
  }
  upgradeNoticeBySession.set(sessionKey, { notice, surfaced: false });
}

/** Return this session's pending notice the first time it's read, then mark it
 *  surfaced so it lands on exactly one reply. */
export function takeUpgradeNotice(sessionKey: string | undefined): string | null {
  if (!sessionKey) return null;
  const state = upgradeNoticeBySession.get(sessionKey);
  if (!state || state.surfaced) return null;
  state.surfaced = true;
  return state.notice;
}

/** Inject the fail-open upgrade notice into the model's NEXT turn, so the agent
 *  itself learns enforcement was degraded — independent of any outbound channel
 *  (unlike the user-facing reply footer). Uses OpenClaw's next-turn-injection
 *  seam, deduped once per session by idempotencyKey. Fire-and-forget and fully
 *  guarded: an injection failure (or a gateway without the API) must never
 *  break the agent loop. */
/** Enqueue model-visible text for the next turn (OpenClaw's integrity-safe
 *  agent channel — never touches the current reply). Advisory; never throws. */
export function enqueueAgentText(
  api: OpenClawPluginApi,
  sessionKey: string | undefined,
  text: string,
  idempotencyKey: string,
): void {
  if (!sessionKey) return;
  try {
    const enqueue = api?.session?.workflow?.enqueueNextTurnInjection;
    if (typeof enqueue !== "function") return;
    const result = enqueue.call(api.session!.workflow, { sessionKey, text, idempotencyKey });
    // Swallow async rejection — advisory only.
    if (result && typeof (result as Promise<unknown>).then === "function") {
      (result as Promise<unknown>).then(undefined, () => {});
    }
  } catch {
    // Never break the agent on an injection failure.
  }
}

export function enqueueAgentNotice(
  api: OpenClawPluginApi,
  sessionKey: string | undefined,
  upgrade: UpgradeRequired,
): void {
  enqueueAgentText(api, sessionKey, upgrade.agentNotice, `reflexes-upgrade:${sessionKey}`);
}

/** Agent-facing note for a `flag` match — surfaced via the next-turn injection
 *  so `flag` is observable to the model (not just the operator log), matching
 *  the `additionalContext` flag notes on Claude Code / Codex. */
export function flagNote(binding: ReflexBinding, verdict: Verdict, subject: string): string {
  const conf = verdict.confidence !== undefined ? ` (confidence ${verdict.confidence})` : "";
  return `[Reflexes] The "${binding.name}" policy flagged your ${subject}${conf}. It was allowed, not blocked — take the flag into account going forward.`;
}

/** Outbound hook: surface this session's pending fail-open upgrade notice as a
 *  distinct `context` (footer) block on the reply the user sees, once per
 *  session. This is the one reliable user-visible surface on OpenClaw —
 *  context/prompt hooks are model-facing only (see FRAMEWORKS.md OpenClaw
 *  notes).
 *
 *  The model's response is never mutated or dropped:
 *   - When the reply already carries a presentation, we append the notice as a
 *     trailing context block and copy every existing block/field unchanged.
 *   - When the reply is plain text, we wrap that text VERBATIM in a `text`
 *     block and add the context block after it. The model's words become the
 *     single source of truth (the presentation), so we drop the now-redundant
 *     top-level `text` to avoid double-rendering on channels that show both
 *     (e.g. Teams); core regenerates plain-channel fallback text from these
 *     blocks (message-presentation.md degradation rules: `text` blocks render
 *     as paragraphs, `context` blocks as compact lines).
 *
 *  Returns a NEW payload object; the input event.payload is never mutated. */
export function appendUpgradeNotice(
  event: ReplyPayloadSendingEvent,
  ctx: MessageContext,
): ReplyPayloadSendingResult | undefined {
  const notice = takeUpgradeNotice(ctx?.sessionKey ?? event?.sessionKey);
  if (!notice) return undefined;
  const payload = event?.payload;
  if (!payload || typeof payload !== "object") return undefined;

  const contextBlock: PresentationBlock = { type: "context", text: notice };
  const existing = payload.presentation;

  // Case 1: reply already has a presentation — append, copying everything else.
  if (existing && Array.isArray(existing.blocks)) {
    return {
      payload: {
        ...payload,
        presentation: { ...existing, blocks: [...existing.blocks, contextBlock] },
      },
    };
  }

  // Case 2: plain-text reply — wrap the model's text verbatim, then the notice.
  const modelText = typeof payload.text === "string" ? payload.text : "";
  if (!modelText) {
    // No text and no presentation (e.g. media-only): attach a context-only
    // presentation so the notice still surfaces; other fields pass through.
    return { payload: { ...payload, presentation: { blocks: [contextBlock] } } };
  }
  const { text: _redundant, ...rest } = payload;
  return {
    payload: {
      ...rest,
      presentation: { blocks: [{ type: "text", text: modelText }, contextBlock] },
    },
  };
}

export function forgetSession(event: SessionEndEvent): void {
  if (event.sessionKey) {
    originalTaskBySession.delete(event.sessionKey);
    upgradeNoticeBySession.delete(event.sessionKey);
  }
}

function recallOriginalTask(ctx: ToolContext): string | null {
  const key = ctx?.sessionKey;
  return key ? originalTaskBySession.get(key) ?? null : null;
}

/** Read the run's stashed request + the session's original task as task-context
 *  sources, or undefined if neither is available. */
export function recallTaskContext(api: OpenClawPluginApi, ctx: ToolContext): TaskContextSources | undefined {
  let captured: CapturedRequest | undefined;
  const runId = ctx?.runId;
  if (runId) {
    try {
      captured = api.runContext.getRunContext({ runId, namespace: REFLEXES_NS }) as CapturedRequest | undefined;
    } catch {
      captured = undefined;
    }
  }
  const currentRequest = captured && typeof captured.prompt === "string" ? captured.prompt : null;
  const standingInstructions = captured?.systemPrompt ?? null;
  const originalTask = recallOriginalTask(ctx);
  if (!currentRequest && !standingInstructions && !originalTask) return undefined;
  const out: TaskContextSources = { currentRequest, standingInstructions };
  if (originalTask) out.originalTask = originalTask;
  return out;
}

// --- Trajectory ("Actions this turn") ---------------------------------------
// before_tool_call/before_agent_finalize don't carry what the agent DID this
// turn. Accumulate it from after_tool_call into the same per-run store, and
// append it as the "Actions this turn:" section for before_response policies
// (Early-Stopping / Laziness-Detection) — matching the Claude Code trajectory.

const REFLEXES_TRAJECTORY_NS = "reflexes/trajectory";
const TRAJECTORY_MAX_ENTRIES = 50; // cap so a long turn can't bloat the context
const TRAJECTORY_ARG_MAX = 120;

/** Pull a readable primary argument from a tool call's params. */
function primaryArg(params: Record<string, unknown> | undefined): string | null {
  if (!params) return null;
  for (const key of ["command", "file_path", "path", "query", "url", "pattern"]) {
    const v = params[key];
    if (typeof v === "string" && v.trim()) return v;
  }
  for (const v of Object.values(params)) {
    if (typeof v === "string" && v.trim()) return v;
  }
  return null;
}

/** Render one completed tool call as a trajectory line. */
export function renderAction(event: AfterToolCallEvent): string {
  const arg = primaryArg(event.params);
  const head = arg ? `${event.toolName}: ${truncate(arg, TRAJECTORY_ARG_MAX)}` : event.toolName;
  return event.error ? `${head} ✗ ${truncate(String(event.error), TRAJECTORY_ARG_MAX)}` : `${head} ✓`;
}

/** Capture hook: append this completed tool call to the run's trajectory. */
export function rememberAction(api: OpenClawPluginApi, event: AfterToolCallEvent, ctx: ToolContext): void {
  const runId = event.runId ?? ctx?.runId;
  if (!runId) return;
  try {
    const prev = api.runContext.getRunContext({ runId, namespace: REFLEXES_TRAJECTORY_NS });
    const lines = Array.isArray(prev) ? (prev as string[]) : [];
    lines.push(renderAction(event));
    const capped = lines.slice(-TRAJECTORY_MAX_ENTRIES);
    api.runContext.setRunContext({ runId, namespace: REFLEXES_TRAJECTORY_NS, value: capped });
  } catch {
    // best-effort; before_response still runs without the trajectory
  }
}

/** Read the run's trajectory back as an "Actions this turn:" block, or null. */
export function recallTrajectory(api: OpenClawPluginApi, ctx: ToolContext): string | null {
  const runId = ctx?.runId;
  if (!runId) return null;
  let lines: string[] = [];
  try {
    const stored = api.runContext.getRunContext({ runId, namespace: REFLEXES_TRAJECTORY_NS });
    if (Array.isArray(stored)) lines = stored.filter((l): l is string => typeof l === "string");
  } catch {
    return null;
  }
  if (lines.length === 0) return null;
  return "Actions this turn:\n" + lines.map((l) => `  ${l}`).join("\n");
}

/** Resolve project standing instructions from disk (AGENTS.md, else CLAUDE.md)
 *  at `cwd`. Independent of the event, so it works even when OpenClaw doesn't
 *  surface the conversation. null if none present. */
export function resolveStandingInstructions(cwd: string): string | null {
  for (const name of ["AGENTS.md", "CLAUDE.md"]) {
    try {
      const body = readFileSync(join(cwd, name), "utf8").trim();
      if (body) return body;
    } catch {
      // not present / unreadable — try the next
    }
  }
  return null;
}

export interface EvaluateOptions {
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  // Pre-resolved task context (from the llm_input carry-over). When absent,
  // standing instructions still resolve from disk.
  taskContext?: TaskContextSources;
  cwd?: string;
  // Pre-rendered "Actions this turn:" block (before_response only).
  trajectory?: string | null;
  // Canonical session key, so a fail-open upgrade condition can stash a
  // user-facing notice keyed by session (surfaced via reply_payload_sending).
  sessionKey?: string;
  // Called on a fail-open tier/capability upgrade so the caller can inform the
  // agent (next-turn injection). Independent of the user-facing reply footer.
  onFailOpenUpgrade?: (upgrade: UpgradeRequired) => void;
  // Called on a `flag` match so the caller can surface it to the agent
  // (next-turn injection) — makes `flag` observable to the model, not just logs.
  onFlag?: (note: string) => void;
}

// --- Fail-open upgrade signal ------------------------------------------------
// Mirrors the Python evaluator's _synthesize_upgrade. The deployed API returns
// plain error bodies (no tier envelope), so we synthesize the upgrade
// condition from the status code: a sustained 429 (rate limit) or a 403
// (subscriber-only capability). Unlike a generic failure, this carries a
// user-facing notice the message_sending hook appends to the reply — OpenClaw's
// context/prompt channels are model-facing only, so the reply is the only
// reliable way to tell the human enforcement was degraded.
const UPGRADE_URL = "https://zentropi.ai/subscription";
const SUPPORT_EMAIL = "info@zentropi.ai";
const COPE_MODEL_CARD = "https://huggingface.co/zentropi-ai/cope-b-a4b";

// Transient server conditions worth an immediate retry (mirrors the Python
// evaluator's RETRYABLE_STATUS_CODES + RETRY_DELAYS). A 429 only becomes an
// upgrade signal if it survives every retry — a sustained limit, not a blip.
const RETRYABLE_STATUS = new Set([429, 500, 502, 503, 529]);
const RETRY_DELAYS_MS = [0, 100, 1000];
const MAX_RETRIES = 3;

export class UpgradeRequired extends Error {
  readonly kind: "rate_limit" | "subscription";
  /** One-line notice shown to the user (appended to the outbound reply). */
  readonly userNotice: string;
  /** Notice injected into the model's next turn, so the agent itself learns
   *  enforcement was degraded even when there's no outbound channel. */
  readonly agentNotice: string;
  constructor(kind: "rate_limit" | "subscription", userNotice: string, agentNotice: string) {
    super(userNotice);
    this.name = "UpgradeRequired";
    this.kind = kind;
    this.userNotice = userNotice;
    this.agentNotice = agentNotice;
  }
}

function synthesizeUpgrade(status: number): UpgradeRequired {
  if (status === 403) {
    return new UpgradeRequired(
      "subscription",
      `⚠️ Reflexes: a policy couldn't be checked — this evaluation requires a ` +
        `Zentropi subscription, so enforcement is degraded for this turn. ` +
        `Subscribe at ${UPGRADE_URL} (or email ${SUPPORT_EMAIL}).`,
      `[REFLEXES] A policy check on your recent action could not run — the ` +
        `evaluation requires a Zentropi subscription. The action proceeded ` +
        `WITHOUT that check (fail-open), so enforcement is degraded this turn. ` +
        `Proceed cautiously; if relevant, tell the user enforcement is ` +
        `temporarily degraded (they can subscribe at ${UPGRADE_URL}).`,
    );
  }
  return new UpgradeRequired(
    "rate_limit",
    `⚠️ Reflexes: a policy couldn't be checked — the Zentropi rate limit was ` +
      `reached, so enforcement is degraded for this turn. Raise the limit at ` +
      `${UPGRADE_URL}, or self-host the open CoPE model for no rate limits at ` +
      `all (${COPE_MODEL_CARD}).`,
    `[REFLEXES] A policy check on your recent action could not run — the ` +
      `Zentropi evaluator is rate-limited. The action proceeded WITHOUT that ` +
      `check (fail-open), so enforcement is degraded this turn. Proceed ` +
      `cautiously; if relevant, tell the user enforcement is temporarily ` +
      `degraded (they can raise limits at ${UPGRADE_URL} or self-host the open ` +
      `CoPE model: ${COPE_MODEL_CARD}).`,
  );
}

const retryDelay = (attempt: number): number =>
  RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)];

const sleep = (ms: number): Promise<void> =>
  ms > 0 ? new Promise((r) => setTimeout(r, ms)) : Promise.resolve();

/** Dispatch to the configured evaluator backend. Mirrors the Python
 *  reference's four evaluator classes (reflexes/evaluators/*.py) — same
 *  endpoints, prompts, and label parsing — so a reflexes.yaml behaves
 *  identically whether evaluated by the Python integration or this plugin. */
export async function evaluate(
  config: ReflexesConfig,
  criteria: string,
  content: string,
  opts: EvaluateOptions = {},
): Promise<Verdict> {
  const type = (config.evaluator.type ?? "zentropi").toLowerCase();
  if (type === "openai") return evaluateOpenAICompatible(config, criteria, content, opts);
  if (type === "anthropic") return evaluateAnthropic(config, criteria, content, opts);
  if (type === "gemini") return evaluateGemini(config, criteria, content, opts);
  return evaluateZentropi(config, criteria, content, opts); // zentropi / cope / default
}

async function evaluateZentropi(
  config: ReflexesConfig,
  criteria: string,
  content: string,
  opts: EvaluateOptions = {},
): Promise<Verdict> {
  const doFetch = opts.fetchImpl ?? fetch;
  const apiKey = process.env[config.evaluator.apiKeyEnv];
  let lastStatus: number | undefined;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? 10_000);
    let res: Awaited<ReturnType<typeof doFetch>>;
    try {
      res = await doFetch(`${config.evaluator.apiBase}/v1/label`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
        },
        body: JSON.stringify({
          model: config.evaluator.model ?? "cope-latest",
          criteria_text: criteria,
          content_text: content,
        }),
        signal: controller.signal,
      });
    } catch (err) {
      clearTimeout(timer);
      // Network/timeout error — retry if attempts remain, else propagate as a
      // generic failure (fail-open, not an upgrade signal).
      if (attempt < MAX_RETRIES) {
        await sleep(retryDelay(attempt));
        continue;
      }
      throw err;
    }
    clearTimeout(timer);
    if (res.ok) {
      const data = (await res.json()) as { label: string; confidence?: number };
      return { label: data.label === "1" ? "1" : "0", confidence: data.confidence };
    }
    // A 403 (subscriber-only capability) can't succeed on retry — surface now.
    if (res.status === 403) throw synthesizeUpgrade(403);
    lastStatus = res.status;
    if (RETRYABLE_STATUS.has(res.status) && attempt < MAX_RETRIES) {
      await sleep(retryDelay(attempt));
      continue;
    }
    break; // non-retryable status, or retries exhausted
  }
  // A 429 that survived every retry is sustained throttling, not a blip.
  if (lastStatus === 429) throw synthesizeUpgrade(429);
  throw new Error(`evaluator HTTP ${lastStatus ?? "unknown"}`);
}

// --- LLM-as-judge evaluators (openai / anthropic / gemini) -----------------
// Byte-for-byte mirror of the Python SYSTEM_PROMPT + `POLICY:/CONTENT:` framing
// (reflexes/evaluators/{openai_compatible,anthropic,gemini}.py). Unlike CoPE
// these are general chat models prompted to return a JSON verdict; they are
// slower/looser and NOT safety-calibrated the way CoPE is — offered for parity
// with the Python integration and self-hosted/OpenAI-compatible setups.
const LLM_SYSTEM_PROMPT =
  "You are a content classifier. You evaluate whether content matches a policy.\n\n" +
  "You will receive a POLICY and CONTENT. Determine whether the content matches " +
  "the policy criteria.\n\n" +
  "Respond with ONLY a JSON object, no other text:\n" +
  '{"label": 0 or 1, "confidence": 0.0 to 1.0}\n\n' +
  "label=1 means the content matches the policy criteria.\n" +
  "label=0 means it does not.";

function llmUserMessage(criteria: string, content: string): string {
  return `POLICY:\n${criteria}\n\nCONTENT:\n${content}`;
}

/** Thrown for a transient failure worth a retry — a retryable HTTP status or an
 *  unparseable model body. Anything else is a hard error surfaced immediately. */
class RetryableEvalError extends Error {}

/** Retry wrapper for the LLM-judge backends — mirrors evaluateZentropi + the
 *  Python evaluators: retry a RetryableEvalError (transient HTTP / bad body),
 *  then fail LOUD (throw → the host fails open with a visible notice). Never
 *  returns a silent label-0. Happy path does zero retries. */
async function withEvalRetry(doAttempt: () => Promise<Verdict>): Promise<Verdict> {
  let last: unknown;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      return await doAttempt();
    } catch (err) {
      last = err;
      if (err instanceof RetryableEvalError && attempt < MAX_RETRIES) {
        await sleep(retryDelay(attempt));
        continue;
      }
      throw err;
    }
  }
  throw last as Error; // unreachable
}

/** Parse an LLM's JSON verdict, tolerating markdown code fences. On a parse
 *  failure, throw a RetryableEvalError (retried, then failed loud) rather than
 *  returning a silent label-0 — a silent 0 on a deny policy is a silent miss. */
function parseLlmVerdict(raw: string): Verdict {
  let text = raw.trim();
  if (text.startsWith("```")) {
    const nl = text.indexOf("\n");
    const end = text.lastIndexOf("```");
    if (nl !== -1 && end > nl) text = text.slice(nl + 1, end).trim();
  }
  let result: { label?: unknown; confidence?: unknown };
  try {
    result = JSON.parse(text) as { label?: unknown; confidence?: unknown };
  } catch {
    throw new RetryableEvalError(`unparseable evaluator body: ${text.slice(0, 200)}`);
  }
  return {
    label: String(result.label) === "1" ? "1" : "0",
    confidence: typeof result.confidence === "number" ? result.confidence : Number(result.confidence) || 0,
  };
}

async function evaluateOpenAICompatible(
  config: ReflexesConfig,
  criteria: string,
  content: string,
  opts: EvaluateOptions = {},
): Promise<Verdict> {
  const doFetch = opts.fetchImpl ?? fetch;
  const apiKey = process.env[config.evaluator.apiKeyEnv];
  const endpoint = config.evaluator.apiBase; // full chat/completions URL
  const model = config.evaluator.model ?? "gpt-4o-mini";
  const headers = {
    "Content-Type": "application/json",
    ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
  };
  const base = {
    model,
    messages: [
      { role: "system", content: LLM_SYSTEM_PROMPT },
      { role: "user", content: llmUserMessage(criteria, content) },
    ],
  };
  const post = (body: unknown) =>
    doFetch(endpoint, { method: "POST", headers, body: JSON.stringify(body) });

  return withEvalRetry(async () => {
    // Widely-supported form first; newer reasoning models reject
    // max_tokens/temperature and require max_completion_tokens (mirror Python).
    let res = await post({ ...base, temperature: 0, max_tokens: 512 });
    if (res.status === 400) {
      const errText = await res.text();
      if (errText.includes("max_completion_tokens")) {
        res = await post({ ...base, max_completion_tokens: 512 });
      } else {
        throw new Error(`openai evaluator HTTP 400: ${errText.slice(0, 200)}`);
      }
    }
    if (RETRYABLE_STATUS.has(res.status)) throw new RetryableEvalError(`openai evaluator HTTP ${res.status}`);
    if (!res.ok) throw new Error(`openai evaluator HTTP ${res.status}`);
    const data = (await res.json()) as { choices: Array<{ message: { content: string } }> };
    return parseLlmVerdict(data.choices[0].message.content);
  });
}

async function evaluateAnthropic(
  config: ReflexesConfig,
  criteria: string,
  content: string,
  opts: EvaluateOptions = {},
): Promise<Verdict> {
  const doFetch = opts.fetchImpl ?? fetch;
  const apiKey = process.env[config.evaluator.apiKeyEnv];
  const endpoint = config.evaluator.apiBase; // full /v1/messages URL
  const model = config.evaluator.model ?? "claude-haiku-4-5-20251001";
  return withEvalRetry(async () => {
    const res = await doFetch(endpoint, {
      method: "POST",
      headers: {
        "x-api-key": apiKey ?? "",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model,
        system: LLM_SYSTEM_PROMPT,
        messages: [{ role: "user", content: llmUserMessage(criteria, content) }],
        max_tokens: 512,
        temperature: 0,
      }),
    });
    if (RETRYABLE_STATUS.has(res.status)) throw new RetryableEvalError(`anthropic evaluator HTTP ${res.status}`);
    if (!res.ok) throw new Error(`anthropic evaluator HTTP ${res.status}`);
    const data = (await res.json()) as { content: Array<{ text: string }> };
    return parseLlmVerdict(data.content[0].text);
  });
}

async function evaluateGemini(
  config: ReflexesConfig,
  criteria: string,
  content: string,
  opts: EvaluateOptions = {},
): Promise<Verdict> {
  const doFetch = opts.fetchImpl ?? fetch;
  const apiKey = process.env[config.evaluator.apiKeyEnv] ?? process.env.GOOGLE_API_KEY;
  const model = config.evaluator.model ?? "gemini-2.5-flash";
  // apiBase is the models base; build `{base}/{model}:generateContent` (mirror Python).
  const endpoint = `${config.evaluator.apiBase}/${model}:generateContent?key=${apiKey ?? ""}`;
  return withEvalRetry(async () => {
    const res = await doFetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system_instruction: { parts: [{ text: LLM_SYSTEM_PROMPT }] },
        contents: [{ parts: [{ text: llmUserMessage(criteria, content) }] }],
        generationConfig: { temperature: 0, maxOutputTokens: 512 },
      }),
    });
    if (RETRYABLE_STATUS.has(res.status)) throw new RetryableEvalError(`gemini evaluator HTTP ${res.status}`);
    if (!res.ok) throw new Error(`gemini evaluator HTTP ${res.status}`);
    const data = (await res.json()) as {
      candidates: Array<{ content: { parts: Array<{ text: string }> } }>;
    };
    return parseLlmVerdict(data.candidates[0].content.parts[0].text);
  });
}

export function denyReason(binding: ReflexBinding): string {
  return (
    `[REFLEXES POLICY CHECK] This tool call was blocked: it matches the ` +
    `"${binding.name}" policy. Read the policy below and choose a compliant ` +
    `alternative that still accomplishes the user's goal.\n\n` +
    `Policy:\n---\n${binding.criteria}\n---`
  );
}

/** Self-correction instruction for a denied response (before_agent_finalize
 *  `retry.instruction`). The harness re-runs the model with this prepended. */
export function reviseInstruction(binding: ReflexBinding): string {
  return (
    `[REFLEXES POLICY CHECK] Your draft final answer matches the ` +
    `"${binding.name}" policy and must be revised. Read the policy below and ` +
    `rewrite your final answer to comply — without repeating completed work ` +
    `or rerunning tools.\n\n` +
    `Policy:\n---\n${binding.criteria}\n---`
  );
}

/** Default cap on concurrent evaluator calls per trigger fire. */
export const DEFAULT_MAX_CONCURRENT = 10;

function maxConcurrent(): number {
  const raw = process.env.REFLEXES_MAX_CONCURRENT;
  const n = raw ? parseInt(raw, 10) : NaN;
  return Number.isInteger(n) && n > 0 ? n : DEFAULT_MAX_CONCURRENT;
}

/** Map over items with at most `limit` running at once; preserves order. */
async function mapWithConcurrency<T, R>(
  items: T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let next = 0;
  async function worker(): Promise<void> {
    while (next < items.length) {
      const i = next++;
      results[i] = await fn(items[i], i);
    }
  }
  const workers = Array.from({ length: Math.min(limit, items.length) }, worker);
  await Promise.all(workers);
  return results;
}

/**
 * Core hook logic, framework-agnostic and fully unit-testable.
 * Returns undefined to allow, or a block result per OpenClaw's
 * BeforeToolCallResult contract.
 */
export async function checkToolCall(
  event: BeforeToolCallEvent,
  config: ReflexesConfig | null,
  opts: EvaluateOptions = {},
): Promise<BeforeToolCallResult | undefined> {
  if (!config) return undefined;
  const bindings = config.bindings.filter((b) => b.trigger === "before_tool_call");
  if (bindings.length === 0) return undefined;
  // Assemble task context so policies that reason about the user's request
  // (e.g. Unconfirmed-Destructive-Action, Constraint-Violation) get the same
  // signal they do on other hosts. The current request + system prompt come
  // from the llm_input carry-over (opts.taskContext); standing instructions
  // fall back to AGENTS.md / CLAUDE.md on disk when the carry-over lacks them.
  const currentRequest = opts.taskContext?.currentRequest ?? null;
  const originalTask = opts.taskContext?.originalTask ?? null;
  const standingInstructions =
    opts.taskContext?.standingInstructions ?? resolveStandingInstructions(opts.cwd ?? process.cwd());
  const context = assembleTaskContext({ currentRequest, originalTask, standingInstructions }) || null;
  const content = assembleContent(toolCallContent(event), context, "Tool call", "Task context");
  // Evaluate all bound policies in parallel (cap: REFLEXES_MAX_CONCURRENT,
  // default 10), then apply results in binding order — first deny wins.
  const verdicts = await mapWithConcurrency(
    bindings,
    maxConcurrent(),
    async (binding): Promise<Verdict | null> => {
      let criteria: string;
      try {
        criteria = await resolveBindingCriteria(binding, opts.fetchImpl);
      } catch (err) {
        // Policy unavailable (missing/unreachable hosted policy) → fail-open,
        // skip this binding. policy_cache already falls back to a stale copy on
        // a transient refetch failure, so this only fires on a cold miss.
        console.warn(`[reflexes] policy "${binding.name}" unavailable, skipping (fail-open): ${String(err)}`);
        return null;
      }
      try {
        return await evaluate(config, criteria, content, opts);
      } catch (err) {
        // Fail-open (SPEC §4): never break the agent on evaluator failure.
        if (err instanceof UpgradeRequired) {
          // Two independent audiences: the user (reply_payload_sending footer,
          // needs an outbound channel) and the agent (next-turn injection,
          // always) — so the model learns enforcement was degraded even with no
          // channel.
          rememberUpgradeNotice(opts.sessionKey, err.userNotice);
          opts.onFailOpenUpgrade?.(err);
        }
        console.warn(`[reflexes] evaluator error (fail-open): ${String(err)}`);
        return null;
      }
    },
  );
  for (let i = 0; i < bindings.length; i++) {
    const binding = bindings[i];
    const verdict = verdicts[i];
    if (!verdict || verdict.label !== "1") continue;
    if (binding.action === "deny") {
      return { block: true, blockReason: denyReason(binding) };
    }
    // flag / log: surface without blocking.
    console.warn(
      `[reflexes] ${binding.action}: tool call ${event.toolName} matched policy ${binding.name}` +
        (verdict.confidence !== undefined ? ` (confidence ${verdict.confidence})` : ""),
    );
    if (binding.action === "flag") opts.onFlag?.(flagNote(binding, verdict, "tool call"));
  }
  return undefined;
}

/**
 * Core `before_response` logic: evaluate the agent's final reply against the
 * bound `before_response` policies. On deny, return `{ action: "revise" }` so
 * OpenClaw blocks finalization and re-runs the model with the policy text as
 * the revision instruction (the before_agent_finalize self-correct loop).
 */
export async function checkResponse(
  event: FinalizeEvent,
  config: ReflexesConfig | null,
  opts: EvaluateOptions = {},
): Promise<FinalizeResult | undefined> {
  if (!config) return undefined;
  const bindings = config.bindings.filter((b) => b.trigger === "before_response");
  if (bindings.length === 0) return undefined;
  const response = event.lastAssistantMessage;
  if (typeof response !== "string" || !response.trim()) return undefined; // nothing to evaluate
  const currentRequest = opts.taskContext?.currentRequest ?? null;
  const originalTask = opts.taskContext?.originalTask ?? null;
  const standingInstructions =
    opts.taskContext?.standingInstructions ?? resolveStandingInstructions(opts.cwd ?? process.cwd());
  // Append the trajectory ("Actions this turn:") after the task context, as
  // supporting context for what the response claims to have done.
  const context =
    [assembleTaskContext({ currentRequest, originalTask, standingInstructions }), opts.trajectory]
      .filter((s): s is string => Boolean(s && s.trim()))
      .join("\n\n") || null;
  const content = assembleContent(response, context, "Agent response", "Task context");
  const verdicts = await mapWithConcurrency(
    bindings,
    maxConcurrent(),
    async (binding): Promise<Verdict | null> => {
      let criteria: string;
      try {
        criteria = await resolveBindingCriteria(binding, opts.fetchImpl);
      } catch (err) {
        // Policy unavailable (missing/unreachable hosted policy) → fail-open,
        // skip this binding. policy_cache already falls back to a stale copy on
        // a transient refetch failure, so this only fires on a cold miss.
        console.warn(`[reflexes] policy "${binding.name}" unavailable, skipping (fail-open): ${String(err)}`);
        return null;
      }
      try {
        return await evaluate(config, criteria, content, opts);
      } catch (err) {
        if (err instanceof UpgradeRequired) {
          rememberUpgradeNotice(opts.sessionKey, err.userNotice);
          opts.onFailOpenUpgrade?.(err);
        }
        console.warn(`[reflexes] evaluator error (fail-open): ${String(err)}`);
        return null;
      }
    },
  );
  for (let i = 0; i < bindings.length; i++) {
    const binding = bindings[i];
    const verdict = verdicts[i];
    if (!verdict || verdict.label !== "1") continue;
    if (binding.action === "deny") {
      return {
        action: "revise",
        reason: `reflexes: ${binding.name}`,
        retry: { instruction: reviseInstruction(binding) },
      };
    }
    // flag / log: surface without blocking (return nothing = accept finalize).
    console.warn(
      `[reflexes] ${binding.action}: response matched policy ${binding.name}` +
        (verdict.confidence !== undefined ? ` (confidence ${verdict.confidence})` : ""),
    );
    if (binding.action === "flag") opts.onFlag?.(flagNote(binding, verdict, "response"));
  }
  return undefined;
}

/** OpenClaw plugin entry. Config is loaded once at registration. Capture hooks
 *  (`llm_input`, `message_received`, `session_end`, `after_tool_call`) build the
 *  task context + trajectory; `before_tool_call` and `before_agent_finalize`
 *  enforce policies; `reply_payload_sending` adds a fail-open upgrade notice as
 *  a distinct footer block so the user sees when enforcement was degraded (the
 *  agent is also informed directly via a next-turn injection). */
const entry = {
  id: "reflexes",
  name: "Reflexes",
  description: "An open safety harness for AI agents — plain-English policy checks on tool calls and replies.",
  register(api: OpenClawPluginApi): void {
    const config = loadConfig(process.env.REFLEXES_CONFIG);
    if (!config) {
      console.warn(
        "[reflexes] no reflexes.yaml found (~/.reflexes/reflexes.yaml or ./reflexes.yaml); plugin idle",
      );
    }
    // Capture the current request + system prompt each model call (same run).
    api.on("llm_input", (event) => rememberRequest(api, event), { priority: 100 });
    // Remember the session's first message (Original task); drop it on end.
    api.on("message_received", (event) => rememberOriginalTask(event), { priority: 100 });
    api.on("session_end", (event) => forgetSession(event), { priority: 100 });
    // Accumulate the per-run trajectory (what the agent actually did).
    api.on("after_tool_call", (event, ctx) => rememberAction(api, event, ctx), { priority: 100 });
    // Surface a fail-open upgrade notice as a distinct context/footer block on
    // the reply the user sees — OpenClaw's context/prompt channels are
    // model-facing only, so the outbound reply is the only reliable way to tell
    // the human enforcement was degraded. Never mutates the model's response.
    api.on("reply_payload_sending", (event, ctx) => appendUpgradeNotice(event, ctx), { priority: 100 });
    // Enforce before_tool_call policies on each tool call.
    api.on(
      "before_tool_call",
      async (event, ctx) =>
        checkToolCall(event, config, {
          taskContext: recallTaskContext(api, ctx),
          sessionKey: ctx.sessionKey,
          onFailOpenUpgrade: (upgrade) => enqueueAgentNotice(api, ctx.sessionKey, upgrade),
          onFlag: (note) => enqueueAgentText(api, ctx.sessionKey, note, `reflexes-flag:${ctx.sessionKey}`),
        }),
      { priority: 100 },
    );
    // Enforce before_response policies on the agent's final reply, with the
    // trajectory so completion policies can check claims against what happened.
    api.on(
      "before_agent_finalize",
      async (event, ctx) =>
        checkResponse(event, config, {
          taskContext: recallTaskContext(api, ctx),
          trajectory: recallTrajectory(api, ctx),
          sessionKey: ctx.sessionKey,
          onFailOpenUpgrade: (upgrade) => enqueueAgentNotice(api, ctx.sessionKey, upgrade),
          onFlag: (note) => enqueueAgentText(api, ctx.sessionKey, note, `reflexes-flag:${ctx.sessionKey}`),
        }),
      { priority: 100 },
    );
  },
};

export default entry;
