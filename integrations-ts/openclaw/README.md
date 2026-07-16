# @zentropi/reflexes-openclaw

**An open safety harness for OpenClaw agents.** Plain-English Reflexes policies
are checked on each tool call and each reply by a policy-steerable classifier
(CoPE by default, ~200ms). On a policy match the agent gets the policy text back
and self-corrects — a blocked tool call is retried, a non-compliant reply is
revised.

## Install

The simplest install is agent-driven: ask your agent to *"Install the Reflexes skill from github.com/zentropi-ai/reflexes — clone the repo, then read and follow skill/SKILL.md to wire it into this agent."* — it follows the canonical procedure in the top-level
[`SKILL.md`](../../skill/SKILL.md) and does everything below for you. To install
by hand:

```bash
openclaw plugins install @zentropi/reflexes-openclaw   # or a local path
```

**Grant conversation access (required):**

```bash
openclaw config set plugins.entries.reflexes.hooks.allowConversationAccess true
```

Without this, tool-call policies still fire but with no `Current request`
context, and reply policies don't enforce — OpenClaw blocks those hooks for
third-party plugins by default.

## Configure

Create `~/.reflexes/reflexes.yaml` — the same config every Reflexes host shares —
and bind policies to triggers:

```yaml
reflexes:
  default:
    - policy: ./policies/Unconfirmed-Destructive-Action.md
      name: Unconfirmed-Destructive-Action
      trigger: before_tool_call
      action: deny
    - policy: ./policies/Sycophancy.md
      name: Sycophancy
      trigger: before_response
      action: deny
evaluators:
  default:
    type: zentropi
    model: cope-latest
    api_key_env: ZENTROPI_API_KEY
```

Set `ZENTROPI_API_KEY` in the gateway environment and restart the gateway.

**Where the policy files come from.** The `./policies/*.md` files are *not*
bundled with the npm plugin — they're the shared Reflexes starter set. The
agent-driven install above places them in `~/.reflexes/policies/` for you. If
you wire OpenClaw by hand, either copy the starter set from
[`skill/reflexes/_starter_policies/`](../../skill/reflexes/_starter_policies)
into `~/.reflexes/policies/`, write your own (see
[`SKILL.md`](../../skill/SKILL.md)), or skip local files entirely and point
`policy:` at a hosted reference (next).

**Policy sources.** `policy:` can be a local file (preferred), a Zentropi-hosted
labeler (`zentropi:<labeler_id>[@<version>]`), or a GitHub-hosted file
(`github:<owner>/<repo>/<path>@<tag-or-sha>`). Hosted policies are fetched and
cached on first use.

**Evaluator backends.** `type` can be `zentropi` (CoPE — the default and
safety-tuned), `openai`, `anthropic`, or `gemini`. The LLM backends are general
chat models prompted to classify, so they are looser and less calibrated than
CoPE; for them, set `model`, `api_key_env`, and (if needed) `endpoint`.

## What it does

Reflexes enforces at three points in the agent loop:

- **`on_input`** — the user's prompt is checked before it reaches the model (via
  OpenClaw's `before_agent_run` input gate). On a deny the prompt is blocked and
  never submitted. (Conversation hook — requires `allowConversationAccess`.)
- **`before_tool_call`** — every tool call is checked before it runs. On a deny
  the call is blocked and the policy text is handed back as the tool error, so
  the agent sees why and chooses a compliant alternative.
- **`before_response`** — the agent's final reply is checked before delivery. On
  a deny the reply goes back for revision with the policy as the instruction, and
  the agent rewrites it (bounded by OpenClaw's max revisions).

`flag` and `log` record a match without blocking.

Every check receives the same **task context** as the other Reflexes hosts, so a
policy classifies the same way here as on Claude Code: the user's current
request, the session's original task, standing instructions (system prompt or
`AGENTS.md`), and — for reply policies — what the agent actually did this turn.

**Fail-open.** If the evaluator can't be reached, the check fails open (the
operation proceeds) and a one-line notice is surfaced two ways: appended to the
reply the user sees (never altering the model's own text) and given to the agent
on its next turn, so degraded enforcement is never silent. A sustained rate limit
or subscriber-only response adds a pointer to self-hosting the open CoPE model.

## Limitations

- **The enforcing triggers are `on_input`, `before_tool_call`, and
  `before_response`.** `after_tool_call` can't enforce on OpenClaw — its post-tool
  hook is observe-only (return discarded) and fires after the tool has already
  run, so bindings on it load but don't enforce. This is an OpenClaw limitation,
  not a Reflexes one. (`on_input` enforcement uses the `before_agent_run` gate,
  which is a conversation hook — set `allowConversationAccess: true`.)
- Policies load once at gateway startup; restart after editing `reflexes.yaml`.
- The user-facing fail-open notice rides the outbound reply, so it appears only
  when a chat channel is configured (Discord, Slack, …). With `openclaw agent
  --local` and no channel it isn't shown — but the agent is still notified and
  the check still fails open with a log, so nothing is lost silently.

## About

Part of [Reflexes](https://github.com/zentropi-ai/reflexes) — an open safety
harness for AI agents. Tested against OpenClaw 2026.6.11–2026.7.1. Licensed
under Apache-2.0.
