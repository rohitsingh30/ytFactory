import test from "node:test";
import assert from "node:assert/strict";

process.env.YTFACTORY_API_BASE = "http://ytfactory-api:8080";
const { GET } = await import("../app/api/[...path]/route.ts");

test("internal proxy preserves caller auth without contacting Google metadata", async () => {
  const original = globalThis.fetch;
  process.env.YTFACTORY_API_AUTH_MODE = "passthrough";
  const calls: { url: string; headers: Headers }[] = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), headers: new Headers(options?.headers) });
    return Response.json({ ok: true });
  };
  try {
    const response = await GET(new Request("https://ytfactory.nikamma.in/api/jobs?limit=2", {
      headers: { authorization: "Bearer caller-token", cookie: "yt_session=signed-cookie" },
    }), { params: { path: ["jobs"] } });
    assert.equal(response.status, 200);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "http://ytfactory-api:8080/api/jobs?limit=2");
    assert.equal(calls[0].headers.get("authorization"), "Bearer caller-token");
    assert.equal(calls[0].headers.get("cookie"), "yt_session=signed-cookie");
  } finally {
    globalThis.fetch = original;
    delete process.env.YTFACTORY_API_AUTH_MODE;
  }
});

test("anonymous internal requests never receive an agent bearer", async () => {
  const original = globalThis.fetch;
  process.env.YTFACTORY_API_AUTH_MODE = "passthrough";
  globalThis.fetch = async (_url, options) => {
    assert.equal(new Headers(options?.headers).get("authorization"), null);
    return Response.json({ error: "auth required" }, { status: 401 });
  };
  try {
    const response = await GET(new Request("https://ytfactory.nikamma.in/api/jobs"), {
      params: { path: ["jobs"] },
    });
    assert.equal(response.status, 401);
  } finally {
    globalThis.fetch = original;
    delete process.env.YTFACTORY_API_AUTH_MODE;
  }
});
