# Troubleshooting Reflexes

This is the full diagnostic walkthrough referenced from SKILL.md. The
most common symptom is a policy that should fire but doesn't — the cause
is usually one of four places: the hook isn't installed, the evaluator
can't be reached, the policy file isn't loaded, or the binding isn't
wired. Walk these in order; the later sections cover debug logging and
filing an issue.

---

## 1. Confirm the hook is installed

Run Step 7 from SKILL.md → Install — the deterministic sanity check.
It asserts every required hook event is registered and every
`policy:` ref in `reflexes.yaml` resolves to a file on disk.

If `OK` doesn't print:

- **Missing event in `settings.json` / `hooks.json`:** Step 6b didn't
  complete. Re-run Step 6.
- **Unresolved `policy:` refs:** the yaml references files that
  aren't in `~/.reflexes/policies/`. Check the policy filename
  exactly matches.
- **Yaml file itself missing:** Step 5 was skipped or the file was
  deleted. Re-run Step 5.

If `policies MISSING`:

- `ls ~/.reflexes/policies/` returned fewer than 6 files. Re-run
  Step 4 to bundle the six starter policies (the `integrity` and
  `autonomy` bundles).

---

## 2. Confirm the host CLI sees the hook

For Claude Code:

```bash
# Look for the hook entry in settings.json.
python3 -c "
import json, os
d = json.load(open(os.path.expanduser('~/.claude/settings.json')))
for ev, entries in d.get('hooks', {}).items():
    for e in entries:
        for h in e.get('hooks', []):
            if 'reflexes' in h.get('command', ''):
                print(f'{ev}: {h[\"command\"]}')"
```

Must print four lines (UserPromptSubmit, PreToolUse, PostToolUse,
Stop) each with a command containing `reflexes.integrations.claude_code`.

For Codex:

```bash
# Same check against ~/.codex/hooks.json.
python3 -c "
import json, os
d = json.load(open(os.path.expanduser('~/.codex/hooks.json')))
for ev, entries in d.get('hooks', {}).items():
    for e in entries:
        for h in e.get('hooks', []):
            if 'reflexes' in h.get('command', ''):
                print(f'{ev}: {h[\"command\"]}')"

# Plus, for Codex, two host-side gates (the doctor checks/flags both):
uv run <SKILL_DIR>/scripts/reflexes-doctor --config ~/.reflexes/reflexes.yaml
```

For Codex specifically, `hooks.json` is **inert** unless BOTH are true:

1. **Enabled:** `hooks = true` is set under the `[features]` table in
   `~/.codex/config.toml`. A bare top-level key is ignored. The doctor flags this.
2. **Trusted:** the user has reviewed and trusted the exact hook definitions —
   run `/hooks` in Codex, trust the Reflexes definitions, restart. Codex
   silently skips **untrusted** hooks, which looks identical to an unreachable
   evaluator: a silent fail-open. Editing a hook's command changes its hash and
   requires trusting it again. The doctor can't see Codex's trust state, so it
   warns rather than declaring Codex "enforcing" — verify by triggering a known
   policy and confirming it fires.

**Did you restart the host CLI after Step 6b?** Both Claude Code and
Codex load settings/hooks on startup. A running session won't pick
up the new hook entries until it's restarted.

---

## 3. Confirm the evaluator can be reached

Per SPEC.md §4.3, an evaluator failure fails-open — the policy
silently doesn't fire rather than blocking the operation. So a
silent no-fire is consistent with the evaluator being unreachable.

**Rate limits and subscriber-only capabilities.** Two evaluator failures
carry a specific meaning and print a `[Reflexes]` notice to stderr:

- **Rate limit (429).** Sustained throttling that survives retries means the
  policy stopped evaluating for that call (fail-open — the action proceeded
  *unchecked*). Tell the user their policies aren't fully enforcing under this
  load; a Zentropi subscription raises the limit (https://zentropi.ai/subscription,
  or info@zentropi.ai).
- **Multimodal (403).** Evaluating an image/video requires Zentropi's
  subscriber-only multimodal model; on a free key that check is skipped
  (fail-open). Tell the user image/video policies need a subscription.

