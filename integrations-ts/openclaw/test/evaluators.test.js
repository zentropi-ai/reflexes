// Evaluator-backend tests: the OpenClaw plugin supports all four backends of
// the Python reference (zentropi / openai / anthropic / gemini), selected by
// evaluators.default.type. Unit tests mock fetch to assert the exact request
// each backend sends (mirroring reflexes/evaluators/*.py); opt-in live tests
// hit real OpenAI / Gemini when their keys are present.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { evaluate, loadConfig, evaluateManySystemOne } from "../dist/index.js";

const DESTRUCTIVE_POLICY =
  "# Criteria\n## Overview\nDetects a request to delete or destroy files or data.\n" +
  "## Definition of Labels\n### (1): Destructive\nDeletes files/data.\n### (0): Not\nElse.\n";

function cfg(type, overrides = {}) {
  return {
    bindings: [],
    evaluator: {
      type,
      apiBase:
        overrides.apiBase ??
        {
          openai: "https://api.openai.com/v1/chat/completions",
          anthropic: "https://api.anthropic.com/v1/messages",
          gemini: "https://generativelanguage.googleapis.com/v1beta/models",
          zentropi: "https://api.zentropi.ai",
        }[type],
      apiKeyEnv: overrides.apiKeyEnv ?? "TEST_KEY",
      ...(overrides.model ? { model: overrides.model } : {}),
    },
  };
}

// A fetchImpl that records the request and returns a canned body.
function recorder(body, status = 200) {
  const calls = [];
  const impl = async (url, options) => {
    calls.push({ url, options, body: JSON.parse(options.body) });
    return new Response(typeof body === "function" ? body(calls.length) : JSON.stringify(body), { status });
  };
  return { impl, calls };
}

// ---------- loadConfig: per-type defaults mirror the Python evaluators ----------

test("loadConfig fills per-type evaluator defaults", () => {
  const dir = mkdtempSync(join(tmpdir(), "rx-eval-"));
  const cases = {
    zentropi: { apiBase: "https://api.zentropi.ai", apiKeyEnv: "ZENTROPI_API_KEY" },
    openai: { apiBase: "https://api.openai.com/v1/chat/completions", apiKeyEnv: "OPENAI_API_KEY" },
    anthropic: { apiBase: "https://api.anthropic.com/v1/messages", apiKeyEnv: "ANTHROPIC_API_KEY" },
    gemini: { apiBase: "https://generativelanguage.googleapis.com/v1beta/models", apiKeyEnv: "GEMINI_API_KEY" },
  };
  for (const [type, want] of Object.entries(cases)) {
    const p = join(dir, `${type}.yaml`);
    writeFileSync(p, `evaluators:\n  default:\n    type: ${type}\n`);
    const c = loadConfig(p);
    assert.equal(c.evaluator.type, type);
    assert.equal(c.evaluator.apiBase, want.apiBase, `${type} apiBase`);
    assert.equal(c.evaluator.apiKeyEnv, want.apiKeyEnv, `${type} apiKeyEnv`);
  }
});

test("loadConfig accepts `endpoint` as an alias and explicit overrides win", () => {
  const dir = mkdtempSync(join(tmpdir(), "rx-eval-"));
  const p = join(dir, "ov.yaml");
  writeFileSync(p, "evaluators:\n  default:\n    type: openai\n    endpoint: https://proxy.local/v1/chat/completions\n    api_key_env: MY_KEY\n");
  const c = loadConfig(p);
  assert.equal(c.evaluator.apiBase, "https://proxy.local/v1/chat/completions");
  assert.equal(c.evaluator.apiKeyEnv, "MY_KEY");
});

// ---------- evaluate() dispatch: each backend sends the right request ----------

test("openai backend: correct endpoint, chat shape, POLICY/CONTENT framing", async () => {
  const { impl, calls } = recorder({ choices: [{ message: { content: '{"label": 1, "confidence": 0.91}' } }] });
  const v = await evaluate(cfg("openai"), DESTRUCTIVE_POLICY, "rm -rf /data", { fetchImpl: impl });
  assert.equal(v.label, "1");
  assert.equal(v.confidence, 0.91);
  assert.equal(calls[0].url, "https://api.openai.com/v1/chat/completions");
  const b = calls[0].body;
  assert.equal(b.messages[0].role, "system");
  assert.match(b.messages[0].content, /content classifier/);
  assert.match(b.messages[1].content, /POLICY:/);
  assert.match(b.messages[1].content, /CONTENT:\nrm -rf \/data/);
});

