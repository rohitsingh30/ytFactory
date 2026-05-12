// Tests for web-next/lib/api.ts — Audit Q2.46 + Q2.54.
//
// Q2.46 — non-JSON 200 response must throw ApiError instead of
//   coercing HTML/text to whatever T claimed.
// Q2.54 — pollJob must (a) skip while document.visibilityState ===
//   "hidden" and (b) stop after maxConsecutiveErrors failures.
//
// Run with: cd web-next && npm test

import test from "node:test";
import assert from "node:assert/strict";
import { request, pollJob, ApiError } from "../lib/api.ts";

// Tiny fetch stub for these tests.
const _origFetch = globalThis.fetch;

async function withFetchStub(handler, fn) {
  globalThis.fetch = handler;
  try {
    return await fn();
  } finally {
    globalThis.fetch = _origFetch;
  }
}

function mkResponse(body, { status = 200, contentType = "application/json" } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "ERR",
    headers: { get: (k) => (k.toLowerCase() === "content-type" ? contentType : null) },
    json: async () => (typeof body === "string" ? JSON.parse(body) : body),
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  };
}

// ---- Q2.46 ----

test("Q2.46: non-JSON 200 throws ApiError with content-type in message", async () => {
  await withFetchStub(
    async () => mkResponse("<html>proxy error</html>", { contentType: "text/html" }),
    async () => {
      let err = null;
      try {
        await request("/api/x");
      } catch (e) {
        err = e;
      }
      assert.ok(err instanceof ApiError, "expected ApiError");
      assert.match(err.message, /expected JSON response/);
      assert.match(err.message, /text\/html/);
    },
  );
});

test("Q2.46: missing content-type still rejected", async () => {
  await withFetchStub(
    async () => mkResponse("plain text", { contentType: "" }),
    async () => {
      let err = null;
      try {
        await request("/api/x");
      } catch (e) {
        err = e;
      }
      assert.ok(err instanceof ApiError);
      assert.match(err.message, /unknown content-type/);
    },
  );
});

test("Q2.46: 200 with application/json still works", async () => {
  await withFetchStub(
    async () => mkResponse({ ok: true, channels: ["a"] }),
    async () => {
      const r = await request("/api/x");
      assert.deepEqual(r, { ok: true, channels: ["a"] });
    },
  );
});

// ---- Q2.54 ----

test("Q2.54: pollJob stops after maxConsecutiveErrors failures", async () => {
  let calls = 0;
  let err = null;
  await withFetchStub(
    async () => {
      calls += 1;
      throw new Error("network down");
    },
    async () => {
      const stop = pollJob(
        "j1",
        () => {},
        {
          intervalMs: 5,
          maxConsecutiveErrors: 3,
          onError: (e) => {
            err = e;
          },
        },
      );
      // Wait long enough for 3 ticks at 5ms each + microtask flush.
      await new Promise((r) => setTimeout(r, 200));
      stop();
    },
  );
  assert.equal(calls, 3, `expected 3 calls (gave up at maxConsecutiveErrors), got ${calls}`);
  assert.ok(err instanceof Error);
  assert.match(err.message, /network down/);
});

test("Q2.54: pollJob skips poll when document.visibilityState === 'hidden'", async () => {
  let calls = 0;
  // Stub document for the visibility check.
  globalThis.document = {
    visibilityState: "hidden",
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  try {
    await withFetchStub(
      async () => {
        calls += 1;
        return mkResponse({ id: "j1", state: "running" });
      },
      async () => {
        const stop = pollJob("j1", () => {}, { intervalMs: 1 });
        await new Promise((r) => setTimeout(r, 30));
        stop();
      },
    );
    assert.equal(calls, 0, `expected 0 calls while hidden, got ${calls}`);
  } finally {
    delete globalThis.document;
  }
});

test("Q2.54: visible tab + successful response keeps polling", async () => {
  let calls = 0;
  globalThis.document = {
    visibilityState: "visible",
    addEventListener: () => {},
    removeEventListener: () => {},
  };
  try {
    await withFetchStub(
      async () => {
        calls += 1;
        return mkResponse({ id: "j1", state: "running" });
      },
      async () => {
        const stop = pollJob("j1", () => {}, { intervalMs: 10 });
        await new Promise((r) => setTimeout(r, 200));
        stop();
      },
    );
    assert.ok(calls >= 2, `expected ≥2 polls, got ${calls}`);
  } finally {
    delete globalThis.document;
  }
});
