---
name: reflexes
description: >
  Install and operate Reflexes — an open safety harness for AI agents that runs
  inference-time policy checks. Use to install Reflexes into a host CLI or Python
  framework, write a custom policy, change the evaluator model, update Reflexes,
  or debug a policy that isn't firing. Follow steps in order; if a check fails,
  stop and ask the user.
metadata:
  author: zentropi
  version: "0.1.1"
---

# Reflexes

Reflexes is an open safety harness for AI agents: at inference time it checks each decision point against plain-English **policies** to keep the agent on track. **Example:** the agent is about to run `rm -rf` on a path the user never named — a Reflexes policy protecting against unconfirmed deletion catches it *before* the tool runs, feeds the agent the rule, and the agent asks first instead of destroying data. 

Reflexes is stewarded by **Zentropi** (https://zentropi.ai) and is open source with an open specification.

**Before you start.** A host-CLI install needs Python 3.10+,
[`uv`](https://docs.astral.sh/uv/), and — for the default evaluator — a free
`ZENTROPI_API_KEY` (https://zentropi.ai); full steps are in `INSTALL.md`.
Throughout, **`SKILL_DIR`** = this skill's own directory (it holds `SKILL.md`,
`INSTALL.md`, `scripts/`, and `reflexes/`).

## Key concepts

Everything Reflexes does is wired together in one file, **`reflexes.yaml`** (at
`~/.reflexes/reflexes.yaml`): it declares which policies are enforced, at which
trigger and action, and which evaluator scores them (detailed in *The config
file*). The pieces it wires together:

- **Policy** — a plain-English rule, written as a markdown file (the whole file is the criteria).
- **Trigger** — *when* a policy is checked: `on_input`, `before_tool_call`, `after_tool_call`, or `before_response`.
- **Action** — *what happens* on a match: `deny` (block and feed the policy back so the agent self-corrects), `flag`, or `log`.
- **Binding** — a line in `reflexes.yaml` tying a policy to a trigger and an action.
- **Evaluator** — the model that scores content against a policy to decide whether it matches. The default is **CoPE**, Zentropi's small open model purpose-built to classify content against a plain-English policy (you can also use OpenAI, Anthropic, or Gemini).

At runtime, Reflexes checks the agent at each trigger, asks the evaluator whether
the bound policies match, and on a match takes the action — such as `deny`,
feeding the policy text back so the agent corrects its own course.

## Default policies

A host-CLI install bundles six **starter policies** into `~/.reflexes/policies/`:

- **Integrity — on by default:** `Unconfirmed-Destructive-Action` (no deleting or destroying data without confirmation), `Constraint-Violation` (don't overstep the user's instructions), `Sycophancy` (honest judgment, not flattery).
- **Autonomy — installed but off:** `Early-Stopping`, `Laziness-Detection`, `Unsourced-Claims` (which all help a long-running agent finish the job it was asked to do); uncomment them in `reflexes.yaml` to enable.

## Operating rules

As Reflexes is a **safety harness**, treat these as hard rules:

- **Follow the steps in order; don't improvise.** If a check fails, **stop and ask the user** — don't invent a workaround.
- **The doctor is your proof.** `reflexes-doctor` verifies enforcement is actually live — the config parses, policies load, and the evaluator answers a live probe. Reflexes fails *open* (a config typo or an unreachable evaluator silently degrades to "every check passes"), so **run it after any config change**: `uv run <SKILL_DIR>/scripts/reflexes-doctor --config ~/.reflexes/reflexes.yaml`. Read the exit code — **0** = enforcing (green); **1** = wired but not fully enforcing (warnings to fix, e.g. a missing `ZENTROPI_API_KEY`); **2** = fail-open risk (stop and fix before relying on it).
- **Don't edit the user's `~/.reflexes/reflexes.yaml` or policies without their explicit OK.**
- **Keep Reflexes current.** An automatic check runs on some hosts, but it doesn't cover all of them, so **also run `--check-update` yourself at least daily** (cheap and cached). When an update is available, tell the user and apply on their go-ahead — never auto-apply (see [Updating](#updating)).

## Pick your task

| Task | Where |
|---|---|
| Install Reflexes into a host CLI | **`INSTALL.md`** |
| Add Reflexes to a Python agent framework | `INSTALL.md` → Library install |
| Uninstall Reflexes (or just pause it) | `INSTALL.md` → Uninstalling |
| Understand or edit `reflexes.yaml` | **The config file** (below) |
| Write, test, or share a policy | **Manage policies** (below); full guide `references/writing-policies.md` |
| Change the evaluator model | **Swap the evaluator** (below) |
| Update to a newer version | **Updating** (below) |
| A policy isn't firing, or other issues | `references/troubleshooting.md` |
| Add a new host / framework integration | `references/add-framework-integration.md` |

## The config file (`reflexes.yaml`)

`~/.reflexes/reflexes.yaml` is the glue that ties everything together: it decides
which policies are enforced, at which trigger, with which action, and which
evaluator scores them. If it isn't in this file, Reflexes doesn't do it. Managing
this file — always with the user's consent — is one of your core jobs, so know
its shape cold. Two required top-level keys carry the substance — `reflexes:`
leads the file — followed by an optional operational footer:

```yaml
reflexes:                # WHAT is enforced — named groups of bindings
  default:               # the `default` group is always active; most bindings go here
    - policy: ./policies/Unconfirmed-Destructive-Action.md   # the policy source
      name: Unconfirmed-Destructive-Action   # required; the policy's identity
      trigger: before_tool_call              # WHEN it's checked
      action: deny                           # WHAT happens on a match
    # - policy: ./policies/Early-Stopping.md # commented out = disabled
    #   name: Early-Stopping
    #   trigger: before_response
    #   action: flag

evaluators:              # WHO scores content against the policies
  default:
    type: zentropi
    model: cope-latest
    api_key_env: ZENTROPI_API_KEY

# --- Operational settings (optional; safe defaults) ---
enabled: true            # global on/off; false disables ALL enforcement. Absent = enabled.
logging:
  level: warning         # off | error | warning (default) | info | debug — content ONLY ever at debug
  format: text           # text | json
```

- **`reflexes:`** — one or more named **groups** of bindings. The `default` group
  is always active, so that's where bindings normally live. (Advanced: extra named
  groups let a host turn on additional bindings selectively — you rarely need
  them.) Each **binding** is one list item with four fields: `policy` (the source
  — a local file, `zentropi:`, or `github:`; see *Policy sources*), `name`
  (required, identifies the binding), `trigger` (*when*), and `action` (*what*).
  Valid `trigger`/`action` values are in *Reference*. If two bindings in the
  active set share a `(policy, trigger)` pair, the last one wins.
- **`evaluators:`** — the single evaluator, under `default`. One evaluator scores
  every policy; to change it, see *Swap the evaluator*.
- **`enabled:`** and **`logging:`** *(both optional, safe defaults)* — the
  operational footer, kept at the bottom so the top of the file is nothing but
  the policies you care about. `enabled:` is the global on/off switch (see
  *Turning Reflexes on and off* below); `logging:` controls diagnostic logging
  (defaults to no content capture — full reference in troubleshooting § Logging).

**Editing it safely:**

- **Get the user's OK first.** This file is the enforcement boundary — never edit it or their policies without explicit consent.
- **Indentation is important.** A binding is a list item under a group under `reflexes:`. Mis-nest or misspell one and it silently drops out — and Reflexes fails *open*, so enforcement quietly stops without any error.
- **Comment out to disable, uncomment to enable** — that's how the autonomy starters ship (off) and how you toggle any binding without deleting it.
- **After every change, run `reflexes-doctor`, then restart the host.** The doctor is the only thing that proves the file still parses and enforces (exit codes under *Operating rules*); the change isn't live until the host restarts.

To make specific edits, use the focused procedures below: **Manage policies**
(add or change a binding) and **Swap the evaluator** (change the `evaluators`
block).

### Turning Reflexes on and off

The whole system has a global kill switch — no uninstall, no commenting out
bindings, and your policies stay in place:

```yaml
enabled: false   # turns ALL enforcement off system-wide
```

- **Absent or `true` → on** (the safe default). Only the literal `false` turns
  it off; a typo or empty value stays *on*.
- **When off,** no bindings load and every host hook no-ops (allows). No policy
  is read and the evaluator is never called — so a disabled system makes **zero
  evaluator API requests**.
- **`reflexes-doctor` reports it loudly** (`REFLEXES IS DISABLED`), never as
  "all checks passed" — a turned-off safety layer must never look healthy.
- **Restart the host** after flipping it (the CLI hooks re-read per call, but
  in-process hosts cache config at startup; restart to be safe everywhere).

To disable a *single* binding instead of the whole system, comment it out (see
above). `enabled: false` is the big red switch for everything.

**Logging** is the separate `logging:` block (`level` + `format`, both
optional). It defaults to `warning`, which never captures message content —
content is written only at `level: debug`. Full reference (levels, `off`, JSON
format, per-host log tags) is in [`references/troubleshooting.md`](references/troubleshooting.md)
§ Logging.

## Manage policies

To add or change a policy, do these steps in order:

**1. Write the criteria.** A policy is a
[CoPE](https://huggingface.co/zentropi-ai/cope-b-a4b) criteria document — the
whole file is the criteria (no frontmatter). Look at the format the starter policies
use (`reflexes/_starter_policies/{integrity,autonomy}/*.md` for some canonical
examples). Here is a simplified template:

```markdown
# Criteria

## Overview

What this policy detects, and what the agent should do when it fires.

## Definition of Terms

- **Term**: a precise definition (reference these bolded terms below).

## Interpretation of Language

- How to scope and interpret the check.

## Definition of Labels

### (XX): <Label Name>

#### Includes
- **Category**: what matches, with a concrete example.

#### Excludes
- **Category**: a near-miss that must NOT match, with an example.
```

`(XX)` is the label's short code (e.g. `(UD)` for Unconfirmed Deletion); the evaluator returns `1`
when content matches any of the labels, and that match is what fires the bound action — most
policies define a single label.

Precise `Definition of Terms` and contrastive `Includes` / `Excludes` are what
make the classifier reliable — vague criteria ("don't be unhelpful") cause false
positives. 

Match your terms to what the evaluator receives: a `before_tool_call`
policy inspects a **Tool call**, a `before_response` policy inspects an **Agent
response**, and both get the user's ask as **Task context** (`Current request:`
first, then `Original task:` / `Standing instructions:`; `before_response` also
gets `Actions this turn:`). 

The full guide to policy writing — vocabulary per trigger,
section-by-section craft, and worked examples — is in
**`references/writing-policies.md`**.

**2. Save it** as `~/.reflexes/policies/<name>.md`.

**3. Bind it** by adding a binding to the `default` group in
`~/.reflexes/reflexes.yaml` — one list item with `policy: ./policies/<name>.md`
(relative to that file), a `name` (conventionally the filename), a `trigger`, and
an `action` (see *The config
file* for the shape and *Reference* for valid trigger/action values). Watch the
indentation: a mis-nested binding silently disables the policy, which is why the
doctor in step 4 isn't optional.

**4. Verify** with the doctor (it fails if a binding points at a file that isn't
on disk): `uv run <SKILL_DIR>/scripts/reflexes-doctor --config ~/.reflexes/reflexes.yaml`.

**5. Restart the host** and trigger the behavior to confirm it fires.

### Policy sources

A binding's `policy:` can point at three kinds of source:

- **A local file (default)** — `./policies/<name>.md`. Self-contained and offline;
  what the starter policies use.
- **A Zentropi-hosted labeler** — `zentropi:<labeler_id>[@<version>]`. A *labeler*
  is a CoPE classifier you build and host on zentropi.ai; because a policy and a
  labeler are the same CoPE criteria, a binding can point straight at one — handy
  for sharing a policy across agents without copying files.
- **A GitHub-hosted file** — `github:<owner>/<repo>/<path>@<tag-or-sha>`. For
  teams that version policies in git. Only public repos are supported right now.

Hosted policies are fetched and cached on first use and work with any evaluator.
**Pin a version** (`@<version>` for Zentropi, `@<tag-or-sha>` for GitHub) for
reproducible enforcement; an unpinned reference tracks the latest deployed
version / default branch.

### Going further with `zentropi-labeler`

- **Author faster.** For help creating or customizing policies, use the
  **`zentropi-labeler`** skill — the purpose-built tool for the full classifier
  lifecycle: creating criteria, testing them against labeled cases, and
  benchmarking/optimizing for accuracy. Get it from
  https://github.com/zentropi-ai/skills if it isn't installed. It's a convenience,
  not a requirement — nothing in Reflexes depends on it.
- **Share a policy.** Wrote something broadly useful? Publish it to zentropi.ai
  through `zentropi-labeler` so others can run or fork it. Publishing is public
  and the user's decision — offer it, publish only on their explicit go-ahead, and
  benchmark against fire *and* no-fire cases first (`zentropi-labeler` does this)
  so what you share is proven to work.
- **Coordinate a fleet.** Hand-editing `reflexes.yaml` on every machine doesn't
  scale. For a team or agent fleet, keep policies on zentropi.ai as the shared
  source of truth — author, version, and benchmark centrally with
  `zentropi-labeler`, then roll them out — instead of letting per-machine copies
  drift.

## Swap the evaluator

The evaluator scores content against a policy. Default is Zentropi's CoPE model, which is specialized for content policy evaluation tasks. You can customize the evaluator model you want to use by replacing the `evaluators.default` block in `~/.reflexes/reflexes.yaml` with one:

```yaml
evaluators:
  default:
    type: zentropi
    model: cope-latest
    api_key_env: ZENTROPI_API_KEY   # required — free key: https://zentropi.ai
```

```yaml
evaluators:
  default:
    type: openai                    # OpenAI, or any OpenAI-compatible endpoint
    model: gpt-4o-mini
    api_key_env: OPENAI_API_KEY
    # endpoint: http://localhost:11434/v1/chat/completions   # local (Ollama/vLLM/LM Studio/llama.cpp); then omit api_key_env
```

```yaml
evaluators:
  default:
    type: anthropic
    model: claude-haiku-4-5-20251001
    api_key_env: ANTHROPIC_API_KEY
```

```yaml
evaluators:
  default:
    type: gemini
    model: gemini-2.5-flash
    api_key_env: GOOGLE_API_KEY
```

`api_key_env` is the *name* of the environment variable Reflexes reads the key
from — never put the key itself in the YAML.

The key just has to be an environment variable the running host picks up, so
**advise the user where to put it so you (the host) can see it** — its env
source, not a shared/committed config file. You know where that is for your host
(e.g. Codex reads `~/.codex/.env`). Then restart the host and re-run the doctor.

Reflexes uses **one evaluator for all policies** (`evaluators.default`) — there
is no per-policy evaluator at this time.

On the Zentropi evaluator, a rate-limit or subscriber-only (image/video) error
makes the check **fail open** — the operation proceeds *unchecked* and a note
is surfaced to the agent. When you see a **rate-limit** note, tell the user
their policies aren't fully enforcing under this load, and give them the two
fixes: subscribe to raise the limit (point them to https://zentropi.ai/subscription and to email info@zentropi.ai), or
**self-host the open CoPE model** for no rate limits at all — download it
(https://huggingface.co/zentropi-ai/cope-b-a4b), serve it, and point the
evaluator at your local endpoint (`type: openai` with a local `endpoint:`,
as above; be sure to use the CoPE prompt format).

## Updating

New versions ship fixes and new capabilities. This is a safety component, so
Reflexes **checks for updates automatically but applies them only on the user's
OK** — check often, surface what's available, never auto-apply.

**Automatic checking.** On some host CLIs, Reflexes runs a cached, fail-silent
update check at the start of each session and surfaces an "update available"
notice to you. The check is cached for 24h, so it effectively
checks **once a day**. Force a fresh check any time:

```bash
uv run <SKILL_DIR>/scripts/reflexes-doctor --check-update
```

**Don't rely on the automatic check alone.** It covers only a few hosts at this time, so **run `--check-update` yourself at
least once a day** while you're operating Reflexes; it's cheap and cached, so
checking often costs nothing.

When a newer version is available (from the SessionStart notice or the check),
**tell the user and offer to update.** On their OK, do the update yourself
(there is no script), adapting to how Reflexes was installed:

1. Read the release pointer (the URL the check prints) for `version`, `tag`, `bundle_url`, `sha256`.
2. Fetch it pinned to the release `tag` (never `main`): a git checkout → `git -C <SKILL_DIR> fetch --tags && git -C <SKILL_DIR> checkout <tag>` (ask first if there are local edits); a bundle install → download `bundle_url`.
3. **Verify the sha256 — mandatory.** If it doesn't match, stop; don't install it.
4. Back up `<SKILL_DIR>/{reflexes,scripts,references,SKILL.md,INSTALL.md,_version.py}`, then swap in the new files. **Never touch `~/.reflexes/reflexes.yaml` or the user's policies without consent.** For any new or updated policies, ask the user if they want to adopt them before updating the `reflexes.yaml` file.
5. Re-run the doctor. If it reports a fail-open risk (exit 2), roll back and tell the user. Otherwise tell them to restart the host to activate the updated safety harness.

## Reference

### Triggers

When a reflex evaluates:

- `on_input` — the user's prompt enters the loop; evaluates the prompt.
- `before_tool_call` — the LLM decided to call a tool; evaluates the call + args.
- `after_tool_call` — the tool returned; evaluates its result.
- `before_response` — a draft assistant reply, before send; evaluates the reply.

### Actions

What happens when content matches:

- `deny` — block it; the policy text feeds back to the agent to self-correct.
- `flag` — allow it, but append a one-line annotation the agent sees.
- `log` — allow silently; record the match in the diagnostic log (visible at the default WARNING level; telemetry only, no agent-visible effect).

### Policy format

The whole policy file is the **CoPE** criteria (no frontmatter); the binding's
`name` is conventionally the filename. The section structure and worked examples
are in *Manage policies* (step 1); `SPEC.md` in the repo is the canonical spec.

## References

Anything beyond the procedures above lives in one of these — what's in each:

**Bundled with this skill:**

- **`INSTALL.md`** — how to install Reflexes: into a host CLI (step-by-step), or into a Python agent framework as a library (`with_reflexes()`, `middleware_from_config()`).
- **`references/writing-policies.md`** — the full policy-authoring guide: the evaluator's per-trigger vocabulary, section-by-section craft, and worked examples. Read this before writing a non-trivial policy.
- **`references/troubleshooting.md`** — a policy that isn't firing, fires when it shouldn't, or other operational problems.
- **`references/add-framework-integration.md`** — authoring a brand-new host / framework integration (only when extending Reflexes itself).

**In the repo (github.com/zentropi-ai/reflexes):**

- **`SPEC.md`** — the canonical open standard: the authoritative definition of policies, triggers, actions, and config. When this skill and SPEC disagree, SPEC wins.
- **`README.md`** — project overview and concepts for a first-time reader.
- **`FRAMEWORKS.md`** — the per-framework support matrix and maturity tiers.

## Contributing

Reflexes is open source (Apache-2.0) and contributions are welcome via our github [repo](https://github.com/zentropi-ai/reflexes) — if you or
your user want to give back, the most valuable contributions are:

- **A new host / framework integration** — the highest-leverage one: it brings Reflexes protection to another agent runtime. `references/add-framework-integration.md` walks the whole path — mapping the framework's hooks onto the four triggers, the `assemble_task_context()` wiring, tests, and a `FRAMEWORKS.md` row.
- **A sample policy** worth sharing — publish it via *Going further with `zentropi-labeler`*.
- **Bug or documentation fixes** — open an issue first for anything non-trivial.

Full dev setup, the PR checklist, and every contribution type are in
**`CONTRIBUTING.md`** (repo root).