In both cases the operation was allowed *without* the policy running — say so
plainly. The upgrade URL lives in the message, not hardcoded here, so it stays
current.

Check the configured evaluator from `~/.reflexes/reflexes.yaml`:

```bash
python3 -c "
import yaml, os
with open(os.path.expanduser('~/.reflexes/reflexes.yaml')) as f:
    cfg = yaml.safe_load(f)
print(cfg.get('evaluators', {}).get('default'))"
```

Then test reachability based on `type:`:

### `type: zentropi`

```bash
# API reachability (health endpoint, unauthenticated).
curl -fsS --max-time 5 https://api.zentropi.ai/v1/health \
  && echo "api OK" || echo "api UNREACHABLE"

# Zentropi CoPE requires an API key. If it's unset, that's the problem —
# get a free key at https://zentropi.ai.
test -n "$ZENTROPI_API_KEY" || echo "ZENTROPI_API_KEY not set — get one at https://zentropi.ai"
test -n "$ZENTROPI_API_KEY" && \
  curl -fsS --max-time 5 \
    -H "Authorization: Bearer $ZENTROPI_API_KEY" \
    https://api.zentropi.ai/v1/check \
    -d '{"content_text":"hello","criteria_text":"matches if it says hello","model":"cope-latest"}' \
  | head -3
```

### `type: openai`

```bash
# Probe the configured endpoint with the configured API key.
ENDPOINT=$(python3 -c "
import yaml, os
with open(os.path.expanduser('~/.reflexes/reflexes.yaml')) as f:
    c = yaml.safe_load(f)['evaluators']['default']
print(c.get('endpoint', 'https://api.openai.com/v1/chat/completions'))")
KEY_ENV=$(python3 -c "
import yaml, os
with open(os.path.expanduser('~/.reflexes/reflexes.yaml')) as f:
    c = yaml.safe_load(f)['evaluators']['default']
print(c.get('api_key_env', ''))")
KEY="${!KEY_ENV}"  # indirect env-var lookup
curl -fsS --max-time 5 -H "Authorization: Bearer $KEY" \
  "$ENDPOINT" -d '{"messages":[{"role":"user","content":"ping"}],"model":"gpt-4o-mini","max_tokens":1}' \
  | head -3
```

If the endpoint is unreachable or auth fails: fix the network /
key, or fall back to Branch (iii) of SKILL.md → Verify it Works
(point at a local OpenAI-compatible LLM).

### `type: anthropic` / `type: gemini`

Same shape — the configured `api_key_env` must resolve to a valid
key, and `api.anthropic.com` / `generativelanguage.googleapis.com`
must be reachable.

---

## 4. Inspect the hook subprocess directly

Run the hook subprocess by hand, feeding it a synthetic event, and
watch what it does. This bypasses the host CLI entirely.

For Claude Code:

```bash
echo '{
  "tool_name": "Bash",
  "args": {"command": "rm /home/user/work/important-document.pdf"},
  "hook_event_name": "PreToolUse"
}' | python3 -m reflexes.integrations.claude_code \
  --config $HOME/.reflexes/reflexes.yaml
```

Expected output: a JSON object with `permissionDecisionReason`
containing the UPD policy text. If you get an empty JSON object
(`{}`) or an error, the hook subprocess can't run end-to-end —
check the error message.

Common errors here:

- `ModuleNotFoundError: No module named 'reflexes'` — the
  `python3` binary in PATH isn't the one that has Reflexes
  installed. Re-check Step 6a's pipx-injected-Python path.
- `FileNotFoundError: ~/.reflexes/reflexes.yaml` — the config
  file isn't where Reflexes expects. Verify it's at
  `~/.reflexes/reflexes.yaml` (not `~/.config/reflexes/` or
  similar).
- `yaml.YAMLError` — the yaml has a syntax error. Re-validate
  with `python3 -c "import yaml; yaml.safe_load(open(...))"`.

For Codex:

```bash
# Same shape, against the codex integration.
echo '{...same JSON...}' | python3 -m reflexes.integrations.openai_codex \
  --config $HOME/.reflexes/reflexes.yaml
```

