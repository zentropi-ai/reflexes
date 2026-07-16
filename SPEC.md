# Reflexes Specification — v0.1

*Status: Draft for v0.1.0 release. This document defines the Reflexes
standard. Implementations bearing the name "Reflexes" or claiming
compatibility MUST conform to this specification.*

## 1. Overview

Reflexes is an open standard for fast, independent policy checks on AI
agents. A Reflexes-compatible system pairs **policies** (plain-English
classification criteria, written in markdown) with an **evaluator**
(any classifier that takes a policy and a content sample and returns a
verdict). Verdicts are mapped to **actions** (deny, flag, log) at
specified **triggers** in the agent's lifecycle.

This spec defines four things:

1. The **policy document format** — what a policy looks like on disk.
2. The **evaluator interface** — what an evaluator takes and returns.
3. The **trigger taxonomy** — where in an agent's lifecycle policies fire.
4. The **action taxonomy** — what happens when a policy matches.

Implementations are free to choose their own framework integrations,
configuration layout beyond the minimum below, evaluator backends, and
distribution mechanics. Those choices are **non-normative**.

## 2. Conformance

The keywords MUST, SHOULD, and MAY are used as defined in RFC 2119.

A *Reflexes-compatible policy* is any document that conforms to §3.
A *Reflexes-compatible evaluator* is any system that conforms to §4.
A *Reflexes-compatible runtime* is any framework integration that wires
policies to triggers and actions per §5–§7.

## 3. Policy Document Format

A policy is a UTF-8 markdown document. **The entire document is the
criteria** sent to the evaluator. Reflexes is a thin consumer of the
policy: it does not require frontmatter, a fixed section layout, or any
Reflexes-specific markup. The policy's **name**, along with the trigger
and action, is supplied by the binding (§6), never by the policy document
itself.

This keeps a policy portable — the same document is a valid CoPE criteria
document and a valid Reflexes policy.

### 3.1 Criteria

The criteria is the whole document, verbatim, with one exception: a
leading YAML frontmatter block (a `---`-delimited block at the very start
of the file) is OPTIONAL metadata and MUST be stripped before the
document is sent to the evaluator. Reflexes does not require or interpret
frontmatter; policies SHOULD omit it.

### 3.2 Name

The policy name is a property of the **binding**, not the policy
document: it is carried by the binding's `name` field (§6), a
human-readable identifier used in feedback, logs, and observability. The
same document may be bound under different names. For a local `file`
reference, a runtime MAY default the name to the source file's basename
without the `.md` extension (e.g. `Unsourced-Claims.md` →
`Unsourced-Claims`, with no slugification or case-folding) when the
binding omits it; a hosted reference (which has no filename) MUST carry an
explicit name.

### 3.3 Recommended structure (CoPE)

Reflexes does not mandate a structure, but the criteria SHOULD be written
so a classifier can apply it reliably. The CoPE convention — used by the
starter policies — is RECOMMENDED:

```markdown
# Criteria

## Overview
<what this detects; what the agent should do when it fires>

## Definition of Terms
- **Term**: <precise definition, referenced below>

## Interpretation of Language
- <how to scope and interpret the check>

## Definition of Labels

### (XX): <Label Name>

#### Includes
- **Category**: <what matches, with a concrete example>

#### Excludes
- **Category**: <a near-miss that must NOT match, with an example>
```

Contrastive `Includes` / `Excludes` and a precise `Definition of Terms`
are what make classification reliable; vague criteria cause false
positives.

## 4. Evaluator Interface

An evaluator is any callable that accepts a policy criteria string and
a content string and returns a verdict.

### 4.1 Input

An evaluator MUST accept at minimum:

| Field      | Type   | Notes |
|------------|--------|-------|
| `policy`   | string | The criteria text from §3.3. |
| `content`  | string | The content to classify. |

It MAY accept additional inputs (e.g. a `reason` flag requesting an
explanation, a model identifier, a timeout).

### 4.2 Output (Verdict)

An evaluator MUST return a verdict with at minimum:

| Field         | Type   | Notes |
|---------------|--------|-------|
| `label`       | int    | 0 (no match) or 1 (match). |

The `label` is the only required field: the action taxonomy (§7) keys
solely off `label`. An evaluator MAY return additional fields:

