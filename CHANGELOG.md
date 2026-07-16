# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0]

Initial public release.

### Added

- **Policy-as-markdown + `reflexes.yaml` model.** Policies are plain
  markdown files (YAML frontmatter + a `## Policy` criteria section);
  `reflexes.yaml` is the single source of truth binding each policy to a
  trigger and action. Named policy groups (with a default group) allow
  opt-in policy sets.
- **Four triggers** — `on_input`, `before_tool_call`, `after_tool_call`,
  and `before_response` — covering every agent decision point.
- **Three actions** — `deny` (block and surface the policy text to the
  agent so it can self-correct), `flag` (allow, but make the verdict
  observable), and `log` (allow, log at INFO).
- **Self-correction protocol.** On `deny`, the policy criteria text is
  fed back to the agent through the framework's native rejection
  mechanism so it revises on its next turn.
- **Evaluators.** Zentropi CoPE as the default (`type: zentropi`), plus
  OpenAI-compatible (`type: openai`, including local Ollama / LM Studio /
  vLLM / llama.cpp / TGI endpoints), Anthropic (`type: anthropic`), and
  Gemini (`type: gemini`). CoPE can also be self-hosted via the
  OpenAI-compatible evaluator pointed at a local endpoint.
- **Fail-open semantics.** When the evaluator is unreachable (network
  outage, rate limit), no action fires and the agent keeps running.
- **Host and framework integrations.** Claude Code and OpenAI Codex
  (agent-driven install via `SKILL.md`), NousResearch Hermes, OpenClaw
  (`@zentropi/reflexes-openclaw` plugin), plus the Python SDKs: OpenAI
  Agents SDK, Claude Agent SDK, Google ADK, and LangGraph.
- **Six starter policies** across the `integrity` and `autonomy`
  bundles: Constraint-Violation, Unconfirmed-Destructive-Action,
  Sycophancy, Early-Stopping, Laziness-Detection, and Unsourced-Claims.
- **Policy sources.** A binding's `policy:` can reference a local file
  (`./policies/x.md`, the default), a Zentropi-hosted labeler
  (`zentropi:<labeler_id>[@<version>]`), or a GitHub-hosted file
  (`github:<owner>/<repo>/<path>@<tag-or-sha>`). Hosted policies are fetched
  and cached (pinned refs permanently, unpinned on a TTL) and work with any
  evaluator. Each binding now requires a `name`.
- **OpenTelemetry observability** (optional `[otel]` extra): the
  `reflexes.evaluate` span, the `reflexes.verdicts` counter, and the
  `reflexes.latency` histogram, plus JSON structured logging via
  `REFLEXES_LOG_FORMAT=json`.
- **`reflexes doctor`** command to verify an installation (config
  parses, evaluator reachable, hooks present).

[0.1.0]: https://github.com/zentropi-ai/reflexes/releases/tag/v0.1.0
