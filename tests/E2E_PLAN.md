# Host integration E2E test plan

How we verify a **host integration** (Claude Code, OpenAI Codex, Hermes,
OpenClaw, and the Python framework adapters) actually enforces. This plan exists
because two real bugs slipped past weaker testing:

- **Hermes `on_input` never fired** — the install wiring omitted the `pre_llm_call`
  event, and the code emitted a block Hermes silently ignores on that event.
- **OpenClaw `on_input` couldn't enforce** — we registered the observe-only
  `llm_input` hook when a real input-gate hook (`before_agent_run`) existed.

Both were missed for the **same two reasons**, which this plan is designed to
prevent:

1. We tested only the *default* policies (which bind `before_tool_call` /
   `before_response`), so "the defaults enforce" was mistaken for "every trigger
   enforces." The untested triggers (`on_input`, `after_tool_call`) were broken.
2. We tested the hook **subprocess in isolation** (piping a synthetic payload to
   the adapter) — which proves the adapter *returns* a block, but not that the
   host **wires the event** or **honors the directive** in a real session.

## Principles

1. **Source-verify the host's hook API first.** Before claiming a trigger is or
   isn't enforceable on a host, read the *host's own source*: enumerate every
   hook/event, and for each, what a handler's return can do (block / veto /
   revise / observe-only). This is the source of truth — not our docs, and not
   the hook's *name*. (Both bugs above came from trusting a hook's apparent
   meaning — `post_tool_call` ≈ "after_tool_call", `llm_input` ≈ "on_input" —
   instead of reading what the host does with the return.)
2. **Test every trigger, not just the defaults.** Exercise all four reflex
   triggers individually on each host — `on_input`, `before_tool_call`,
   `after_tool_call`, `before_response` — with a policy bound to *that* trigger.
   The shipped default policies only touch two of them.
3. **Both directions per trigger.** A clear MATCH (the deny actually
   blocks/degrades) **and** a clear NO-MATCH (allowed). A deny-only test can't
   catch over-blocking or a silently-ignored block.
4. **Real install + full in-session round-trip.** Install the actual host
   runtime (`hermes-agent`, `openclaw`, `codex`, `claude`, the framework SDKs),
   install Reflexes into it **per `skill/INSTALL.md`** (isolated HOME, real
   sandbox), and drive a real session. Judge enforcement by **side effect** —
   the destructive action did not happen, the prompt was not answered — not by
   inspecting the adapter's return. Piping a payload to the adapter proves the
   adapter works; it does **not** prove the host wires the event or honors the
   directive.
5. **No silent fail-open at any trigger.** For every trigger a host *cannot*
   enforce, confirm the integration does not emit a directive the host silently
   drops. It must degrade **visibly** (e.g. context injection), or the loader
   must **warn / skip-with-notice**, and `reflexes-doctor` must surface it. A
   trigger that fires the hook but has its directive ignored is a *silent
   fail-open* — the exact failure class this project exists to prevent.
6. **Fail-open safety.** With the evaluator unreachable, every enforcing trigger
   must fail **open** (allow), including under a host's fail-*closed* gate (e.g.
   OpenClaw `before_agent_run`).

## Procedure (per host)

1. **Source-verify hooks.** Read the installed host's source; record a
   hook → accepted-return → effect table and the host version. Decide, per reflex
   trigger, which host hook (if any) can enforce it.
2. **Install for real.** Install the host runtime; install Reflexes per
   `INSTALL.md` into an isolated HOME; grant any required capability flag
   (Codex hook *trust* via `/hooks`; OpenClaw `allowConversationAccess`). Never
   touch the developer's real config; clean up after.
3. **Doctor.** `reflexes-doctor` is green, or its warnings are expected and
   documented (e.g. Codex trust is unverifiable).
4. **Per-trigger round-trip.** For each of the four triggers: bind a
   *deterministic* policy at that trigger, drive a real session that triggers it,
   confirm block/degrade **by side effect**, then confirm a no-match passes.
5. **Fail-open.** Point the evaluator at an unreachable endpoint → the action is
   allowed (never blocked by our failure).
6. **Record the matrix** below, with evidence, and the source reason for any
   `UNSUPPORTED` trigger.

## Coverage matrix (fill per host, with evidence)

| Host | on_input | before_tool_call | after_tool_call | before_response | Fail-open | Install method / version |
|---|---|---|---|---|---|---|
| Claude Code | | | | | | agent-driven install per INSTALL.md |
| OpenAI Codex | | | | | | agent-driven install; hooks **trusted** via `/hooks` |
| Hermes | | | | | | `pip install hermes-agent`; shell hooks on all `pre_*` events |
| OpenClaw | | | | | | `openclaw plugins install <local>`; `allowConversationAccess` |
| OpenAI Agents SDK / Claude Agent SDK / Google ADK / LangGraph / PydanticAI / smolagents / NeMo / AutoGen | | | | | | `pip install <framework>` + library wiring |

Cell values: `✅ blocks` · `⚠️ degrades (how)` · `❌ UNSUPPORTED (source reason)` ·
`—` (n/a). Every non-`✅` cell must name the host source that proves it and
confirm it is **not** a silent fail-open.

## Automated vs. manual

- **Automated (`tests/test_*_e2e.py`):** adapter-level per-trigger behaviour with
  a live evaluator — gated on the framework + `*_API_KEY` being present. These
  cover triggers the shipped tests exercise; extend them when a trigger's
  behaviour changes.
- **Manual / clean-room (this plan):** the real-runtime install + in-session
  round-trip. Run before a release and whenever a host integration or its
  supported-trigger set changes. Capture the coverage matrix in the PR.

## Anti-patterns (these hid the bugs — do not rely on them)

- "The default policies enforce" as proof that all triggers work.
- Piping a synthetic hook payload to the adapter as proof the host wires/honors it.
- Trusting a hook's *name* instead of reading what the host does with its return.
- Trusting our own prior docs as the source of truth for host capabilities.