| Field         | Type           | Notes |
|---------------|----------------|-------|
| `confidence`  | float \| null  | In `[0.0, 1.0]` when provided. Absent or null for evaluators that do not expose a confidence. |
| `reason`      | string \| null | Free-text rationale, when requested. |
| `latency_ms`  | float          | End-to-end evaluation latency. |

### 4.3 Failure modes

When an evaluator cannot produce a verdict (network error, timeout,
malformed response), the runtime MUST treat the result as an
**evaluator error**, distinct from `label=0`. Evaluator errors MUST NOT
trigger any action (§7); the runtime MUST log the error and allow the
agent to continue (**fail-open**).

This is a deliberate choice: a governance tool that blocks production
on evaluator outage will not be adopted. Failing open at the evaluator
boundary preserves the agent's availability while making the failure
visible in logs.

## 5. Trigger Taxonomy

A Reflexes-compatible runtime MUST support these four triggers:

| Trigger             | Fires when … | Content sent to evaluator |
|---------------------|--------------|---------------------------|
| `on_input`          | The agent receives a user input. | The user input (passthrough). |
| `before_tool_call`  | The agent is about to invoke a tool. | JSON object `{"tool_name": <name>, "args": <input>}` serialized. |
| `after_tool_call`   | A tool returned a result. | `tool_name returned: <result>`. |
| `before_response`   | The agent is about to emit a final response. | The response text (passthrough). |

A runtime MAY support additional triggers (e.g. `on_plan_step`,
`on_memory_write`); such extensions are non-normative for v0.1.

### 5.1 Optional context enrichment

Implementations MAY surface additional context to the evaluator alongside
the `content` field (e.g., the user's most-recent request, the multi-step
task **trajectory**, the agent's **standing instructions** — CLAUDE.md
for Claude Code, `Agent(instructions=...)` for OpenAI Agents SDK,
`agent.instruction` for Google ADK, etc.). Such context is reference-only:
the evaluator MUST classify the `content`, not the context.

When enrichment is provided, implementations SHOULD render the current
request first and give each section a self-describing label, so the
evaluator can distinguish the load-bearing signal from supporting
context. The reference Python implementation uses these labels:

- `Current request:` — the user's most-recent request, rendered first
  and capped most generously so it is never starved.
- `Original task:` — the first user message of the session, suppressed
  when identical to the current request.
- `Standing instructions:` — host-native standing instructions, capped
  most aggressively so a large CLAUDE.md or system prompt cannot crowd
  out the current request.
- `Actions this turn:` — for `before_response`, a step list of the tool
  calls the agent made this turn (its trajectory), so completion claims
  can be checked against what actually happened.

Per-section size budgets — not just a total cap — are RECOMMENDED so the
current-request slot is never starved by large background sections.

