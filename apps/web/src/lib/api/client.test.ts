import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setCsrfToken, unwrap } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken(null);
});

function stubFetch(status: number, body: unknown) {
  const f = vi.fn(async (req: Request) => {
    void req;
    return new Response(JSON.stringify(body), { status });
  });
  vi.stubGlobal("fetch", f);
  return f;
}

describe("api client", () => {
  it("sends credentials and the CSRF header on writes only", async () => {
    const f = stubFetch(201, { id: "x" });
    setCsrfToken("tok123");
    await api.POST("/chats", { body: {} });
    await api.GET("/chats");
    const [post, get] = f.mock.calls.map((c) => c[0] as Request);
    expect(post.method).toBe("POST");
    expect(post.headers.get("X-CSRF-Token")).toBe("tok123");
    expect(post.credentials).toBe("include");
    expect(get.headers.get("X-CSRF-Token")).toBeNull();
  });

  it("unwrap maps error bodies to ApiError", async () => {
    stubFetch(422, { error: { code: "key_invalid", message: "nope" } });
    const err = await unwrap(api.GET("/chats")).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 422, code: "key_invalid", message: "nope" });
  });

  it("unwrap maps network failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await expect(unwrap(api.GET("/chats"))).rejects.toMatchObject({ code: "network", status: 0 });
  });
});
