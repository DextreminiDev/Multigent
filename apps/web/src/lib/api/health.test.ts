import { describe, expect, it, vi } from "vitest";
import { fetchReadiness } from "./health";

describe("fetchReadiness", () => {
  it("returns parsed readiness", async () => {
    const body = { status: "ok", postgres: true, redis: true };
    const f = vi.fn().mockResolvedValue(new Response(JSON.stringify(body)));
    await expect(fetchReadiness(f, "http://api")).resolves.toEqual(body);
    expect(f).toHaveBeenCalledWith("http://api/readyz", { cache: "no-store" });
  });

  it("returns null when the API is unreachable", async () => {
    const f = vi.fn().mockRejectedValue(new TypeError("network"));
    await expect(fetchReadiness(f, "http://api")).resolves.toBeNull();
  });
});
