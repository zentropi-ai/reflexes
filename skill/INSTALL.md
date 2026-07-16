# Installing Reflexes

Install Reflexes into a host. Follow the steps in order. If a check fails,
stop and ask the user — don't invent a workaround.

`SKILL_DIR` = this skill's directory (holds `SKILL.md`, `scripts/`,
`reflexes/`). You're reading this file, so you know it.

## Two install types

- **Subprocess host CLI** (Claude Code, Codex, Hermes) — the user runs an
  agent as a CLI with hooks. Do **Steps 1–8**, one host at a time.
- **OpenClaw** — also a CLI host, but Reflexes ships as a native OpenClaw
  plugin, not a Python hook. Follow the **OpenClaw variant** (under Step 6); it
  overrides Steps 2 and 6 (no pip runtime, no `reflexes-hook` wiring).
- **Python-framework project** (OpenAI Agents SDK, Claude Agent SDK, Google
  ADK, LangGraph, PydanticAI, smolagents, AutoGen, NeMo Guardrails)
  — the user is building an agent and wants Reflexes importable. Skip to
  **Library install** at the bottom.

If unsure, ask: protecting an existing CLI agent, or adding Reflexes to a
codebase they're writing?

## Done when

**Copying the skill folder is not installing Reflexes** — nothing enforces
until the runtime is wired and the doctor passes. Done only when all three
hold (verified by commands, not assumed):

1. `uv run <SKILL_DIR>/scripts/reflexes-hook --host <HOST> --config /dev/null < /dev/null` prints `{}`. (OpenClaw has no `reflexes-hook`; instead its plugin is installed — see the variant.)
2. The host's hook config contains Reflexes entries (Step 6).
3. `uv run <SKILL_DIR>/scripts/reflexes-doctor --config ~/.reflexes/reflexes.yaml` exits 0 (green) or 1 (warnings only). Exit 2 is a fail-open risk to fix.

If the doctor exits 1 with `no ZENTROPI_API_KEY set`, the install is wired
but **not enforcing** — tell the user to add a key (or swap the evaluator)
and re-run; don't claim protection is live.

## Step 1: Prerequisites

Need Python 3.10+, `uv`, and (for the default Zentropi evaluator) an API key.

```bash
python3 --version   # 3.10+
uv --version        # if missing: pip install uv
test -n "$ZENTROPI_API_KEY" && echo "key: set" || echo "key: MISSING"
```

If the key is **MISSING**: tell the user to create a free account at
https://zentropi.ai, mint an API key, and add `export ZENTROPI_API_KEY=<key>`
to their shell profile — it must live in the environment the host CLI runs
in. Or skip the key and swap to another evaluator (SKILL.md → Swap the
evaluator). Either way the doctor (Step 7) will tell you where you stand.

## Step 2: Confirm the bundled runtime

No pip install for a CLI host — the runtime is bundled in this skill. From
`SKILL_DIR`:

```bash
uv run ./scripts/reflexes-hook --host claude-code --config /dev/null < /dev/null
```

Prints `{}` and exits 0. (`--host` is irrelevant here — it only checks the
runtime.)

## Step 3: Identify the host

| HOST | CLI | Settings file |
|---|---|---|
| `claude-code` | Claude Code | `~/.claude/settings.json` |
| `codex` | Codex | `~/.codex/hooks.json` (+ `~/.codex/config.toml`) |
| `hermes` | Hermes ≥ 0.18 | `~/.hermes/config.yaml` |
| `openclaw` | OpenClaw | native plugin (`openclaw plugins install …`) — see the OpenClaw variant, not a hook file |

You're running inside a host, so you know which one. If the user named a
different host, use that slug. If unsure, check which of `~/.claude` /
`~/.codex` exists, or ask.

## Step 4: Install the starter policies

The starter policies ship in two bundles under
`$SKILL_DIR/reflexes/_starter_policies/`:

- **`integrity/`** — `Constraint-Violation`, `Unconfirmed-Destructive-Action`,
  `Sycophancy`. Safety: don't overstep the user's instructions, don't
  destroy data without asking, don't flatter instead of giving honest
  judgment. **Enabled by default.**
- **`autonomy/`** — `Early-Stopping`, `Laziness-Detection`, `Unsourced-Claims`.
  Reliability: policies that help an agent run reliably over long stretches so
  it's more likely to actually achieve the user's goal — finish the job, do
  the work for real, ground its claims. **Installed but off by default**
  (present, commented out, in Step 5's config for easy discovery).

Verify `SKILL_DIR` first: `test -f "$SKILL_DIR/scripts/reflexes-hook"`.

Copy **both** bundles' `.md` files into `~/.reflexes/policies/`, flat (create
the dir; **skip files that already exist** — don't overwrite the user's edits,
e.g. `cp -n`):

```bash
mkdir -p ~/.reflexes/policies
cp -n "$SKILL_DIR"/reflexes/_starter_policies/integrity/*.md ~/.reflexes/policies/
cp -n "$SKILL_DIR"/reflexes/_starter_policies/autonomy/*.md  ~/.reflexes/policies/
```

Don't resolve them via `python3 -c "import reflexes…"` — a CLI host has no pip
`reflexes`. You should end with all six policy files in `~/.reflexes/policies/`.

## Step 5: Write the default config

If `~/.reflexes/reflexes.yaml` exists, leave it. Otherwise write it:

Write it exactly as below — the integrity bundle active, and the autonomy
bundle present but commented out, so a user editing `reflexes.yaml` discovers
it naturally without the installer having to explain any of this:

```yaml
reflexes:
  default:
    # --- Integrity (safety) — enabled ---
    - policy: ./policies/Constraint-Violation.md
      name: Constraint-Violation
      trigger: before_tool_call
      action: deny
    - policy: ./policies/Unconfirmed-Destructive-Action.md
      name: Unconfirmed-Destructive-Action
      trigger: before_tool_call
      action: deny
    - policy: ./policies/Sycophancy.md
      name: Sycophancy
      trigger: before_response
      action: deny

    # --- Autonomy (reliability) — optional; uncomment to enable ---
    # These help the agent run reliably over long stretches so it's more likely
    # to actually achieve your goal: finish the job, do the work for real, and
    # ground its claims. Early-Stopping and Unsourced-Claims `deny` (block the
    # reply so the agent self-corrects); Laziness-Detection — the most
    # subjective — is `flag` (nudge without blocking). Adjust either way.
    # - policy: ./policies/Early-Stopping.md
    #   name: Early-Stopping
    #   trigger: before_response
    #   action: deny
    # - policy: ./policies/Laziness-Detection.md
    #   name: Laziness-Detection
    #   trigger: before_response
    #   action: flag
    # - policy: ./policies/Unsourced-Claims.md
    #   name: Unsourced-Claims
    #   trigger: before_response
    #   action: deny
evaluators:
  default:
    type: zentropi
    model: cope-latest
    api_key_env: ZENTROPI_API_KEY
```

The integrity policies use mixed triggers by design: `Constraint-Violation`
and `Unconfirmed-Destructive-Action` run `before_tool_call` (they inspect the
pending tool call and stop it *before* it executes); `Sycophancy` runs
`before_response`. Change `deny` to `flag` on any you'd rather log than block.

## Step 6: Wire the hook

Add Reflexes as a command hook on all five lifecycle events. The command
must be exact; you do the merge — edit the settings file yourself, the way
you'd edit any config.

Command (verbatim — substitute `<SKILL_DIR>` and `<HOST>`, keep `$HOME`
literal so the host expands it at runtime):

```
uv run <SKILL_DIR>/scripts/reflexes-hook --host <HOST> --config $HOME/.reflexes/reflexes.yaml
```

