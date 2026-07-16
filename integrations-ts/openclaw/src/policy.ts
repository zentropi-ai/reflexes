/**
 * Hosted policy references for OpenClaw — a TypeScript port of the Python
 * reflexes.policy_ref / policy_sources / policy_cache. A binding's `policy:` can
 * be a local file (default), `zentropi:<labeler_id>[@<version>]`, or
 * `github:<owner>/<repo>/<path>[@<tag-or-sha>]`. All resolve to criteria text,
 * so hosted policies work with any evaluator.
 *
 * The on-disk cache uses the SAME format as the Python implementation
 * (~/.reflexes/.policy-cache/<scheme>-<sha256[:32]>.json), so the two share
 * cache entries. Pinned refs are cached permanently; unpinned use a TTL with
 * write-through + fail-safe-to-stale. There is intentionally no http: scheme.
 */
import { readFileSync, mkdirSync, writeFileSync, renameSync } from "node:fs";
import { join, isAbsolute } from "node:path";
import { homedir } from "node:os";
import { createHash } from "node:crypto";

export type PolicyScheme = "file" | "zentropi" | "github";

const REGISTERED_SCHEMES = new Set<string>(["file", "zentropi", "github"]);
const VERSIONED_SCHEMES = new Set<string>(["zentropi", "github"]);

export interface PolicyRef {
  scheme: PolicyScheme;
  locator: string;
  version: string | null;
  raw: string;
  isPinned: boolean;
}

export function parseRef(ref: string): PolicyRef {
  const raw = ref;
  const text = ref.trim();
  let scheme: PolicyScheme = "file";
  let locator = text;
  const idx = text.indexOf(":");
  if (idx > 0) {
    const head = text.slice(0, idx);
    if (REGISTERED_SCHEMES.has(head)) {
      scheme = head as PolicyScheme;
      locator = text.slice(idx + 1);
    }
  }
  let version: string | null = null;
  if (VERSIONED_SCHEMES.has(scheme)) {
    const at = locator.lastIndexOf("@");
    if (at >= 0) {
      version = locator.slice(at + 1) || null;
      locator = locator.slice(0, at);
    }
  }
  return { scheme, locator, version, raw, isPinned: version !== null };
}

export class PolicyNotFound extends Error {
  ref: PolicyRef;
  constructor(ref: PolicyRef, detail = "") {
    super(`Could not resolve policy: ${ref.raw}${detail ? ` (${detail})` : ""}`);
    this.name = "PolicyNotFound";
    this.ref = ref;
  }
}

type FetchImpl = typeof fetch;

// --- HTTP GET with transient retries (mirrors policy_sources._get) ----------
const RETRYABLE_STATUS = new Set([429, 500, 502, 503, 504]);
const RETRY_DELAYS_MS = [300, 1000]; // before retry 2 and retry 3 (3 attempts)
const FETCH_TIMEOUT_MS = 15_000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function httpGet(
  url: string,
  headers: Record<string, string>,
  fetchImpl: FetchImpl,
): Promise<Response> {
  const attempts = RETRY_DELAYS_MS.length + 1;
  let lastErr: unknown;
  let resp: Response | undefined;
  for (let attempt = 0; attempt < attempts; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    try {
      resp = await fetchImpl(url, { headers, signal: controller.signal });
    } catch (e) {
      lastErr = e;
      resp = undefined;
    } finally {
      clearTimeout(timer);
    }
    if (resp && !RETRYABLE_STATUS.has(resp.status)) return resp; // success or terminal
    if (attempt < attempts - 1) await sleep(RETRY_DELAYS_MS[attempt]);
  }
  if (resp) return resp; // exhausted on a retryable status; caller interprets it
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

// --- zentropi -------------------------------------------------------------
function zentropiBuildBase(): string {
  return (process.env.ZENTROPI_BUILD_API_BASE ?? "https://zentropi.ai").replace(/\/+$/, "");
}

function zentropiHeaders(): Record<string, string> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const key = process.env.ZENTROPI_API_KEY;
  if (key) headers.Authorization = `Bearer ${key}`;
  return headers;
}

function checkZentropiStatus(ref: PolicyRef, resp: Response): void {
  if (resp.status === 404) throw new PolicyNotFound(ref, "labeler/version not found (404)");
  if (resp.status === 401 || resp.status === 403) {
    throw new Error(
      `Zentropi denied access fetching policy ${ref.raw} (HTTP ${resp.status}). ` +
        `Check ZENTROPI_API_KEY and that the labeler is accessible.`,
    );
  }
  if (!resp.ok) throw new Error(`Zentropi fetch failed (HTTP ${resp.status}) for ${ref.raw}`);
}

async function zentropiFetch(ref: PolicyRef, fetchImpl: FetchImpl): Promise<string> {
  const base = zentropiBuildBase();
  const headers = zentropiHeaders();
  let criteria: string | undefined;
  if (ref.version !== null) {
    const resp = await httpGet(`${base}/v1/labelers/${ref.locator}/versions/${ref.version}`, headers, fetchImpl);
    checkZentropiStatus(ref, resp);
    criteria = ((await resp.json()) as { criteria_text?: string } | null)?.criteria_text;
  } else {
    const resp = await httpGet(`${base}/v1/labelers/${ref.locator}/versions`, headers, fetchImpl);
    checkZentropiStatus(ref, resp);
    const versions = ((await resp.json()) as Array<Record<string, unknown>>) ?? [];
    const deployed = versions.filter((v) => v?.is_deployed);
    if (deployed.length === 0) throw new PolicyNotFound(ref, "labeler has no deployed version");
    deployed.sort((a, b) => String(a.created_at ?? "").localeCompare(String(b.created_at ?? "")));
    criteria = deployed[deployed.length - 1]?.criteria_text as string | undefined;
  }
  if (!criteria) throw new PolicyNotFound(ref, "resolved version has empty criteria");
  return criteria;
}

