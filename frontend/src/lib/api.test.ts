import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, createSite, getSite, hostedUrl } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

function respondWith(status: number, body: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(body, { status })),
  );
}

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  const err = await promise.then(
    () => null,
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(ApiError);
  return err as ApiError;
}

describe("request", () => {
  it("joins the configured base URL without a double slash", () => {
    expect(hostedUrl("s1")).toBe("https://api.test/sites/s1/llms.txt");
  });

  it("sends JSON only when there is a body", async () => {
    const fetchMock = vi.fn(async () => new Response('{"site_id":"s1","job_id":"j1"}'));
    vi.stubGlobal("fetch", fetchMock);
    await createSite("acme.com");
    await getSite("s1");
    const [post, get] = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(post[1].headers).toEqual({ "Content-Type": "application/json" });
    // A Content-Type on a GET would force a CORS preflight for nothing.
    expect(get[1].headers).toBeUndefined();
  });

  it.each([
    ["an HTTPException's detail", '{"detail":"Not a valid site."}', "Not a valid site."],
    ["a validation error's first message", '{"detail":[{"msg":"Field required"}]}', "Field required"],
    ["a non-JSON error page", "<html>Bad gateway</html>", "Request failed (HTTP 502)."],
  ])("shows %s", async (_, body, message) => {
    respondWith(body.startsWith("<") ? 502 : 422, body);
    const err = await failure(createSite("x"));
    expect(err.message).toBe(message);
    expect(err.status).toBe(body.startsWith("<") ? 502 : 422);
  });

  it("reports a network failure with no status, so callers treat it as transient", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const err = await failure(getSite("s1"));
    expect(err.status).toBeNull();
    expect(err.message).toMatch(/network or CORS/);
  });

  it("gives up on a request that never answers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new DOMException("signal timed out", "TimeoutError");
      }),
    );
    const err = await failure(getSite("s1"));
    expect(err.status).toBeNull();
    expect(err.message).toMatch(/took too long/);
  });
});
