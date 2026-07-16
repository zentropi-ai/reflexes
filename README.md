# Reflexes

**Reflexes is an open safety harness for AI agents.** At inference-time it checks each agentic decision point against plain-English policies to keep agents on track. It is an open standard and ships with this reference implementation.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)

> **Status: v0.1 — first public release.** Claude Code, OpenAI Codex, LangGraph,
> and Google ADK are Stable; other hosts are Beta or Experimental — see
> [`FRAMEWORKS.md`](./FRAMEWORKS.md) for per-host maturity.

## Contents

**Getting started**
- [How it works](#how-it-works)
- [Quick Install — via Agents](#quick-install--via-agents)
- [Quick Install — via Python Frameworks](#quick-install--via-python-frameworks)
- [What you get out of the box](#what-you-get-out-of-the-box)

**Using Reflexes**
- [Configuration](#configuration)
- [Writing a policy](#writing-a-policy)
- [Triggers and actions](#triggers-and-actions)
- [Evaluators](#evaluators)
- [Supported frameworks](#supported-frameworks)

**Reference**
- [Observability](#observability)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## How it works

AI agents drift: they run a destructive command, overstep the instructions you
gave, assert something they never verified, or stop before the job is done — and
you often find out only after the tool has run or the reply has shipped. Reflexes
is the safety net. You write the rules in plain English, and it checks the agent
against them at each decision point, catching a mistake *before* it lands and
handing the agent the rule so it can correct itself — no babysitting required.

Reflexes has five key components, wired together in a `reflexes.yaml` file:

- **Policy** — a rule written in plain English as a markdown file (e.g. "don't
  delete data without confirmation"). The whole document is the criteria.
- **Trigger** — *when* a policy is checked: `on_input` (a user message),
  `before_tool_call`, `after_tool_call`, or `before_response`.
- **Action** — *what happens* on a match: `deny` (block, and hand the policy text
  back so the agent self-corrects), `flag` (allow but record it), or `log`.
- **Binding** — a line in `reflexes.yaml` tying a policy to a trigger and an
  action. Your bindings *are* your configuration.
- **Evaluator** — the classifier that decides whether content matches a policy.
  The default is **[CoPE](https://huggingface.co/zentropi-ai/cope-b-a4b)**, a small model purpose-built for it; you can also use
  OpenAI, Anthropic, or Gemini.

At each **trigger**, Reflexes asks the **evaluator** whether the
content matches each bound **policy**, and on a match takes the **action** —
usually `deny`, which feeds the policy back so the agent corrects its own course.

By telling an agent *which* policy it broke and *why*, Reflexes help agents revise and adapt,
instead of just looping against a wall or breaking one of your rules.

## Quick Install — via Agents

Reflexes can install itself. **Just tell your coding agent to install the Reflexes
skill:**

> Install the reflexes skill from github.com/zentropi-ai/reflexes then wire it into this agent.

Your agent reads `SKILL.md` and does the rest for your host (Claude Code, OpenAI
Codex, Hermes, or OpenClaw): detects the host, installs the starter policies,
writes `~/.reflexes/reflexes.yaml`, and wires the hooks without disturbing your
existing config — then verifies with `reflexes doctor`. It's idempotent, so you
can re-run it any time.

Prerequisites: Python 3.10+, [`uv`](https://docs.astral.sh/uv/), and — for the
default CoPE evaluator — a free `ZENTROPI_API_KEY` from
[zentropi.ai](https://zentropi.ai).

**Building an agent in code** with a Python framework (OpenAI Agents SDK,
LangGraph, Google ADK, …) rather than a host CLI? `pip install reflexes` and wire
it in — see [Quick Install — via Python Frameworks](#quick-install--via-python-frameworks).

## Quick Install — via Python Frameworks

First, install the package:

```bash
pip install reflexes
# the OpenAI Agents SDK integration also needs the `openai` extra:
pip install "reflexes[openai]"
```

For OpenAI Agents SDK and Google ADK:

```python
from reflexes import with_reflexes

agent = ...  # your framework's agent object
agent = with_reflexes(agent, reflexes=["default"], config_file="reflexes.yaml")
```

For LangGraph:

```python
from langchain.agents import create_agent
from reflexes import middleware_from_config

agent = create_agent(
    model=...,
    tools=[...],
    middleware=[middleware_from_config(reflexes=["default"], config_file="reflexes.yaml")],
)
```

For Claude Agent SDK (use `create_hooks()`, not `with_reflexes()`):

```python
from claude_agent_sdk import ClaudeAgentOptions, query
from reflexes.integrations.claude_agent_sdk import create_hooks

options = ClaudeAgentOptions(
    hooks=create_hooks(reflexes=["default"], config_file="reflexes.yaml"),
)
```

## What you get out of the box

The install ships six **starter policies**, ready to use, in two groups:

- **Integrity** (enabled by default):
  - `Unconfirmed-Destructive-Action` — don't delete or destroy data without
    confirmation.
  - `Constraint-Violation` — don't overstep the user's stated instructions.
  - `Sycophancy` — give honest judgment, not flattery.
- **Autonomy** (installed, off by default — uncomment in `reflexes.yaml` to
  enable): `Early-Stopping`, `Laziness-Detection`, `Unsourced-Claims` — help a
  long-running agent actually finish the job and ground its claims.

**Where to go next:**

- **Pick your policies** — edit `~/.reflexes/reflexes.yaml` to enable/disable policies
  or change an action (e.g. `deny` → `flag`). See [Configuration](#configuration).
- **Write your own** — drop a markdown policy in `~/.reflexes/policies/` and bind
  it. See [Writing a policy](#writing-a-policy).
- **Swap the evaluator** — use OpenAI / Anthropic / Gemini, or self-host CoPE.
  See [Evaluators](#evaluators).

## Configuration

`reflexes.yaml` is the single source of truth for which policies fire
where. Here is an example:

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

# --- Operational settings (optional; safe defaults) ---
enabled: true      # global on/off; false disables ALL enforcement. Absent = enabled.
logging:
  level: warning   # off | error | warning (default) | info | debug — content ONLY ever at debug
  format: text     # text | json
```

The `default` group always applies. Add named groups (e.g. `customer-comms`,
`code-safety`) for opt-in policy sets:

```python
agent = with_reflexes(agent, reflexes=["customer-comms", "code-safety"])
```

Same `(policy, trigger)` pair across groups uses last-writer-wins.

**Policy sources.** Each binding needs a `name` and a `policy` reference. The
`policy` can point at three kinds of source:

- **A local file (default)** — `./policies/<name>.md`. Self-contained and
  offline;
- **A Zentropi-hosted labeler** — `zentropi:<labeler_id>[@<version>]`. Useful
  when you author and version policies in Zentropi and share them across agents.
- **A GitHub-hosted file** — `github:<owner>/<repo>/<path>@<tag-or-sha>`. For
  teams that version policies in git.

Hosted policies are fetched and cached on first use, so they work with any
evaluator. **Pin a version** for reproducible enforcement; an unpinned reference
tracks the latest deployed version (Zentropi) or the default branch (GitHub).

## Writing a policy

A policy is a plain markdown file, and **the whole document is the criteria**
sent to the evaluator. CoPE is tuned for the structure the starter policies
follow:

- **`## Overview`** — one paragraph: summary of what to detect, and the correction the agent
  might make when it fires.
- **`## Definition of Terms`** — define each load-bearing term (in **bold**) once.
- **`## Interpretation of Language`** — how to scope the check and what tips an
  edge case one way or the other.
- **`## Definition of Labels`** — the label as `### (XX): <Name>` (a short code),
  split into **`#### Includes`** (categories that match) and **`#### Excludes`**
  (near-misses that must not). That Includes/Excludes boundary is what makes CoPE
  precise.

Generally, you do **not** need to write a policy on your own. **Use the Reflexes Skill within your Agent to generate a conforming policy** for whatever rule you want to build. Here is an example of a simple one:

```markdown
# Criteria

## Overview

Fires when a tool call would permanently destroy local data (files, git history,
database records) without the user having confirmed that specific action. On a
match, the agent should say what would be lost and ask before proceeding.

## Definition of Labels

### (UDA): Unconfirmed Destructive Action

#### Includes

- **Unconfirmed deletion**: deletes or wipes data (`rm -rf`, `git clean -fd`)
  with no confirmation in the task context.
- **Irreversible history/DB change**: `git reset --hard`, `DROP TABLE`, or a
  force-push, without confirmation.

#### Excludes

- **User-requested**: the request explicitly asked for this exact destruction.
- **Reversible or regenerable**: moves to backup/trash, `git stash`, or deletes
  only rebuildable artifacts (`node_modules`, `dist/`).
```

See [`writing-policies.md`](./skill/references/writing-policies.md) for the full
method and worked examples, and [`SPEC.md`](./SPEC.md) §3 for the format spec.

## Triggers and actions

| Trigger | Fires when | Content sent to evaluator |
|---|---|---|
| `on_input` | The agent receives a user input | The user input |
| `before_tool_call` | The agent is about to invoke a tool | `tool_name(args)` |
| `after_tool_call` | A tool returned a result | `tool_name returned: <result>` |
| `before_response` | The agent is about to emit a final response | The response text |

| Action | Behavior on `label=1` |
|---|---|
| `deny` | Block the operation. Surface the policy criteria text to the agent so it can self-correct. |
| `flag` | Allow the operation. Make the verdict observable (logs, structured metadata, framework-native state). |
| `log` | Allow the operation. Record the verdict in the diagnostic log (visible at the default level). |

See [`SPEC.md`](./SPEC.md) §5–§7 for normative semantics.

## Supported frameworks

Each host's maturity tier below summarizes how thoroughly the
integration is validated. This is an **abridged** table of the most-used
hosts; see [`FRAMEWORKS.md`](./FRAMEWORKS.md) for the full host list
(including PydanticAI, smolagents, AutoGen, and NeMo Guardrails) and the
per-host evidence — live-runtime tests, dogfood passes, and known framework
limits.

| Framework | Wiring | Maturity |
|---|---|---|
| LangGraph | `middleware_from_config()` | 🟢 Stable |
| Claude Code | `SKILL.md` agent-driven install (settings.json) | 🟢 Stable |
| OpenAI Codex | `SKILL.md` agent-driven install (hooks.json) | 🟢 Stable |
| NousResearch Hermes (≥0.18) | `hooks:` in `~/.hermes/config.yaml` → `nous_hermes_shell` | 🟡 Beta — `before_tool_call` blocks; `on_input`/`after_tool_call`/coding-turn limits |
| OpenClaw | `@zentropi/reflexes-openclaw` plugin (`before_tool_call` + `before_response`) | 🟡 Beta |
| Google ADK | `with_reflexes()` (callbacks) | 🟢 Stable |
| Claude Agent SDK | `create_hooks()` | 🟡 Beta |
| OpenAI Agents SDK | `with_reflexes()` + `run_with_reflexes()` | 🟡 Beta |

## Evaluators

The default evaluator model is **[Zentropi CoPE](https://huggingface.co/zentropi-ai/cope-b-a4b)** (sub-second latency with generous free tier) —
[purpose-built for policy-steerable classification](https://arxiv.org/abs/2512.18027). It requires a free API
key: create an account at https://zentropi.ai, mint a key, and set
`ZENTROPI_API_KEY` in the environment your agent runs in.

- **Free tier** — a free key covers the starter policies, your own custom
  policies, and everyday use.
- **Subscriber tier** — high-volume usage and higher rate limits.

**Privacy:** the Zentropi API retains zero content by default — the policy
and content you send for evaluation are not stored.

Prefer not to use Zentropi? Point Reflexes at any OpenAI-compatible model
instead (see below) — no Zentropi key required.

Other evaluators are supported out of the box:

- `type: openai` — any OpenAI-compatible endpoint (GPT-4o-mini, Groq, etc.)
- `type: anthropic` — Claude as evaluator
- `type: gemini` — Gemini as evaluator

The `openai` evaluator includes **local deployments**. Anything that
exposes the OpenAI chat-completions shape on a localhost port works —
Ollama (`http://localhost:11434/v1`), LM Studio, vLLM, llama.cpp, TGI,
or your own serving stack. No `api_key` field needed for local servers
that don't require auth. Example pointing at Ollama running `llama3.1`:

```yaml
evaluators:
  default:
    type: openai
    endpoint: http://localhost:11434/v1/chat/completions
    model: llama3.1
```

When the evaluator is unreachable (network outage, rate limit), Reflexes
**fails open**: the verdict is treated as "not available," no action
fires, the agent keeps running. A governance tool that blocks production
on evaluator outage won't be adopted. See [`SPEC.md`](./SPEC.md) §4.3.

### Self-host CoPE (advanced)

For security-sensitive deployments or zero-API-dependency setups,
CoPE runs on consumer-grade GPU hardware and can be self-hosted:

- **Weights:** `zentropi-ai/cope-b-a4b` on Hugging Face
  (gated access — accept the terms; sign in once).
- **License:** `apache-2.0` — a true open weight model.
- **Hardware:** single consumer-grade GPU is sufficient for CoPE-B-A4B.
  Typically sub-100ms inference latency in production deployments.
- **Token limit:** 256K combined policy + content per evaluation.

Self-hosting gets you: lower per-check cost, no external API
dependency, end-to-end open-source inference (open SDK + open
policies + open weights). Useful where data residency or air-gapped
operation is required.

Point Reflexes at your local CoPE endpoint via the OpenAI-compatible
evaluator config:

```yaml
evaluators:
  default:
    type: openai
    endpoint: http://localhost:8000/v1/chat/completions
    model: cope-latest
```

**Roadmap (not yet shipped):** Ollama and llama.cpp builds. Until
then, run CoPE via vLLM, TGI, or your preferred OpenAI-compatible
serving stack.

## Observability

Reflexes ships optional OpenTelemetry instrumentation for production
deployments. Install the `[otel]` extra:

```bash
pip install 'reflexes[otel]'
```

Without the extra, every observability call is a no-op — zero runtime cost. With
it, each policy evaluation emits three signals to whatever OTLP backend you've
configured (Datadog, Honeycomb, Grafana/Loki, …):

- a **span** per evaluation, tagged with the policy, evaluator, trigger, and
  verdict;
- a **verdict counter**, labeled by the action taken (`deny` / `flag` / `log` /
  `error` / `no_match`) — rate-limit and auth failures are counted separately
  from evaluator errors, so dashboards can split product signal from infra noise;
- a **latency histogram** per evaluation.

For log-based stacks without OTLP ingest, set `logging.format: json` in
`reflexes.yaml` to emit structured JSON logs (and `logging.level` to control
verbosity — see [Configuration](#configuration)). The full instrumentation
surface (for custom collectors) lives in `reflexes.observability`.

## Documentation

- [`SPEC.md`](./SPEC.md) — the open standard (policy format, evaluator
  interface, trigger taxonomy, action taxonomy, self-correction protocol)
- [`FRAMEWORKS.md`](./FRAMEWORKS.md) — per-host compatibility, maturity
  tiers, and validation evidence
- [`CONTRIBUTING.md`](./CONTRIBUTING.md) — dev setup, how to add a framework
  integration or a policy, and the PR checklist
- [`SECURITY.md`](./SECURITY.md) — how to report a vulnerability privately
- `tests/` — the integration + end-to-end suite (in the source repo;
  not shipped in install bundles).

## Contributing

Contributions are welcome under Apache-2.0. Start with
[`CONTRIBUTING.md`](./CONTRIBUTING.md) — it covers dev setup, adding a
framework integration
([`add-framework-integration.md`](./skill/references/add-framework-integration.md)),
contributing a policy
([`writing-policies.md`](./skill/references/writing-policies.md)), and the PR
checklist. For security issues, follow [`SECURITY.md`](./SECURITY.md) — please
don't open a public issue for vulnerabilities.

Reflexes is stewarded by [Zentropi](https://zentropi.ai).

## License

Reflexes is licensed under the [Apache License 2.0](./LICENSE).
