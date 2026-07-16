# Writing Reflexes policies

A policy is a **CoPE criteria document**: the whole file is the criteria sent
to the evaluator, and the policy name is the filename (SPEC §3). This guide
covers two things — (1) the exact vocabulary Reflexes feeds the evaluator, so
your criteria name the right things, and (2) how to write criteria that
classify reliably. The bundled starter policies in
`reflexes/_starter_policies/` (the `integrity/` and `autonomy/` bundles) are
worked examples of everything below.

## What the evaluator sees

At runtime Reflexes hands the evaluator your **policy**, the **thing being
evaluated**, and the **Task context** for reference, framed like this:

```
Evaluate the {WHAT} below against the policy. The Task context is provided for
reference — evaluate the {WHAT}, not the Task context.

Task context: {context}

{WHAT}: {content}

Policy:
---
{your criteria}
---
```

`{WHAT}` depends on the binding's `trigger`. Write your **Definition of Terms**
to name the exact thing your policy inspects — it should match `{WHAT}`:

| Trigger            | `{WHAT}` (what to name) | `{content}` the evaluator scores |
|--------------------|-------------------------|-----------------------------------|
| `on_input`         | **Content**             | the user's prompt text |
| `before_tool_call` | **Tool call**           | `{"tool_name": ..., "args": {...}}` — the pending call |
| `after_tool_call`  | **Tool result**         | `<tool> returned: <result>` |
| `before_response`  | **Agent response**      | the agent's draft reply text |

So a `before_tool_call` policy inspects a **Tool call**; a `before_response`
policy inspects an **Agent response**. Use that term — it's what the evaluator
is told it's scoring.

## The Task context vocabulary

For the tool and response triggers, the reference material is the **Task
context** — the current request first (the load-bearing signal), each section
self-labeled:

- **`Current request:`** — the user's most-recent prompt. This is the
  load-bearing signal — the request, constraint, or confirmation to check
  against. When you mean "did the user ask for / permit this", point here.
- **`Original task:`** — the first user prompt of the session (background).
- **`Standing instructions:`** — CLAUDE.md / AGENTS.md / the agent's system
  instructions (background).
- **`Actions this turn:`** (`before_response` only) — a step list of what the
  agent *actually did* this turn: its tool calls and results. Use it to check a
  response's claims against reality (e.g. "the response says it ran the tests —
  do the actions this turn show a test run?").

Rendered, the context handed to the evaluator looks like:

```
Current request:
delete the old migration files

Standing instructions:
<project CLAUDE.md, truncated>
```

Name these sections in your criteria. "Check whether the **Current request**
authorized the deletion" beats "check whether the user asked" — it points the
evaluator at the exact section it was handed.

## Policy structure (CoPE)

```markdown
# Criteria

## Overview
<what this detects; what the agent should do when it fires>

## Definition of Terms
- **Term**: <precise definition, referenced verbatim below>

## Interpretation of Language
- <scoping rules; how to read edge cases>

## Definition of Labels

### (XX): <Label Name>

#### Includes
- **Category**: <what matches, with a concrete example>

#### Excludes
- **Category**: <a near-miss that must NOT match, with an example>
```

Section by section:

- **Overview** — one paragraph: what this detects, and the *correction* the
  agent should make when it fires (the deny feedback is this whole document, so
  the correction has to be in the text).
- **Definition of Terms** — bold every load-bearing term and define it once.
  Name the `{WHAT}` (**Tool call** / **Agent response**) and the Task-context
  sections you rely on (**Current request**, **Actions this turn**). Reference
  the bolded terms verbatim in the labels.
- **Interpretation of Language** — how to scope the check; what tips an edge
  case each way.
- **Definition of Labels** — `### (XX): <Name>` with a 2-letter code, then:
  - **#### Includes** — categories that MATCH, each a **bold name**: concrete
    example. Sequence clearest → subtlest.
  - **#### Excludes** — near-misses that must NOT match, each with an example.
    The gap between an Includes category and its contrasting Exclude is the
    decision boundary — that contrast is what suppresses false positives.

## Principles

- **Observable content only.** Classify what's in the `{WHAT}` and Task context,
  never intent you can't see.
- **Includes AND Excludes.** Every Includes category deserves a contrasting
  Exclude. Excludes are not optional — they draw the boundary.
- **Bold terms consistently.** Same term, same bolding, every occurrence.
- **An example per category.** Concrete `commands` / quotes beat abstract rules.
- **Determinism.** Two evaluators reading the same content should reach the same
  label. If a category is subjective, tighten it or move it to Excludes.

## Two worked shapes

**A `before_tool_call` guard** inspects the pending **Tool call** and checks it
against the **Current request** — e.g. *Unconfirmed-Destructive-Action* fires
when the **Tool call** would irreversibly destroy local data and the **Current
request** neither asked for nor confirmed it. "User requested it"
is an Exclude, so a direct `delete X` request does not fire.

**A `before_response` check** inspects the **Agent response**, often against
**Actions this turn** — e.g. *Early-Stopping* fires when the response claims
completion but the **Current request** or **Actions this turn** shows unaddressed
items.

Read the six starter policies for full instances of both shapes.

## Validate

Bind the policy, then:

1. `reflexes-doctor --config <file>` — confirms the config parses, the policy is
   on disk, and the evaluator answers a live probe.
2. Check one true case and one false case actually land `label=1` / `label=0` —
   a policy that never fires (or always fires) is worse than none.

## Source

The CoPE criteria format is the same one the Zentropi labeler uses; this guide
adds the Reflexes runtime vocabulary (`{WHAT}` per trigger, the Task-context
slots). `SPEC.md` §3 is canonical for the format.