Register it on `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
and `Stop`, each with matcher `"*"`. (The four policy events run the checks;
`SessionStart` runs a cached, fail-silent update check and surfaces an "update
available" notice once per session — the same command handles all of them.)
Files: `claude-code` → `~/.claude/settings.json`; `codex` → `~/.codex/hooks.json`.
Same JSON shape — here's one event as the template:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "*", "hooks": [ { "type": "command", "command": "uv run <SKILL_DIR>/scripts/reflexes-hook --host <HOST> --config $HOME/.reflexes/reflexes.yaml" } ] }
    ]
  }
}
```

Merge rules:

- Preserve everything already in the file — merge into the existing `hooks`
  object. If an event already has entries, **append** the Reflexes entry to
  its array (don't replace it).
- Idempotent: if an event already has a command containing `reflexes-hook`,
  leave it.
- If the file isn't valid JSON, stop and tell the user — don't overwrite it.
- Create the file (and its dir) if absent.

**Codex only:** enable hooks in `~/.codex/config.toml` — it must be **under the
`[features]` table**, not a bare top-level key (Codex silently ignores
`hooks.json` otherwise, so this is easy to get wrong):

```toml
[features]
codex_hooks = true
```

Add the `[features]` header if it's absent; add `codex_hooks = true` under it if
missing. If the user has `codex_hooks = false`, ask before flipping. After this,
`reflexes-doctor` will confirm Codex hooks are actually enabled (not just that
`hooks.json` exists).

### Hermes variant

Create `~/.reflexes/hermes-hook.sh`, executable (keep `$HOME` literal):

```bash
#!/usr/bin/env bash
exec uv run <SKILL_DIR>/scripts/reflexes-hook --host hermes --config $HOME/.reflexes/reflexes.yaml
```

Register its **absolute** path in `~/.hermes/config.yaml` under
`pre_tool_call` and `pre_verify` as `command` hooks (preserve existing).
Approve it (`hermes hooks test pre_tool_call`, or run Hermes once with
`--accept-hooks`), then `hermes hooks doctor`. Pre-0.18 Hermes has no
pre-call hook — deny lands one turn late.

### OpenClaw variant

No pip package; Step 2 doesn't apply. Install the plugin:

```bash
openclaw plugins install @zentropi/reflexes-openclaw
```

Then:

1. **Grant conversation access (required):**
   ```bash
   openclaw config set plugins.entries.reflexes.hooks.allowConversationAccess true
   ```
   OpenClaw gates the `llm_input` and `before_agent_finalize` hooks off for
   third-party plugins by default. Without this flag the plugin still loads and
   `before_tool_call` still fires, but with **no `Current request` context**
   (`llm_input` is blocked, so the two tool-call policies are context-starved —
   `Unconfirmed-Destructive-Action` can over-block, `Constraint-Violation` can't
   see a constraint stated this turn) and **no `before_response` enforcement at
   all** (`before_agent_finalize` is blocked, so `Sycophancy` and the autonomy
   bundle silently don't run). Verified on openclaw 2026.6.11.
2. Ensure `~/.reflexes/reflexes.yaml` + policies exist (Steps 4–5); put
   `ZENTROPI_API_KEY` in the gateway env; restart the gateway.

**Full trigger parity.** All three default integrity policies run:
`Constraint-Violation` and `Unconfirmed-Destructive-Action` at
`before_tool_call`, and `Sycophancy` at `before_response` (via OpenClaw's
`before_agent_finalize` hook — a deny returns `{action: "revise"}` and the
agent rewrites its reply). The opt-in autonomy bundle (`before_response`) works
too. Each check receives the same assembled task context as the other hosts
(SPEC §5.1): the plugin captures the `Current request` + system prompt from an
`llm_input` hook (plus the per-turn trajectory from `after_tool_call`) and
reads them back (verified against openclaw 2026.6.11). So the completion
policies (`Early-Stopping`, `Laziness-Detection`) get the trajectory here too —
full context parity with Claude Code.

## Step 7: Verify

```bash
uv run <SKILL_DIR>/scripts/reflexes-doctor --config ~/.reflexes/reflexes.yaml
```

Confirms the config parses (and every `policy:` ref is on disk), the
evaluator answers a live probe, and which hosts are wired. Exit `0` =
enforcing; `1` = warnings (commonly no API key — wired but not enforcing);
`2` = a fail-open risk. Act on what it prints before reporting done.

## Step 8: Tell the user

- **Doctor exit 0:** Reflexes is installed and enforcing on `<HOST>`.
  Restart `<HOST>` to load the hook. The 3 integrity policies are active.
- **Doctor exit 1 (no key):** Reflexes is wired but not enforcing yet.
  The user must add `ZENTROPI_API_KEY` (free at https://zentropi.ai) or swap
  the evaluator, then re-run the doctor.

Then optionally walk **Verify it works** to see a policy fire.

## Verify it works

Confirms a policy fires end-to-end. The two `before_tool_call` integrity
policies stop a pending tool call: in a fresh host session (restart first),
give the agent a broad task ("clean up this project"); if it decides on its
own to `rm` a file you never named, `Unconfirmed-Destructive-Action` denies
the call *before it runs* and the agent asks you first instead. `Sycophancy`
(`before_response`) fires when the agent's reply flatters or caves instead of
giving honest judgment. Any policy firing and the agent revising = working. If
nothing fires, see SKILL.md → Debug.

## Library install (Python frameworks)

Here "install" means the `reflexes` package is importable in the project's
environment. Do NOT wire `with_reflexes(...)` now — that's applied later
when the user builds the agent.

1. Add it with the project's own package manager: `uv add reflexes`,
   `poetry add reflexes`, or `pip install reflexes`.
2. Verify it imports: `python -c "import reflexes; print(reflexes.__version__)"`.
3. Create a project-local `reflexes.yaml` + `policies/` (same shape as
   Step 5, living in the project dir).
4. Verify: `python -m reflexes doctor --config reflexes.yaml`.

Wiring reference (apply when building the agent):

```python
# OpenAI Agents SDK / Google ADK / PydanticAI / smolagents:
from reflexes import with_reflexes
agent = with_reflexes(agent, reflexes=["default"], config_file="reflexes.yaml")
```
```python
# LangGraph (middleware):
from reflexes import middleware_from_config
agent = create_agent(model=..., tools=[...],
    middleware=[middleware_from_config(reflexes=["default"], config_file="reflexes.yaml")])
```
```python
# Claude Agent SDK (hooks, not with_reflexes):
from claude_agent_sdk import ClaudeAgentOptions
from reflexes.integrations.claude_agent_sdk import create_hooks
options = ClaudeAgentOptions(
    hooks=create_hooks(reflexes=["default"], config_file="reflexes.yaml"))
```

## Troubleshooting

- **Permission denied writing the settings file:** owned by another user or
  a read-only mount. Stop and ask the user.
- **Invalid JSON in the existing settings file:** don't overwrite — surface
  the error and stop.
- **Hook doesn't fire after install:** check that `uv` is on the host's PATH;
  the `<SKILL_DIR>` baked into the settings command is correct
  (`test -f <SKILL_DIR>/scripts/reflexes-hook`); the host was restarted. Run
  the settings command by hand with a sample JSON payload on stdin to see the
  error.
- **Codex does nothing:** `codex_hooks = true` is missing from
  `~/.codex/config.toml`, or it's there but **not under the `[features]` table**
  (a bare top-level `codex_hooks = true` is silently ignored). `reflexes-doctor`
  now flags this.

## Re-running

Install is idempotent: Step 4 skips existing policies, Step 5 skips an
existing `reflexes.yaml`, Step 6 skips an already-wired event. Safe to
re-run — to switch hosts (re-run with the other slug) or recover partial
state.
