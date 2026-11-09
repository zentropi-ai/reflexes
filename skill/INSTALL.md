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

If the doctor exits 1 with `no PARASAIL_API_KEY set` (the default evaluator's
key), the install is wired but **not enforcing** — tell the user to add a key
(or swap the evaluator) and re-run; don't claim protection is live.

## Step 1: Prerequisites

Need Python 3.10+, `uv`, and (for the default CoPE-via-Parasail evaluator) an
API key in `PARASAIL_API_KEY`.

```bash
python3 --version   # 3.10+
uv --version        # if missing: pip install uv
test -n "$PARASAIL_API_KEY" && echo "key: set" || echo "key: MISSING"
```

If the key is **MISSING**: tell the user to create a free account at
https://zentropi.ai and mint an API key, and **advise them where to put it so
the host (you) can see it** as an environment variable — its env source, not a
shared/committed config file. You know where that is for your host (e.g. Codex
reads `~/.codex/.env`). Or skip the key and swap to another evaluator (SKILL.md
→ Swap the evaluator). Either way the doctor (Step 7) tells you where you stand.

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

(GNU coreutils ≥9 prints `warning: behavior of -n is non-portable … use
--update=none instead` — it's benign; the existing files are still skipped.
It's a warning, not an error; don't treat it as a failure.)

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
    type: systemone                 # evaluates ALL policies at a decision point in ONE call
    model: parasail-cope-b-s1       # Zentropi CoPE, served via Parasail
    endpoint: https://api.parasail.io/v1/systemone
    api_key_env: PARASAIL_API_KEY

# --- Operational settings (optional; safe defaults) ---
enabled: true      # global on/off — set false to disable ALL enforcement (policies kept). Absent = enabled.
logging:
  level: warning   # off | error | warning (default) | info | debug — content ONLY ever at debug
  format: text     # text | json
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

**Codex only — two steps, both required (Codex silently skips hooks otherwise):**

**(a) Enable hooks** in `~/.codex/config.toml`, **under the `[features]` table**
(a bare top-level key is ignored):

```toml
[features]
hooks = true
```

Add the `[features]` header if it's absent; if the user has `hooks` set to
`false`, ask before flipping.

**(b) Trust the hooks.** Codex won't run command hooks until the user has
reviewed and **trusted** them — an untrusted hook looks wired but never fires
(fail open). Tell the user to review the Reflexes definitions and trust them:
in the **Codex CLI**, run `/hooks`; in the **Codex app** (Codex in ChatGPT), open
**Settings → Hooks**. Then restart Codex. (Editing a hook's command later
changes its hash and requires trusting it again.)

`reflexes-doctor` confirms (a) but **cannot see Codex's trust state**, so it
won't declare Codex "enforcing" — after trusting, confirm end-to-end: trigger a
known policy and check it actually fires.

### Hermes variant

Create `~/.reflexes/hermes-hook.sh`, executable (keep `$HOME` literal):

```bash
#!/usr/bin/env bash
exec uv run <SKILL_DIR>/scripts/reflexes-hook --host hermes --config $HOME/.reflexes/reflexes.yaml
```

