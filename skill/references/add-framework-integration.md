# Add a New Framework Integration

This is the contributor walkthrough for adding a new framework
binding to Reflexes. Referenced from SKILL.md's **Add a New
Framework Integration** section.

Adding a new integration is a Python module under
`reflexes/integrations/<framework>.py` plus a row in
`FRAMEWORKS.md`. The skeleton is small (~100 lines for most
frameworks) once you understand how the framework's hook protocol
maps onto Reflexes' four-trigger taxonomy.

---

## 1. Read the existing integrations as templates

`reflexes/integrations/` has 11 working integrations. Pick the
closest analogue to the framework you're adding:

| If your framework is... | Read |
|---|---|
| A subprocess host CLI (reads JSON on stdin, returns JSON on stdout) | `claude_code.py` or `openai_codex.py` |
| Python in-process with callback hooks | `google_adk.py` or `langgraph.py` |
| Python in-process with Colang-style flows | `nemo_guardrails.py` |
| Python in-process via lifecycle observer | `nous_hermes.py` |
| Python SDK with explicit `with_X(agent)` wrap | `openai_agents.py` or `claude_agent_sdk.py` |

The Hermes integration is the most defensive — it probes for the
framework's per-event context schema and falls back gracefully if
field names aren't where it expects. Use it as a reference for
frameworks with under-documented internals.

---

## 2. Map the framework's hook protocol to Reflexes' four triggers

Reflexes has four triggers — see SKILL.md → Reference for the
canonical list. Your integration's job is to bind the framework's
hooks to those four triggers and pass the right content + context
to the shared `assemble_task_context()` helper.

| Reflexes trigger | What you evaluate |
|---|---|
| `on_input` | the user's most recent prompt text |
| `before_tool_call` | the tool name + args, as JSON: `{"tool_name": "...", "args": {...}}` |
| `after_tool_call` | `{tool_name} returned: {result}` |
| `before_response` | the assistant's draft message text |

Frameworks differ in what they expose at each lifecycle event. Some
(Claude Code, Codex, Google ADK) have native hooks at all four;
some (Hermes) only have a single per-turn observer and synthesize
the four from `agent:step` context introspection.

---

## 3. Mandatory: route user-context through `assemble_task_context()`

Every integration MUST send `before_tool_call`, `after_tool_call`,
and `before_response` content through the shared
`reflexes._context.assemble_task_context()` helper, which renders
the self-describing task-context labels.

```python
from reflexes._context import assemble_task_context

content_text, _ = assemble_task_context(
    primary_user_prompt=user_message,
    standing_instructions=...,        # CLAUDE.md / agent instruction, if available
    initial_user_prompt=...,          # session-initial prompt, if available
    primary_content=tool_call_payload,  # the thing being evaluated
)
```

The structural test `tests/test_content_labeling_parity.py` asserts
your integration imports `assemble_task_context` and uses it.
Skipping this helper means policies behave differently on your
framework than on the Stable-tier integrations — and the parity test will fail.

Standing instructions and initial-user-prompt slots are optional —
omit them if your framework doesn't expose them. The current-request
slot is the load-bearing piece.

---

## 4. Document any asymmetries honestly

If your framework can't support full deny at every trigger, document
the asymmetry. Hermes is the canonical example: `before_tool_call`
deny becomes one round-trip late because Hermes' `agent:step`
observer fires after the tool already ran. The Hermes integration
makes this explicit in three places:

- The structured return value carries an `asymmetry_note` field.
- The log line is annotated `[deny-on-next-turn ...]`.
- FRAMEWORKS.md → "Hermes" section explains the asymmetry.

If a deny path can't be made full on your framework, do the same:
flag and log fire fully, document the deny asymmetry, surface it
in observability.

---

## 5. Wire the integration entry point

Three patterns by framework type:

### Subprocess host (Claude Code / Codex)

The integration is a `__main__` runnable: `python3 -m reflexes.integrations.<framework>`. It reads JSON on stdin, dispatches to the right trigger, returns JSON on stdout. See `claude_code.py` for the dispatcher shape.

### Python in-process with native callbacks

Expose a `with_reflexes(agent, *, config=...)` function that wraps the user's agent object and registers the framework's native callbacks (e.g. `agent.add_callback("before_tool_call", ...)` for Google ADK). See `google_adk.py`.

### Python in-process via middleware / graph state

Expose a `middleware_from_config(config_path)` function returning the framework's middleware object. See `langgraph.py`.

---

## 6. Add tests

Three required tests:

1. **Content-labeling parity** — pick one of:
   - Your integration's `before_tool_call` payload matches the
     canonical JSON shape `{"tool_name": "...", "args": {...}}`.
   - Your integration's user-context goes through
     `assemble_task_context` (self-describing task-context labels).
   - Add an assertion to `tests/test_content_labeling_parity.py`.

2. **Unit tests for the dispatcher** — fixture an event payload,
   feed it through your integration entry point, assert the right
   trigger fires with the right content. See
   `tests/test_nous_hermes.py` for the pattern.

3. **End-to-end smoke** — if your framework is a core (Stable-tier) integration,
   add an end-to-end test that drives your integration entry point
   with a synthetic evaluator (returns label=1 stub) and asserts the
   deny path triggers. See `tests/test_codex_e2e.py` and
   `tests/test_claude_code_e2e.py` for the subprocess-host pattern
   (fixtured `reflexes.yaml` + starter policy, no example app needed).

---

## 7. Update `FRAMEWORKS.md`

Add a row to the at-a-glance matrix table. If your framework needs
non-trivial documentation (asymmetries, defensive context probing,
unusual content shapes), add a per-framework section under "What
we send to the evaluator." Include:

- Hook protocol summary (one paragraph).
- The four-trigger mapping table (content sent + prepended context
  per trigger).
- Any asymmetries vs the core integrations.
- Implementation + testing state row in the bottom table.

---

## 8. Run the full test suite

```bash
pytest tests/
```

All tests must pass. The structural parity tests catch most
integration mistakes (wrong content shape, missing
`assemble_task_context` call, drifted trigger taxonomy). Behavioral
equivalence tests catch the rest.

---

## 9. Open a PR

PR description should answer:

- What framework? Version range supported?
- Which Reflexes triggers does it bind?
- Are all four full-deny, or are there asymmetries? (If asymmetric,
  why — does the framework's hook protocol just not expose a
  pre-tool-call event, etc.?)
- Live runtime tested or unit-test-only?

Maintainers will check the FRAMEWORKS.md row, the integration code,
and the test coverage. Live-runtime confirmation usually lands in a
follow-up revision (the core integrations were each validated
through repeated live testing; new integrations earn that status
incrementally).