Context enrichment is non-normative. Policies that don't need it work
against the minimal `(policy, content)` contract from §4.1. Policies that
do need it (e.g., laziness-detection / early-stopping — "did the response
claim completion when the actions this turn show skipped steps?";
constraint-violation — "would this tool call do something the user
explicitly said NOT to do?") are more reliable when the runtime provides
task-state context with the current request rendered first. The reference
implementation does this for the Claude Code integration (disk-based
resolution of CLAUDE.md + transcript-based initial-prompt extraction)
and for the OpenAI Agents SDK and Google ADK integrations (in-process
attribute access for standing instructions).

## 6. Configuration

A Reflexes runtime SHOULD load configuration from a file named
`reflexes.yaml` (or one explicitly passed by the user). The file
declares **reflex bindings** — `(policy, name, trigger, action)` tuples —
grouped under named sets:

```yaml
enabled: true   # optional global switch (default true); false disables all enforcement
reflexes:
  default:
    - policy: ./policies/prompt-injection.md
      name: prompt-injection
      trigger: on_input
      action: deny
    - policy: ./policies/safe-delete.md
      name: safe-delete
      trigger: before_tool_call
      action: deny

  customer-comms:
    - policy: ./policies/no-timeline-commitments.md
      name: no-timeline-commitments
      trigger: before_response
      action: deny

evaluators:
  default:
    type: zentropi
    model: cope-latest
    api_key_env: ZENTROPI_API_KEY
```

An optional top-level `enabled` boolean (default `true`) is a global kill
switch: when `false`, a runtime MUST load no bindings and every check MUST allow
(as if no policies were bound). Because a disabled safety layer is
indistinguishable from a healthy one at the enforcement path, a runtime SHOULD
surface the disabled state prominently in any status/health output — never as a
healthy or enforcing state.

The `default` group, if present, MUST always apply. Named groups are
opt-in by the runtime user. A runtime MUST resolve the same
`(policy_ref, trigger)` pair across multiple groups using
**last-writer-wins** semantics on the merged binding list.

Each binding declares a `name` — a required human-readable identifier used in
feedback, logs, and observability (though a runtime MAY default it to the
basename of a local `file` policy when omitted; §3.2) — and a `policy`
**reference** of the form
`<scheme>:<locator>`. The `file` scheme — the default when no registered scheme
prefix is present — resolves to a local policy document; other schemes (e.g.
`zentropi:`, `github:`) resolve to a hosted policy by implementation-defined
means. However a reference is resolved, it MUST yield a §3 policy document
(criteria text); the trigger and action semantics (§5–§7) are unchanged. An
optional trailing `@<version>` pins a hosted reference to an immutable version.
(Registry / distribution mechanics remain out of scope — see §9.)

## 7. Action Taxonomy

A Reflexes-compatible runtime MUST support these three actions:

| Action  | Behavior on `label=1` |
|---------|------------------------|
| `deny`  | Block the operation. Construct a feedback message containing the criteria text from §3.3 and surface it to the agent via the framework's native rejection mechanism. |
| `flag`  | Allow the operation. Make the verdict observable (logs, structured metadata, framework-native state) without interrupting the agent. |
| `log`   | Allow the operation. Log the verdict at INFO. |

A runtime MAY define additional actions (e.g. `escalate`, `modify`,
`route`); such extensions are non-normative for v0.1.

### 7.1 Self-correction on deny

When `deny` fires, the runtime MUST surface the policy criteria text
to the agent in a form the agent can read on its next turn. The
canonical feedback shape is:

```
<trigger-specific intro>

Policy:
---
<criteria text from §3.3>
---
```

The trigger-specific intros SHOULD be concrete enough that the agent
can decide what to do next:

- `on_input`: "The user's input did not comply with a policy. Inform the user and ask them to rephrase."
- `before_tool_call`: "Your tool call did not comply with a policy. Resubmit your tool call using an alternative approach that complies with the policy."
- `after_tool_call`: "The tool result did not comply with a policy. Do not use this result directly."
- `before_response`: "Your response did not comply with a policy. Please revise to be consistent with the following rules."

Self-correction is the load-bearing user-facing property of the
standard: an agent that is told *which* policy it failed and *why*
will revise on its next turn. An agent that is merely blocked will
loop. Conformant runtimes MUST deliver the criteria text, not just
the policy name.

## 8. Versioning

This specification follows semantic versioning. v0.1 is the launch
release. Breaking changes (renamed fields in §3.1, new required fields
in §4.2, removed triggers in §5, removed actions in §7) require a
major version bump.

Implementations SHOULD declare the spec version they target in their
package metadata.

## 9. What This Spec Does NOT Define

The following are **deliberately non-normative**:

- **Evaluator implementation.** Any classifier that conforms to §4 is
  acceptable. The default evaluator in the reference Python
  implementation is Zentropi CoPE; OpenAI-compatible, Anthropic, and
  Gemini evaluators are also supported. Self-hosted models are an
  implementation choice.
- **Framework integrations.** How a runtime wires triggers to a
  specific agent framework is implementation-defined. Reference
  integrations exist for OpenAI Agents SDK, Claude Agent SDK, Google
  ADK, LangGraph, OpenAI Codex, Claude Code, and others.
- **Policy registry / distribution.** §6 references policies by path. A
  shared registry that resolves short names to policy documents (so a
  binding could reference a published policy instead of a local file) is a
  possible future extension; its design is non-normative and out of scope
  for v0.1.
- **Caching.** Whether and how a runtime caches verdicts is an
  implementation choice. v0.1 reference behavior: no caching.
- **Threshold tuning.** Mapping `confidence` to action (e.g. only deny
  above 0.8) is an implementation choice, and is only available with
  evaluators that report a `confidence` (it is optional — see §4.2).

---

*Reference implementation: https://reflexes.sh*