**`uv` must be on the PATH of Hermes' hook-exec environment.** Hermes spawns
hooks with `shell=False` and treats a failed spawn as "no decision" → a **silent
fail-open** (enforcement just doesn't run). If `uv` isn't reliably on that PATH,
use an absolute interpreter instead of `uv run` — point the wrapper at a venv
that has `reflexes` installed:
`exec /abs/path/venv/bin/python -m reflexes.integrations.nous_hermes_shell --config $HOME/.reflexes/reflexes.yaml`.

Register its **absolute** path in `~/.hermes/config.yaml` on **all three**
per-call events (preserve existing) — each maps to a trigger, and an omitted
event means that trigger's policies **silently never fire**:

```yaml
hooks:
  pre_llm_call:    # on_input policies (also lets fail-open / upgrade notices reach the model)
    - command: "<absolute path>/.reflexes/hermes-hook.sh"
  pre_tool_call:   # before_tool_call policies
    - command: "<absolute path>/.reflexes/hermes-hook.sh"
  pre_verify:      # before_response policies
    - command: "<absolute path>/.reflexes/hermes-hook.sh"
```

Approve it (`hermes hooks test pre_tool_call`, or run Hermes once with
`--accept-hooks`), then `hermes hooks doctor`.

**Hermes trigger caveats:** an `on_input` deny **fires and injects the policy as
context (best-effort), but Hermes can't hard-block a `pre_llm_call`** — for a
hard stop on the user's request, use `before_tool_call` / `before_response`
policies. `after_tool_call` is unsupported too: Hermes' `post_tool_call` hook is
observer-only for shell hooks (its return is discarded) and fires after the tool
already ran. (Pre-0.18 Hermes has no pre-call hook either — deny lands one turn
late.)

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
   (The `reflexes` in the config path is the plugin **id** — not the npm package
   name `@zentropi/reflexes-openclaw` you install with; don't substitute the
   package name here.)
   OpenClaw gates the `llm_input` and `before_agent_finalize` hooks off for
   third-party plugins by default. Without this flag the plugin still loads and
   `before_tool_call` still fires, but with **no `Current request` context**
   (`llm_input` is blocked, so the two tool-call policies are context-starved —
   `Unconfirmed-Destructive-Action` can over-block, `Constraint-Violation` can't
   see a constraint stated this turn) and **no `before_response` enforcement at
   all** (`before_agent_finalize` is blocked, so `Sycophancy` and the autonomy
   bundle silently don't run). Verified on openclaw 2026.6.11.
2. Ensure `~/.reflexes/reflexes.yaml` + policies exist (Steps 4–5); put
   `PARASAIL_API_KEY` (the default evaluator's key) in the gateway env; restart
   the gateway.

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
  The user must add `PARASAIL_API_KEY` (the default evaluator's key, from
  https://parasail.io) or swap the evaluator, then re-run the doctor.
- **Codex, trust not yet granted:** the doctor can't see Codex's hook-trust
  state, so even a fully-wired Codex install is **not enforcing** until the user
  trusts the Reflexes definitions — in the Codex CLI via `/hooks`, or in the
  Codex app (Codex in ChatGPT) via **Settings → Hooks** — and restarts. Tell
  the user this explicitly — it's a distinct "wired but not enforcing" case from
  the no-key one, and it needs a human (it can't be done headlessly).

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
   `poetry add reflexes`, or `pip install reflexes`. The target framework is a
   peer dependency you install yourself — Reflexes doesn't pull it in. Note the
   **LangGraph** path also needs the `langchain` package (for
   `langchain.agents.create_agent` + `AgentMiddleware`); `langgraph` +
   `langchain-core` alone is not enough. The import is lazy, so
   `import reflexes.integrations.langgraph` succeeds — it's building the
   middleware (`middleware_from_config(...)`) that raises
   `ModuleNotFoundError: No module named 'langchain'` until it's installed.
2. Verify it imports: `python -c "import reflexes; print(reflexes.__version__)"`.
3. Create a project-local `reflexes.yaml` + `policies/` (same shape as
   Step 5, living in the project dir).
4. Verify: `python -m reflexes doctor --config reflexes.yaml`. For a library
   install this exits **1** with a single `hosts: no known agent hosts detected`
   warning — that's expected (you're not wiring a CLI host); as long as
   `config`, `evaluator`, and `logging` are ✓, you're good.

Update checks are surfaced only by `reflexes doctor` (and `--check-update`) for
a library install — the middleware/callbacks don't emit an in-process
"update available" notice — so run the doctor periodically to learn about new
releases.

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
- **Codex does nothing:** usually one of two host-side gates — (1) hooks not
  enabled: add `hooks = true` under the `[features]` table in
  `~/.codex/config.toml` (a bare top-level key is ignored); `reflexes-doctor`
  flags this. Or (2) hooks not **trusted**: trust the Reflexes definitions
  (Codex CLI: run `/hooks`; Codex app in ChatGPT: **Settings → Hooks**) and
  restart — Codex silently skips untrusted hooks (same symptom as an unreachable
  evaluator: fail-open), and editing a hook re-requires trust. The doctor can't
  see trust state, so it warns you to verify (2) yourself by triggering a known
  policy.

## Re-running

Install is idempotent: Step 4 skips existing policies, Step 5 skips an
existing `reflexes.yaml`, Step 6 skips an already-wired event. Safe to
re-run — to switch hosts (re-run with the other slug) or recover partial
state.

## Uninstalling

Uninstalling means removing two things: the **skill** and the host **wiring**
(hooks or plugin). By default, **leave `~/.reflexes/` in place** — it holds the
user's policies and config (their edits), it's small, and keeping it makes a
future re-install a one-step affair. Delete it only for a *truly clean* removal
(last step below). No scripts are needed; these are edits you make.

> **Just want to pause enforcement, not uninstall?** Set `enabled: false` in the
> operational footer of `~/.reflexes/reflexes.yaml` instead — enforcement stops
> everywhere, everything stays wired, and flipping it back is instant. See
> SKILL.md → *Turning Reflexes on and off*. Uninstall only when you want the
> hooks and skill actually gone.

Do the steps for the install type you're removing.

### Subprocess host CLI (Claude Code, Codex, Hermes)

1. **Un-wire the hooks** — reverse Step 6 in the same settings file, removing
   only the Reflexes entries and preserving everything else:
   - **`claude-code`** (`~/.claude/settings.json`) — in each of `SessionStart`,
     `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, delete the entry
     whose `command` contains `reflexes-hook`. If an event's array is left empty,
     you may drop the event key. Leave any non-Reflexes hooks alone.
   - **`codex`** (`~/.codex/hooks.json`) — same: remove the `reflexes-hook`
     entries. You can leave `[features] hooks = true` in `~/.codex/config.toml`
     (harmless once no hooks are wired) or remove it if Reflexes was the only
     reason it was enabled. Once the entries are gone, the trust state is moot —
     no `/hooks` step needed.
   - **`hermes`** (`~/.hermes/config.yaml`) — remove the `hermes-hook.sh` entries
     under `pre_llm_call`, `pre_tool_call`, and `pre_verify`, then delete
     `~/.reflexes/hermes-hook.sh`.
   - If your edit leaves the settings file invalid (JSON/YAML), you broke it —
     fix or restore it; never leave it malformed.
2. **Remove the skill** — delete the skill folder (`SKILL_DIR`), or remove it
   however the host manages skills. (Copying the folder in was the install;
   deleting it is the uninstall.)
3. **Restart the host** so it stops invoking the removed hooks.
4. **Confirm** — after restart, trigger something a policy used to catch; nothing
   should fire. (You can't run `reflexes-doctor` once the skill is gone — that's
   expected, not an error.)

### OpenClaw

1. Uninstall the plugin:
   `openclaw plugins uninstall @zentropi/reflexes-openclaw`.
2. Optional: unset the access flag granted at install —
   `openclaw config unset plugins.entries.reflexes.hooks.allowConversationAccess`.
3. Restart the gateway.

### Python-framework project (library)

1. Remove the `with_reflexes(...)` / `middleware_from_config(...)` /
   `create_hooks(...)` wiring from the agent code.
2. Drop the dependency: `uv remove reflexes` (or `poetry remove reflexes` /
   `pip uninstall reflexes`).
3. The project-local `reflexes.yaml` + `policies/` are the user's — keep or
   delete them.

### Truly clean removal (optional)

The steps above leave `~/.reflexes/` (policies, config, and the
`~/.reflexes/.policy-cache/` of hosted policies) so a re-install is trivial. To
leave nothing behind, also delete it and the runtime cache (sessions + the
`debug.log`):

```bash
rm -rf ~/.reflexes "${XDG_CACHE_HOME:-$HOME/.cache}/reflexes"
```

(The runtime cache honors `$XDG_CACHE_HOME` — falling back to `~/.cache` only
when it's unset — so use the form above; a bare `rm -rf ~/.cache/reflexes` would
miss the cache on a host with a custom `XDG_CACHE_HOME`.) Only do this if you're
sure you won't re-install — otherwise the keep-it default saves re-writing the
config and re-tuning the policies.
