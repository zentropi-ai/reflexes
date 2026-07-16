/**
 * Tests for hosted policy references in the OpenClaw plugin (src/policy.ts):
 * ref parsing, sources (mocked fetch), retries, and the on-disk cache
 * (pinned-permanent / unpinned-TTL / write-through / fail-safe-to-stale).
 *
 * A live layer (opt-in) exercises a real Zentropi labeler and a public GitHub
 * policy — skipped unless the env vars are set.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  parseRef,
  resolveCriteria,
  PolicyNotFound,
} from "../dist/index.js";

// Isolate the cache under a temp HOME (cacheDir() reads $HOME lazily). Set once;
// tests use distinct refs so cache files never collide.
process.env.HOME = mkdtempSync(join(tmpdir(), "rx-openclaw-home-"));
delete process.env.REFLEXES_POLICY_CACHE_TTL;

// --- fake fetch helpers ---------------------------------------------------
function res(status, { json, text } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => json,
    text: async () => text ?? (json != null ? JSON.stringify(json) : ""),
  };
}

/** Build a fetch impl from a queue of responses (or thrown errors). */
function queueFetch(items) {
  let i = 0;
  const calls = [];
  const impl = async (url) => {
    calls.push(url);
    const item = items[Math.min(i, items.length - 1)];
    i++;
    if (item instanceof Error) throw item;
    return item;
  };
  impl.calls = calls;
  return impl;
}

// --- parseRef -------------------------------------------------------------
test("parseRef: bare and file paths are the file scheme", () => {
  for (const p of ["./policies/x.md", "/abs/y.md", "file:./z.md", "C:\\win\\p.md"]) {
    assert.equal(parseRef(p).scheme, "file");
    assert.equal(parseRef(p).version, null);
  }
});

test("parseRef: zentropi pinned + unpinned", () => {
  assert.deepEqual(
    { ...parseRef("zentropi:lab123") },
    { scheme: "zentropi", locator: "lab123", version: null, raw: "zentropi:lab123", isPinned: false },
  );
  const p = parseRef("zentropi:lab123@v5");
  assert.equal(p.version, "v5");
  assert.equal(p.isPinned, true);
});

test("parseRef: github with path + git ref", () => {
  const p = parseRef("github:acme/repo/pols/p.md@v1.2.0");
  assert.equal(p.scheme, "github");
  assert.equal(p.locator, "acme/repo/pols/p.md");
  assert.equal(p.version, "v1.2.0");
});

test("parseRef: unregistered prefix (no http scheme) is a file path", () => {
  assert.equal(parseRef("http://example.com/p.md").scheme, "file");
});

// --- file source ----------------------------------------------------------
test("resolveCriteria: file reads and strips frontmatter", async () => {
  const dir = mkdtempSync(join(tmpdir(), "rx-file-"));
  writeFileSync(join(dir, "p.md"), "---\nname: x\n---\n# Criteria\n\nbody");
  const out = await resolveCriteria(parseRef("./p.md"), { baseDir: dir });
  assert.equal(out, "# Criteria\n\nbody");
});

// --- zentropi source (mocked) --------------------------------------------
test("zentropi pinned fetches that version", async () => {
  const f = queueFetch([res(200, { json: { criteria_text: "PINNED" } })]);
  const out = await resolveCriteria(parseRef("zentropi:labA@v1"), { fetchImpl: f });
  assert.equal(out, "PINNED");
  assert.match(f.calls[0], /\/v1\/labelers\/labA\/versions\/v1$/);
});

test("zentropi unpinned picks latest deployed", async () => {
  const f = queueFetch([res(200, { json: [
    { id: "a", criteria_text: "OLD", is_deployed: true, created_at: "2026-01-01" },
    { id: "b", criteria_text: "NEW", is_deployed: true, created_at: "2026-02-01" },
    { id: "c", criteria_text: "DRAFT", is_deployed: false, created_at: "2026-03-01" },
  ] })]);
  const out = await resolveCriteria(parseRef("zentropi:labB"), { fetchImpl: f });
  assert.equal(out, "NEW");
});

test("zentropi 404 -> PolicyNotFound", async () => {
  const f = queueFetch([res(404, { text: "nope" })]);
  await assert.rejects(
    () => resolveCriteria(parseRef("zentropi:labC@v9"), { fetchImpl: f }),
    PolicyNotFound,
  );
});

// --- github source (mocked) ----------------------------------------------
test("github builds raw url + returns text", async () => {
  const f = queueFetch([res(200, { text: "# Criteria\n\ngh" })]);
  const out = await resolveCriteria(parseRef("github:acme/repo/p.md@sha1"), { fetchImpl: f });
  assert.equal(out, "# Criteria\n\ngh");
  assert.equal(f.calls[0], "https://raw.githubusercontent.com/acme/repo/sha1/p.md");
});

