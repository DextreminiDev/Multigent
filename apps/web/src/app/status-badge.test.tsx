import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StatusBadge } from "./status-badge";

afterEach(() => vi.unstubAllGlobals());

describe("StatusBadge", () => {
  it("shows ready when the backend is healthy", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ status: "ok", postgres: true, redis: true })),
      ),
    );
    render(<StatusBadge />);
    expect(await screen.findByRole("status")).toHaveTextContent("Backend ready");
  });

  it("shows unavailable when the backend is down", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("down")));
    render(<StatusBadge />);
    expect(await screen.findByRole("status")).toHaveTextContent("Backend unavailable");
  });
});