// --- github (public repos only) -------------------------------------------
async function githubFetch(ref: PolicyRef, fetchImpl: FetchImpl): Promise<string> {
  const parts = ref.locator.split("/");
  if (parts.length < 3 || parts.some((p) => !p)) {
    throw new PolicyNotFound(ref, "expected github:<owner>/<repo>/<path-to-file>");
  }
  const [owner, repo, ...rest] = parts;
  const path = rest.join("/");
  const gitRef = ref.version ?? "HEAD";
  const url = `https://raw.githubusercontent.com/${owner}/${repo}/${gitRef}/${path}`;
  const resp = await httpGet(url, {}, fetchImpl);
  if (resp.status === 404) throw new PolicyNotFound(ref, "file not found on GitHub (404)");
  if (resp.status === 401 || resp.status === 403) {
    throw new Error(`GitHub denied access fetching ${ref.raw} (HTTP ${resp.status}). Only public repos are supported.`);
  }
  if (!resp.ok) throw new Error(`GitHub fetch failed (HTTP ${resp.status}) for ${ref.raw}`);
  const text = await resp.text();
  if (!text.trim()) throw new PolicyNotFound(ref, "GitHub file is empty");
  return text;
}

// --- file -----------------------------------------------------------------
export function fileFetch(ref: PolicyRef, baseDir?: string): string {
  const p = ref.locator;
  const path = isAbsolute(p) ? p : join(baseDir ?? process.cwd(), p);
  return readFileSync(path, "utf8");
}

async function fetchCriteria(ref: PolicyRef, baseDir: string | undefined, fetchImpl: FetchImpl): Promise<string> {
  if (ref.scheme === "file") return fileFetch(ref, baseDir);
  if (ref.scheme === "zentropi") return zentropiFetch(ref, fetchImpl);
  if (ref.scheme === "github") return githubFetch(ref, fetchImpl);
  throw new Error(`Unknown policy scheme: ${ref.scheme}`);
}

// --- cache (same on-disk format as the Python policy_cache) ---------------
const DEFAULT_TTL_SECONDS = 3600;
const CACHEABLE_SCHEMES = new Set<string>(["zentropi", "github"]);

function cacheDir(): string {
  // Lazy so tests can point HOME at a temp dir; default matches the Python impl.
  return join(homedir(), ".reflexes", ".policy-cache");
}

function ttlSeconds(): number {
  const raw = process.env.REFLEXES_POLICY_CACHE_TTL;
  if (raw) {
    const n = parseInt(raw, 10);
    if (!Number.isNaN(n) && n >= 0) return n;
  }
  return DEFAULT_TTL_SECONDS;
}

function cachePath(ref: PolicyRef): string {
  const identity = `${ref.scheme}:${ref.locator}@${ref.version ?? ""}`;
  const digest = createHash("sha256").update(identity).digest("hex").slice(0, 32);
  return join(cacheDir(), `${ref.scheme}-${digest}.json`);
}

function readEntry(path: string): { criteria: string; fetched_at?: number } | null {
  try {
    const entry = JSON.parse(readFileSync(path, "utf8"));
    if (entry && typeof entry.criteria === "string") return entry;
  } catch {
    /* miss */
  }
  return null;
}

function writeEntry(path: string, entry: object): void {
  try {
    mkdirSync(cacheDir(), { recursive: true });
    const tmp = `${path}.${process.pid}.tmp`;
    writeFileSync(tmp, JSON.stringify(entry));
    renameSync(tmp, path); // atomic
  } catch {
    /* caching is best-effort */
  }
}

async function getOrFetch(ref: PolicyRef, fetch: () => Promise<string>): Promise<string> {
  if (!CACHEABLE_SCHEMES.has(ref.scheme)) return fetch();
  const path = cachePath(ref);
  const entry = readEntry(path);

  if (ref.isPinned) {
    if (entry) return entry.criteria; // immutable → permanent cache
    const criteria = await fetch();
    writeEntry(path, { criteria });
    return criteria;
  }

  // Unpinned → TTL + write-through + fail-safe.
  const now = Date.now() / 1000;
  if (entry && now - (entry.fetched_at ?? 0) < ttlSeconds()) return entry.criteria;
  let criteria: string;
  try {
    criteria = await fetch();
  } catch (e) {
    if (entry) return entry.criteria; // fail-safe to stale
    throw e;
  }
  writeEntry(path, { criteria, fetched_at: now });
  return criteria;
}

// --- public resolve -------------------------------------------------------
const FRONTMATTER_RE = /^\s*---\s*\n[\s\S]*?\n---\s*\n/;

export function stripFrontmatter(text: string): string {
  return text.replace(FRONTMATTER_RE, "").trim();
}

export interface ResolveOptions {
  baseDir?: string;
  fetchImpl?: FetchImpl;
}

export async function resolveCriteria(ref: PolicyRef, opts: ResolveOptions = {}): Promise<string> {
  const fetchImpl = opts.fetchImpl ?? fetch;
  const raw = await getOrFetch(ref, () => fetchCriteria(ref, opts.baseDir, fetchImpl));
  return stripFrontmatter(raw);
}

// Exposed for tests.
export const _internal = { cachePath, cacheDir, getOrFetch, fetchCriteria, ttlSeconds, CACHEABLE_SCHEMES };
