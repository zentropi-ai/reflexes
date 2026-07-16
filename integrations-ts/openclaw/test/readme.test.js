// Drift guards: the OpenClaw README's checkable claims must match repo + code
// reality. This is the "docs say X, code proves it" gate for this plugin — it
// caught (and prevents regressing) a legacy policy name in the install example,
// an inaccurate on_input claim, and undocumented hosted-policy support.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, "..", "..", "..");
const README = readFileSync(join(__dirname, "..", "README.md"), "utf8");
const STARTER_DIR = join(REPO_ROOT, "skill", "reflexes", "_starter_policies");

function mdBasenames(dir) {
  const out = new Set();
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    if (statSync(p).isDirectory()) for (const n of mdBasenames(p)) out.add(n);
    else if (entry.endsWith(".md")) out.add(entry.replace(/\.md$/, ""));
  }
  return out;
}

// 1. Every `policy: ./policies/<X>.md` the README shows must be a real starter
//    policy — no legacy/invented names (e.g. the old no-timeline-commitments).
test("README install example references only real starter policies", () => {
  const starters = mdBasenames(STARTER_DIR);
  const referenced = [...README.matchAll(/policy:\s*\.\/policies\/([\w.-]+)\.md/g)].map((m) => m[1]);
  assert.ok(referenced.length > 0, "README shows no example policy binding");
  for (const name of referenced) {
    assert.ok(
      starters.has(name),
      `README references ./policies/${name}.md which is not a starter policy (have: ${[...starters].join(", ")})`,
    );
  }
});

// 2. The evaluator backends the README advertises must match what the code
//    supports (loadConfig's per-type defaults dispatch these four).
test("README lists exactly the evaluator backends the code supports", () => {
  const src = readFileSync(join(__dirname, "..", "src", "index.ts"), "utf8");
  for (const type of ["zentropi", "openai", "anthropic", "gemini"]) {
    assert.match(README, new RegExp(`\\b${type}\\b`), `README omits the '${type}' backend`);
    assert.ok(src.includes(`"${type}"`) || src.includes(`${type}:`), `code missing '${type}' backend`);
  }
});

// 3. Hosted policy schemes are implemented (src/policy.ts), so the README must
//    document them — they were silently supported-but-undocumented before.
test("README documents the hosted policy schemes the code implements", () => {
  const policySrc = readFileSync(join(__dirname, "..", "src", "policy.ts"), "utf8");
  assert.ok(policySrc.includes("zentropiFetch") && policySrc.includes("githubFetch"), "code missing hosted fetchers");
  assert.match(README, /zentropi:/, "README omits the zentropi: policy scheme");
  assert.match(README, /github:/, "README omits the github: policy scheme");
});

// 4. on_input / after_tool_call are observe-only on OpenClaw (framework limit).
//    Guard the wording from regressing to "supported"/"not yet".
test("README states on_input/after_tool_call are observe-only, not 'not yet'", () => {
  assert.match(README, /observe-only/, "README should state the observe-only framework limitation");
  assert.match(README, /on_input/, "README should name on_input in the limitations");
  assert.doesNotMatch(README, /not yet wired/i, "stale 'not yet wired' wording — it's a framework limit, not a TODO");
});