// --- retries --------------------------------------------------------------
test("retries transient 503 then succeeds", async () => {
  const f = queueFetch([res(503), res(503), res(200, { json: { criteria_text: "RECOVERED" } })]);
  const out = await resolveCriteria(parseRef("zentropi:labR@v1"), { fetchImpl: f });
  assert.equal(out, "RECOVERED");
  assert.equal(f.calls.length, 3);
});

test("does not retry terminal 404", async () => {
  const f = queueFetch([res(404), res(200, { json: { criteria_text: "x" } })]);
  await assert.rejects(() => resolveCriteria(parseRef("zentropi:labT@v9"), { fetchImpl: f }), PolicyNotFound);
  assert.equal(f.calls.length, 1);
});

// --- cache ----------------------------------------------------------------
// Pinned refs GET a single version object; unpinned refs GET a versions array.
function countingFetch(value) {
  const state = { n: 0, value };
  const impl = async () => {
    state.n++;
    return res(200, { json: { criteria_text: state.value } });
  };
  impl.state = state;
  return impl;
}

function countingVersions(value) {
  const state = { n: 0, value };
  const impl = async () => {
    state.n++;
    return res(200, { json: [{ id: "v", criteria_text: state.value, is_deployed: true, created_at: "2026-01-01" }] });
  };
  impl.state = state;
  return impl;
}

test("pinned is cached permanently (ignores TTL)", async () => {
  const f = countingFetch("v5text");
  const ref = parseRef("zentropi:cachePin@v5");
  assert.equal(await resolveCriteria(ref, { fetchImpl: f }), "v5text");
  process.env.REFLEXES_POLICY_CACHE_TTL = "0"; // even with TTL 0, pinned won't refetch
  f.state.value = "CHANGED";
  assert.equal(await resolveCriteria(ref, { fetchImpl: f }), "v5text");
  assert.equal(f.state.n, 1);
  delete process.env.REFLEXES_POLICY_CACHE_TTL;
});

test("unpinned serves fresh cache within TTL", async () => {
  const f = countingVersions("latest");
  const ref = parseRef("zentropi:cacheFresh");
  process.env.REFLEXES_POLICY_CACHE_TTL = "3600";
  assert.equal(await resolveCriteria(ref, { fetchImpl: f }), "latest");
  assert.equal(await resolveCriteria(ref, { fetchImpl: f }), "latest");
  assert.equal(f.state.n, 1);
  delete process.env.REFLEXES_POLICY_CACHE_TTL;
});

test("unpinned refetches after TTL with write-through", async () => {
  const f = countingVersions("old");
  const ref = parseRef("zentropi:cacheWrite");
  process.env.REFLEXES_POLICY_CACHE_TTL = "0"; // always stale
  assert.equal(await resolveCriteria(ref, { fetchImpl: f }), "old");
  f.state.value = "new";
  assert.equal(await resolveCriteria(ref, { fetchImpl: f }), "new");
  assert.equal(f.state.n, 2);
  delete process.env.REFLEXES_POLICY_CACHE_TTL;
});

test("unpinned fail-safe serves stale on fetch error", async () => {
  const ref = parseRef("zentropi:cacheFailsafe");
  // Warm the cache.
  assert.equal(await resolveCriteria(ref, { fetchImpl: countingVersions("warm") }), "warm");
  // Expire + break the fetch → serve stale.
  process.env.REFLEXES_POLICY_CACHE_TTL = "0";
  const boom = async () => { throw new Error("network down"); };
  assert.equal(await resolveCriteria(ref, { fetchImpl: boom }), "warm");
  delete process.env.REFLEXES_POLICY_CACHE_TTL;
});

test("cold cache + fetch error rejects", async () => {
  const boom = async () => { throw new Error("network down"); };
  await assert.rejects(() => resolveCriteria(parseRef("zentropi:neverCached"), { fetchImpl: boom }));
});

// --- opt-in live ----------------------------------------------------------
const LABELER = process.env.REFLEXES_TEST_LABELER_ID;
const PINNED = process.env.REFLEXES_TEST_LABELER_PINNED_VERSION;
const GITHUB_REF = process.env.REFLEXES_TEST_GITHUB_REF;

test("live: zentropi hosted fetch", { skip: !(process.env.ZENTROPI_API_KEY && LABELER && PINNED) }, async () => {
  const pinned = await resolveCriteria(parseRef(`zentropi:${LABELER}@${PINNED}`));
  assert.ok(pinned.trim().length > 0);
  const latest = await resolveCriteria(parseRef(`zentropi:${LABELER}`));
  assert.ok(latest.trim().length > 0);
});

test("live: github hosted fetch", { skip: !GITHUB_REF }, async () => {
  const out = await resolveCriteria(parseRef(GITHUB_REF));
  assert.ok(out.trim().length > 0);
});