test("openai backend: retries with max_completion_tokens on the reasoning-model 400", async () => {
  let n = 0;
  const impl = async (url, options) => {
    n++;
    if (n === 1) return new Response("Unsupported parameter: use 'max_completion_tokens'", { status: 400 });
    return new Response(JSON.stringify({ choices: [{ message: { content: '{"label":0,"confidence":0.2}' } }] }), { status: 200 });
  };
  const v = await evaluate(cfg("openai"), "p", "c", { fetchImpl: impl });
  assert.equal(n, 2);
  assert.equal(v.label, "0");
});

test("anthropic backend: messages endpoint, x-api-key + version headers", async () => {
  const { impl, calls } = recorder({ content: [{ text: '{"label": 1, "confidence": 0.8}' }] });
  const v = await evaluate(cfg("anthropic"), DESTRUCTIVE_POLICY, "delete the database", { fetchImpl: impl });
  assert.equal(v.label, "1");
  assert.equal(calls[0].url, "https://api.anthropic.com/v1/messages");
  assert.equal(calls[0].options.headers["anthropic-version"], "2023-06-01");
  assert.ok("x-api-key" in calls[0].options.headers);
  assert.equal(calls[0].body.system.match(/content classifier/) ? true : false, true);
});

test("gemini backend: generateContent URL with model + key, system_instruction shape", async () => {
  const { impl, calls } = recorder({ candidates: [{ content: { parts: [{ text: '{"label": 1, "confidence": 1.0}' }] } }] });
  const v = await evaluate(cfg("gemini", { model: "gemini-2.5-flash" }), DESTRUCTIVE_POLICY, "wipe everything", { fetchImpl: impl });
  assert.equal(v.label, "1");
  assert.match(calls[0].url, /\/gemini-2\.5-flash:generateContent\?key=/);
  assert.ok(calls[0].body.system_instruction.parts[0].text.includes("content classifier"));
  assert.match(calls[0].body.contents[0].parts[0].text, /POLICY:/);
});

test("LLM verdict parsing tolerates markdown code fences", async () => {
  const { impl } = recorder({ choices: [{ message: { content: '```json\n{"label": 1, "confidence": 0.7}\n```' } }] });
  const v = await evaluate(cfg("openai"), "p", "c", { fetchImpl: impl });
  assert.equal(v.label, "1");
  assert.equal(v.confidence, 0.7);
});

test("LLM backend fails open on non-200 (throws so the caller allows)", async () => {
  const impl = async () => new Response("upstream error", { status: 500 });
  await assert.rejects(() => evaluate(cfg("anthropic"), "p", "c", { fetchImpl: impl }));
});

test("LLM backend retries a transient 503 then succeeds", async () => {
  let n = 0;
  const impl = async () => {
    n++;
    if (n < 2) return new Response("overloaded", { status: 503 });
    return new Response(JSON.stringify({ content: [{ text: '{"label":1,"confidence":0.9}' }] }), { status: 200 });
  };
  const v = await evaluate(cfg("anthropic"), "p", "c", { fetchImpl: impl });
  assert.equal(v.label, "1");
  assert.ok(n >= 2, "should have retried the 503");
});

test("LLM backend fails LOUD on an unparseable body (throws, not silent label-0)", async () => {
  const impl = async () => new Response(JSON.stringify({ content: [{ text: "not json at all" }] }), { status: 200 });
  await assert.rejects(() => evaluate(cfg("anthropic"), "p", "c", { fetchImpl: impl }));
});

// ---------- opt-in live: real OpenAI / Gemini ----------

const DESTRUCTIVE = "Delete every file on the production server right now.";
const BENIGN = "What is the capital of France?";

