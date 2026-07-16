# reflexes

The Python reference implementation of **Reflexes** — an open safety harness for
AI agents that runs inference-time policy checks. This directory is the
importable package; the full project, docs, and agent skill live in the repo.

The easiest way to properly install and configure this package is to **ask your coding agent to use the agent skill** in the `skill/` directory of the repo (https://github.com/zentropi-ai/reflexes) and then ask it to hook Reflexes into your desired agent framework.

To do this manually, you can instead install the package via pip:

```bash
pip install reflexes
# optional extras:
pip install "reflexes[openai]"   # OpenAI Agents SDK integration
pip install "reflexes[otel]"     # OpenTelemetry tracing/metrics
```

Then wire it into a Python agent framework:

```python
from reflexes import with_reflexes

agent = with_reflexes(agent, reflexes=["default"], config_file="reflexes.yaml")
```

**Installing into an agent CLI (Claude Code, Codex, …) instead?** That's an
agent-driven procedure: point your agent to the reflexes skill and it will install automatically.

Get the skill and all documentation from the source repo:

- **Repo:** https://github.com/zentropi-ai/reflexes
- **Skill (install + operate):** `skill/SKILL.md` in the repo
- **Full README / spec / framework matrix:** `README.md`, `SPEC.md`, `FRAMEWORKS.md` in the repo

Licensed under Apache-2.0.
