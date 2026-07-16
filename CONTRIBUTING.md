# Contributing to Reflexes

Thanks for your interest in Reflexes! Contributions of all sizes are
welcome — bug fixes, docs, new framework integrations, and additional sample policies. This guide gets you set up and points you at the right in-depth doc for
whatever you're working on.

By contributing, you agree that your contributions are licensed under
the project's [Apache-2.0](./LICENSE) license.

Please also read our [Code of Conduct](./CODE_OF_CONDUCT.md). For
security issues, **do not open a public issue** — see
[SECURITY.md](./SECURITY.md).

## Where to start, by contribution type

- **Adding a framework integration** (a new host or SDK binding):
  follow [`skill/references/add-framework-integration.md`](./skill/references/add-framework-integration.md).
  It walks through mapping a framework's hooks onto Reflexes' four
  triggers, the mandatory `assemble_task_context()` wiring, tests, and
  updating `FRAMEWORKS.md`.
- **Writing or improving a policy:** follow
  [`skill/references/writing-policies.md`](./skill/references/writing-policies.md).
  It covers the CoPE criteria format, the runtime vocabulary the
  evaluator sees, and how to write criteria that classify reliably. The
  six starter policies in `skill/reflexes/_starter_policies/` are worked
  examples.
- **Bug fixes and doc fixes:** open an issue first if the change is
  non-trivial or you want to discuss the approach; small, obvious fixes
  can go straight to a PR. If you're diagnosing a policy that should
  fire but doesn't, [`skill/references/troubleshooting.md`](./skill/references/troubleshooting.md)
  is the diagnostic walkthrough.

## Dev setup

Reflexes is a Python package (the reference implementation) plus a
TypeScript OpenClaw plugin. Most contributions touch the Python side.

The package uses the [Hatch](https://hatch.pypa.io/) build backend, with
the importable package living under `skill/reflexes` (see
`pyproject.toml`). An editable install wires it up correctly regardless
of that layout.

Requirements: Python 3.10+.

```bash
# From the repo root, in a fresh virtual environment:
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Editable install with the dev extras (pytest, pytest-asyncio):
pip install -e ".[dev]"

# Run the test suite:
pytest tests/

# Or a single file while iterating:
pytest tests/test_core.py -q
```

Some tests exercise the real Zentropi CoPE endpoint and are **skipped
unless `ZENTROPI_API_KEY` is set** in the environment. You do not need a
key to run the suite — those tests are simply skipped, and the rest run
offline. If you have a key (get a free one at https://zentropi.ai), set
it to run the integration tests too:

```bash
export ZENTROPI_API_KEY=...      # optional; enables the live-endpoint tests
```

Optional extras: `pip install -e ".[otel]"` for the OpenTelemetry
instrumentation, and `pip install -e ".[openai]"` for the OpenAI Agents
SDK integration.

## Opening a pull request

Before you open a PR, please make sure:

- **Tests pass** — `pytest tests/` is green.
- **Docs are updated** — if behavior changed, update the relevant docs
  (`README.md`, `FRAMEWORKS.md`, `SPEC.md`, or the `skill/references/`
  guides). New integrations must add a row to `FRAMEWORKS.md`.
- **No functional change to unrelated areas** — keep the diff focused on
  the change you're making.

If your PR adds a **framework integration**, the description should
answer these four questions (from
[`add-framework-integration.md`](./skill/references/add-framework-integration.md)
§9):

- What framework? Version range supported?
- Which Reflexes triggers does it bind?
- Are all four full-deny, or are there asymmetries? (If asymmetric,
  why — does the framework's hook protocol just not expose a
  pre-tool-call event, etc.?)
- Live runtime tested or unit-test-only?

Maintainers will review the code, the tests, and (for integrations) the
`FRAMEWORKS.md` row. Thanks for contributing!
