# Reflexes — host compatibility

Which hosts Reflexes supports, how each integration works, and how mature it is.
If you're choosing a host or wiring one up, this is the reference.

## Host taxonomy

Reflexes plugs into two structurally different kinds of host, and the
distinction affects how (and whether) it can block an action and surface
notices:

- **Harnesses** — standalone agent *runtimes* with a lifecycle hook system that
  you install a plugin into (you don't write the agent loop): **Claude Code,
  OpenAI Codex, OpenClaw, Hermes.**
- **Framework libraries** — SDKs you build an agent *with*, in your own code;
  Reflexes wraps the agent via middleware/callbacks you register: **Google ADK,
  LangGraph, NeMo Guardrails, Claude Agent SDK, OpenAI Agents SDK, PydanticAI,
  smolagents, AutoGen.**

This is architectural, not a ranking — hook surfaces vary *within* each kind.
Capability-based groupings (e.g. the fail-open table) therefore cut across the
split.

## Maturity tiers

Every host below is published; the tier tells you **what to expect**. Reflexes
is an open standard, and the per-host integration work — especially in the
Experimental tier — is exactly where community contributions land.

- **🟢 Stable** — production-ready: deny/flag/log actions, self-correction, the key triggers, and
  task context are in place and verified end-to-end against a live agent.
  → **Claude Code, OpenAI Codex, LangGraph, Hermes, Google ADK.** (ADK's one
  caveat: `before_response` deny *substitutes* the policy text for the response
  rather than forcing a revise — fail-safe, a framework limit; its other
  triggers self-correct.)
- **🟡 Beta** — the important pieces are built and it self-corrects, but it has a
  documented gap:
  - **Claude Agent SDK** — `before_response` is inform-only (responses stream and
    can't be retracted); deny at `on_input`/`before_tool_call` self-corrects.
  - **OpenAI Agents SDK** — full triggers + self-correction; input/output
    guardrails hard-stop unless you use the `run_with_reflexes()` retry wrapper.
  - **OpenClaw** — the TypeScript plugin is fully live-verified, but it's a
    separate codebase from the Python hosts and is soaking before promotion. It
    also can't enforce `on_input`/`after_tool_call` (OpenClaw's input-side and
    post-tool hooks are observe-only); `before_tool_call` + `before_response`
    enforce with self-correction.
- **🔴 Experimental** — a core capability is structurally missing (a framework
  limit), or the integration is newer and wants more soak. Works within its
  limits; contributions welcome:
  - **smolagents** — no `before_tool_call` hook (can't check tool calls before
    they run).
  - **PydanticAI** — no `after_tool_call` hook; recently migrated to the
    pydantic-ai 2.x `capabilities` system.
  - **NeMo Guardrails** & **AutoGen** — no self-correction (NeMo substitutes a
    canned refusal; AutoGen drops the message).

## At-a-glance

Which triggers each host can enforce. **✅** enforces · **⚠️** conditional (see
notes) · **❌** not available (framework limit). Every host supports the
`deny` / `flag` / `log` actions, though `flag`/`log` degrade to log-only where
the host has no agent-visible channel (see [Actions](#actions)).

### Harnesses (runtimes you install a plugin into)

| Host | on_input | before_tool_call | after_tool_call | before_response | Maturity |
|---|---|---|---|---|---|
| Claude Code | ✅ | ✅ | ✅ | ✅ | 🟢 Stable |
| OpenAI Codex | ✅ | ✅ | ✅ | ✅ | 🟢 Stable |
| Hermes | ⚠️ context only | ✅ | ❌ | ⚠️ coding turns only | 🟢 Stable |
| OpenClaw | ❌ | ✅ | ❌ | ✅ | 🟡 Beta |

### Framework libraries (SDKs you wrap in your own code)

| Host | on_input | before_tool_call | after_tool_call | before_response | Maturity |
|---|---|---|---|---|---|
| LangGraph | ✅ | ✅ | ✅ | ✅ | 🟢 Stable |
| Google ADK | ✅ | ✅ | ✅ | ⚠️ substitute | 🟢 Stable |
| OpenAI Agents SDK | ✅ | ✅ | ✅ | ✅ | 🟡 Beta |
| Claude Agent SDK | ✅ | ✅ | ✅ | ⚠️ inform-only | 🟡 Beta |
| smolagents | ✅ | ❌ | ✅ | ✅ | 🔴 Experimental |
| PydanticAI | ✅ | ✅ | ❌ | ✅ | 🔴 Experimental |
| NeMo Guardrails | ✅ | ✅ | ✅ | ⚠️ substitute | 🔴 Experimental |
| AutoGen | ✅ | ✅ | ✅ | ⚠️ drop | 🔴 Experimental |

## What we send to the evaluator

For every host the evaluator receives the same call shape: a `content_text`
field, a `criteria_text` field, and a model name. Hosts differ only in how the
content string is built and what task context gets prepended.

**Content-labeling principle:** when the content includes both context and the
thing being evaluated, the framing clearly marks which is which, so the
evaluator judges the right text. The content leads with the current request
under a `Current request:` label, with supporting context (`Original task:`,
`Standing instructions:`, `Actions this turn:`) under their own labels.

### Context by host

Every host builds the same labeled task-context block (SPEC §5.1), but each can
only supply the sections its runtime exposes. Context matters:
`Constraint-Violation` needs the **Current request** to see the constraint, and
`Unconfirmed-Destructive-Action` needs it to tell an authorized deletion from an
unauthorized one.

| Host | Current request | Original task | Standing instructions | Actions this turn |
|---|---|---|---|---|
| Claude Code | ✓ | ✓ | ✓ CLAUDE.md / AGENTS.md | ✓ |
| OpenAI Codex | ✓ | ✓ | ✓ | ✓ |
| OpenClaw | ✓ | ✓ | ✓ system prompt / AGENTS.md | ✓ |
| Hermes | ✓ | ✓ | ✓ AGENTS.md / CLAUDE.md | ✓ |
| Google ADK | ✓ | — | ✓ `agent.instruction` | — |
| OpenAI Agents SDK | ✓ | — | ✓ `Agent(instructions=…)` | — |
| Claude Agent SDK | ✓ | ✓ | ⚠ `create_hooks(system_prompt=…)` | — |
| LangGraph | ✓ | — | ⚠ system message (best-effort) | — |
| NeMo Guardrails | ✓ | — | — | — |
| smolagents | ✓ | — | — | — |
| AutoGen | ✓ | — | — | — |

`✓` provided · `—` not available on that host · `⚠` conditional. **Actions this
turn** (the trajectory) needs the host to persist what the agent did — available
on the harnesses. Where a section is missing, dependent policies degrade rather
than error.

### Fail-open notice (who learns enforcement was degraded)

When a policy can't be evaluated for a capacity reason (rate limit or
subscriber-only), the check **fails open** — the operation proceeds unchecked —
and Reflexes surfaces a notice through whatever channel the host has. The
baseline is everywhere: a stderr notice + an `upgrade_required` OpenTelemetry
counter.

**Governing principle — never alter the agent's actual response.** Downstream
code often depends on the exact shape of a model's output, so a Reflexes notice
must not rewrite it. On a fail-open, Reflexes warns the **agent** only through
its *input* context, and warns the **user** only through a channel separate from
the response body (an out-of-band field or a distinct message). This is why a
host whose only user-facing option would rewrite the reply is left agent-only or
operator-only.

| Hosts | User | Agent | Mechanism |
|---|---|---|---|
| OpenClaw | ✓ | ✓ | User: reply footer (a distinct `context` block, needs a chat channel). Agent: next-turn injection, independent of any channel. Both once per session, never rewriting the reply. |
| Claude Code / OpenAI Codex | ✓ | ✓ | User: out-of-band `systemMessage`, once per session. Agent: `additionalContext`, once per turn. |
| Claude Agent SDK; Google ADK; LangGraph; Hermes | — | ✓ | Agent-context injection only (`additionalContext`, graph state, or Hermes' `pre_llm_call` context). No integrity-safe user channel. |
| PydanticAI, smolagents | — | — | A non-blocking channel exists next to our hook but isn't wired yet. |
| NeMo Guardrails, OpenAI Agents SDK, AutoGen | — | — | No integrity-safe channel reachable from where Reflexes plugs in. |

The notice's own text carries the fix (subscribe to raise the limit, or
self-host the open CoPE model), so it flows into every channel above without
per-host wording.

## Per-host integration

### Claude Code

Subprocess hook: reads JSON on stdin, returns JSON on stdout. Wired into
`~/.claude/settings.json` on four events.

| Trigger | Content sent | Prepended context |
|---|---|---|
| on_input | the user's prompt verbatim | none |
| before_tool_call | `{"tool_name": "...", "args": {...}}` | task context (current request + CLAUDE.md + session's first prompt) |
| after_tool_call | `{tool_name} returned: {result}` | same |
| before_response | the last assistant message | same |

### OpenAI Codex

Same hook protocol as Claude Code (the integration re-uses Claude Code's
handler). Hooks live in `~/.codex/hooks.json`; same content shape and context.

### Google ADK

In-process Python integration via ADK's agent callbacks. Same content shape and
context labels as Claude Code.

| Trigger | Content sent | Prepended context |
|---|---|---|
| on_input | the last user-role text from the model request | none |
| before_tool_call | `{tool_name}({json_args})` | current request + `agent.instruction` |
| after_tool_call | `{tool_name} returned: {result}` | same |
| before_response | the LLM response text | same |

`before_response` deny substitutes the policy text for the response (it can't
force a same-turn revise — an ADK framework limit); the other triggers
self-correct.

### LangGraph

In-process integration via LangGraph middleware; register it with
`middleware_from_config()`. Note the middleware is async — run agents through the
async API (`ainvoke`).

| Trigger | Content sent | Prepended context |
|---|---|---|
| on_input | the last user message from graph state | none |
| before_tool_call | `{"tool_name": "...", "args": {...}}` | `Current request:` |
| after_tool_call | `{tool_name} returned: {result}` | same |
| before_response | the last AI message from state | same |

Standing instructions are best-effort — captured only when the system message
lives in graph state (with `create_agent(system_prompt=…)` it lives in model
config, outside state). The current-request backbone is always provided.

### NeMo Guardrails

In-process integration via Colang custom actions. Register them, then reference
the actions in your input/output rails. `on_input`/`before_response`/tool checks
all supported. Deny substitutes NeMo's canned refusal — the model never sees the
policy and doesn't self-correct (inherent to NeMo's output-rails model). The
fail-open notice is operator-only.

### OpenClaw

`integrations-ts/openclaw` — the `@zentropi/reflexes-openclaw` plugin. Registers
seven hooks: four capture context (`llm_input`, `message_received`,
`session_end`, `after_tool_call`), two enforce (`before_tool_call`,
`before_agent_finalize`), and `reply_payload_sending` surfaces the fail-open
notice.

- **`before_tool_call` deny** → `{block: true, blockReason: <policy>}`; OpenClaw
  hands the policy text to the model as the tool error, which self-corrects.
- **`before_response` deny** → `{action: "revise", retry: {instruction: <policy>}}`;
  OpenClaw blocks finalization and re-runs the model to revise.
- **`on_input` / `after_tool_call` can't enforce** — OpenClaw's input-side and
  post-tool hooks are observe-only (they can't block).

Reads the same `~/.reflexes/reflexes.yaml` as every other host and assembles the
same labeled task context (the `before_*` events carry no conversation, so the
capture hooks reconstruct it). Supports all four evaluator backends
(zentropi/openai/anthropic/gemini). Policies load at gateway startup.

**Install requirement:** set
`plugins.entries.reflexes.hooks.allowConversationAccess=true` — OpenClaw gates
the `llm_input` + `before_agent_finalize` hooks off for third-party plugins by
default; without it, tool-call policies run context-starved and reply policies
don't run. See the plugin's [README](./integrations-ts/openclaw/README.md).

### Hermes (NousResearch)

An agent harness, like Claude Code / Codex / OpenClaw. Reflexes installs as a
Hermes **shell hook** declared in `~/.hermes/config.yaml` (integration:
`nous_hermes_shell.py`, for hermes-agent ≥ 0.18). Use the console script
`reflexes-hermes-hook`; Hermes spawns hook commands with `shell=False`, so the
command must be a bare executable (no `cd`, `&&`, or `~`) or the hook fails to
spawn — which fails open.

| Reflexes trigger | Hermes hook | Capability |
|---|---|---|
| before_tool_call | `pre_tool_call` | Pre-execution deny — vetoes the call; the policy text becomes the tool result the model sees. |
| on_input | `pre_llm_call` | Fires and injects the policy as model-visible context (best-effort refusal) + delivers fail-open/update notices. Hermes honors **no block** on `pre_llm_call`, so a deny can't hard-stop the turn — use `before_tool_call`/`before_response` for a hard block. |
| before_response | `pre_verify` | Force a revise — but `pre_verify` fires only on coding/file-editing turns, so `before_response` policies don't run on plain conversational turns. `before_tool_call` has no such limit. |
| after_tool_call | `post_tool_call` | Not supported for enforcement — Hermes' `post_tool_call` fires *after* execution and a shell hook's return there is discarded (observer-only), so it can't veto. Enforce at `before_tool_call`. |

The pre-0.18 `nous_hermes.py` (loop-level observer, deny one turn late) is
retained only for legacy installs.

## Actions

**deny / flag / log** are distinct in practice:

- **deny** — block the operation; the agent receives the full policy text via the
  host's native error/feedback channel and self-corrects.
- **flag** — allow the operation; the agent receives a one-line note. Only
  agent-visible where the host has a context-append channel (Claude Code / Codex,
  Claude Agent SDK, Google ADK, LangGraph, OpenClaw, Hermes). Elsewhere it
  degrades to log-only.
- **log** — allow silently; written to the debug log only.

**Self-correction on deny** is where hosts differ most:

- **Full self-correction** (agent reads the policy and produces a compliant
  result): **Claude Code / Codex**, **OpenClaw** (revise loop + tool-block
  reason), **Hermes** (`pre_tool_call` block; `pre_verify` on coding turns),
  **LangGraph** (re-runs the model / tool deny returns an adaptable message),
  **PydanticAI** (`ModelRetry`), **smolagents** (error rendered into the next
  prompt, on `before_response`), **OpenAI Agents SDK** (tool-trigger deny; input/
  output guardrails need the `run_with_reflexes()` wrapper).
- **Partial** — **Google ADK**: `before_tool_call` deny returns an error the
  agent adapts to, but `before_response` deny substitutes the policy text (no
  same-turn revise — a framework limit). **Claude Agent SDK**:
  `on_input`/`before_tool_call` self-correct; `before_response` is inform-only
  (streamed responses can't be retracted).
- **None** — **NeMo Guardrails** substitutes a canned refusal; **AutoGen** drops
  the message. Neither shows the policy to the model, so neither self-corrects.

## Known limitations

Framework limits — gaps imposed by the host, not by Reflexes:

- **OpenClaw `on_input` + `after_tool_call`** can't enforce (observe-only hooks).
  `before_tool_call` + `before_response` enforce fully.
- **Hermes `before_response`** self-correction fires only on coding/file-editing
  turns (`pre_verify` posture). The primary `before_tool_call` deny is full.
- **Google ADK `before_response`** deny substitutes rather than forcing a
  same-turn revise (the response is still blocked — fail-safe).
- **Claude Agent SDK `before_response`** is inform-only (streamed responses can't
  be retracted).
- **NeMo Guardrails / AutoGen** have no self-correction path (substitute / drop).
- **smolagents** has no `before_tool_call` hook; **PydanticAI** has no
  `after_tool_call` hook.
- **Standing-instructions context** is not available on NeMo, smolagents, or
  AutoGen (no clean instructions field to read); the current-request backbone is
  provided on all three.

Other:

- **Policy benchmarking** — the starter policies are usable as shipped; formal
  precision/recall benchmarks across large datasets are still being expanded.