---

## 5. Logging

Logging is controlled entirely by the `logging:` block in `reflexes.yaml`
— there are no environment variables. Both keys are optional with safe
defaults:

```yaml
logging:
  level: warning   # off | error | warning (default) | info | debug
  format: text     # text | json
```

**Levels.** `level` sets how much is captured:

| level | captures |
|---|---|
| `off` | nothing (no log file is created) |
| `error` | evaluator/config failures only |
| `warning` *(default)* | enforcement problems (fail-open, evaluator errors) **and** `log`-action hits — but **no message content** |
| `info` | + per-decision detail (still no content) |
| `debug` | + the **evaluated content** (user requests, tool args, responses) and per-policy verdicts |

Message content is emitted **only at `debug`**, so the `warning` default
keeps potentially sensitive data out of the log. This is the key knob for
diagnosing "why didn't my policy fire?": set `level: debug`, restart the
host, reproduce, inspect the `reflexes.*` records — then **set the level
back**. `reflexes-doctor` warns while `debug` is active, precisely because
content is being written to disk.

**Format.** `format: json` emits one JSON object per line (fields `ts`,
`level`, `logger`, `host`, `message`, plus any structured extras) for
log-aggregation stacks (Loki, Cloudwatch, …); `text` is the default
human-readable format.

**Host tag.** Every record is prefixed with the originating host —
`[claude-code]`, `[codex]`, `[hermes]`, `[openclaw]` — because multiple
hooks on one machine can share a log file (Codex reuses the Claude Code
hook and writes to the same `~/.cache/reflexes/debug.log`, owner-only), so
the tag is what tells their lines apart. In-process SDK hosts log to their
own application output.

**Turning enforcement off** is a separate switch — see *Turning Reflexes
on and off* in `SKILL.md` (the top-level `enabled:` key), not the log
level. `logging: off` silences the log; it does **not** stop enforcement.

For richer per-evaluation telemetry — the trigger that fired, the policy
name, and the verdict (label, confidence, latency) as metrics/spans —
install the `[otel]` extra (`pip install 'reflexes[otel]'`) and point it
at your OpenTelemetry collector; the `reflexes.evaluate` span carries
those attributes (see the README's Observability section).

If the evaluation shows the policy scored but returned `label=0`: the
evaluator scored the content as not matching. Inspect the
`content_text` field — does it look like the behavior you expected?
The `before_response` trigger evaluates the *assistant message
text*, not the user's prompt; the `before_tool_call` trigger
evaluates the *tool name + args*, not the surrounding context.
Mismatched triggers are a common cause of "policy didn't fire."

If the log shows no entries at all after restart + trigger: the
hook subprocess didn't run. Walk steps 1-4 again.

---

## 6. Inspect the binding ↔ trigger match

A policy bound to `before_response` won't catch a destructive
deletion in `Bash` — that needs `before_tool_call`. A policy bound
to `before_tool_call` won't catch laziness in an assistant message
— that needs `before_response`.

Cross-check what you're catching against what the starter policies use:

| Policy | Trigger | Catches |
|---|---|---|
| `Constraint-Violation` | `before_tool_call` | Tool call that oversteps a user instruction or stated constraint |
| `Unconfirmed-Destructive-Action` | `before_tool_call` | Destructive file/data action without explicit user confirmation |
| `Sycophancy` | `before_response` | Assistant over-agreeing or flattering instead of being straight |
| `Early-Stopping` | `before_response` | Assistant stopping work prematurely |
| `Laziness-Detection` | `before_response` | Assistant message hedging out with placeholder or unfinished work |
| `Unsourced-Claims` | `before_response` | Assistant assertions without sources |

If your custom policy is on the wrong trigger, change the
`trigger:` value in `reflexes.yaml` and restart the host CLI.

---

## 7. Last resort: file an issue

If steps 1-6 don't surface the cause, capture the debug log and the
content of `~/.reflexes/reflexes.yaml` (redacting API keys) and
file an issue at https://github.com/zentropi-ai/reflexes/issues.
Include the host CLI version, the policy slug, the
trigger configured, and the exact content you expected to catch.