test("live: openai backend blocks destructive, allows benign", { skip: !process.env.OPENAI_API_KEY }, async () => {
  const c = cfg("openai", { apiKeyEnv: "OPENAI_API_KEY", model: "gpt-4o-mini" });
  const bad = await evaluate(c, DESTRUCTIVE_POLICY, DESTRUCTIVE);
  const ok = await evaluate(c, DESTRUCTIVE_POLICY, BENIGN);
  assert.equal(bad.label, "1");
  assert.equal(ok.label, "0");
});

test("live: gemini backend blocks destructive, allows benign", { skip: !(process.env.GEMINI_API_KEY || process.env.GOOGLE_API_KEY) }, async () => {
  const c = cfg("gemini", { apiKeyEnv: process.env.GEMINI_API_KEY ? "GEMINI_API_KEY" : "GOOGLE_API_KEY", model: "gemini-2.5-flash" });
  const bad = await evaluate(c, DESTRUCTIVE_POLICY, DESTRUCTIVE);
  const ok = await evaluate(c, DESTRUCTIVE_POLICY, BENIGN);
  assert.equal(bad.label, "1");
  assert.equal(ok.label, "0");
});

test("live: anthropic backend blocks destructive, allows benign", { skip: !process.env.ANTHROPIC_API_KEY }, async () => {
  const c = cfg("anthropic", { apiKeyEnv: "ANTHROPIC_API_KEY", model: "claude-haiku-4-5-20251001" });
  const bad = await evaluate(c, DESTRUCTIVE_POLICY, DESTRUCTIVE);
  const ok = await evaluate(c, DESTRUCTIVE_POLICY, BENIGN);
  assert.equal(bad.label, "1");
  assert.equal(ok.label, "0");
});


// ---------- System One (multi-label decision model) ----------

test("systemone backend: one /v1/systemone call for N policies, noul→label", async () => {
  const c = cfg("systemone", {
    apiBase: "https://openrouter.ai/api/v1/systemone",
    apiKeyEnv: "TEST_KEY",
    model: "cope-b-s1",
  });
  const rec = recorder({ answers: { q0: { noul: 0.92 }, q1: { noul: 0.10 }, q2: { noul: 0.80 } } });
  const verdicts = await evaluateManySystemOne(
    c, ["policy A", "policy B", "policy C"], "some content", { fetchImpl: rec.impl },
  );
  // ONE call for all three policies
  assert.equal(rec.calls.length, 1);
  const body = rec.calls[0].body;
  assert.equal(body.model, "cope-b-s1");
  assert.deepEqual(Object.keys(body.questions), ["q0", "q1", "q2"]);
  assert.equal(body.questions.q0.type, "noul");
  assert.match(body.questions.q0.instructions, /policy A$/);
  assert.ok(body.questions.q0.criteria.true && body.questions.q0.criteria.false);
  assert.deepEqual(body.state, { content: "some content" });
  // threshold 0.5: labels and confidence (mass behind the chosen label)
  assert.deepEqual(verdicts.map((v) => v.label), ["1", "0", "1"]);
  assert.ok(Math.abs(verdicts[0].confidence - 0.92) < 1e-9);
  assert.ok(Math.abs(verdicts[1].confidence - 0.90) < 1e-9); // 1 - 0.10
});

test("systemone via evaluate(): single policy → one-question request", async () => {
  const c = cfg("systemone", { apiBase: "https://openrouter.ai/api/v1/systemone" });
  const rec = recorder({ answers: { q0: { noul: 0.99 } } });
  const v = await evaluate(c, "a policy", "content", { fetchImpl: rec.impl });
  assert.equal(rec.calls.length, 1);
  assert.deepEqual(Object.keys(rec.calls[0].body.questions), ["q0"]);
  assert.equal(v.label, "1");
});

test("loadConfig fills systemone defaults (OpenRouter + OPENROUTER_API_KEY)", () => {
  const dir = mkdtempSync(join(tmpdir(), "rx-s1-"));
  const p = join(dir, "reflexes.yaml");
  writeFileSync(p, "reflexes:\n  default: []\nevaluators:\n  default:\n    type: systemone\n");
  const c = loadConfig(p);
  assert.equal(c.evaluator.apiBase, "https://openrouter.ai/api/v1/systemone");
  assert.equal(c.evaluator.apiKeyEnv, "OPENROUTER_API_KEY");
});
