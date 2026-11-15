import { describe, expect, it } from "vitest";
import { safeNext } from "./safe-next";

describe("safeNext", () => {
  it.each([
    ["/c/123", "/c/123"],
    ["/invite/abc?x=1", "/invite/abc?x=1"],
    [null, "/"],
    ["", "/"],
    ["https://evil.test", "/"],
    ["//evil.test", "/"],
    ["/\\evil.test", "/"], // browsers treat /\ like //
    ["/evil.test", "/evil.test"], // a plain relative path is fine
    ["javascript:alert(1)", "/"],
  ])("%s -> %s", (input, out) => {
    expect(safeNext(input)).toBe(out);
  });
});
