---
name: Bug report
about: A policy that should fire but doesn't, a crash, or other unexpected behavior
title: ""
labels: bug
assignees: ""
---

## What happened

<!-- A clear description of the bug. If a policy didn't fire as expected,
say what you expected to be caught and what happened instead. -->

## Environment

- **Host CLI + version** (e.g. Claude Code x.y.z, OpenAI Codex x.y.z, or the framework/SDK + version):
- **Reflexes version** (`pip show reflexes`, or the plugin version):
- **OS / Python version**:

## Reproduction details

Please gather the following (see the troubleshooting guide,
`skill/references/troubleshooting.md` §7):

- **Policy slug** (the policy that should have fired):
- **Trigger configured** (`on_input` / `before_tool_call` / `after_tool_call` / `before_response`):
- **Content you expected to catch** (the exact input, tool call, or response text):
- **Evaluator type** (from `reflexes.yaml`: `zentropi` / `openai` / `anthropic` / `gemini`):

## Debug log excerpt

<!-- Set REFLEXES_LOG_FORMAT=json, restart your host, reproduce, and paste
the relevant `reflexes.*` log records here. REDACT API keys and secrets. -->

```
<paste debug log here>
```

## Anything else

<!-- Additional context, screenshots, or a redacted reflexes.yaml. -->
