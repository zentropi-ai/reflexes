## What does this PR do

<!-- A short description of the change and why it's needed. Link any
related issue (e.g. "Fixes #123"). -->

## Checklist

- [ ] Tests pass — `pytest tests/` is green.
- [ ] Docs updated where behavior changed (`README.md`, `FRAMEWORKS.md`,
      `SPEC.md`, or the `skill/references/` guides).
- [ ] No functional change to unrelated areas — the diff is focused.

## For a new framework integration

<!-- Delete this section if it doesn't apply. -->

- **What framework? Version range supported?**
- **Which Reflexes triggers does it bind?**
- **Are all four full-deny, or are there asymmetries?** (If asymmetric,
  why — does the framework's hook protocol just not expose a
  pre-tool-call event, etc.?)
- **Live runtime tested or unit-test-only?**
