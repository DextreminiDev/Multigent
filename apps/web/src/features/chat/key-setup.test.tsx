import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Chat } from "@/lib/api/client";
import { ApiError } from "@/lib/api/client";
import { KeySetup, keyErrorMessage } from "./key-setup";
import { isChatReady, setupSteps } from "./setup-checklist";

const mutate = vi.fn();
const putState: { isError: boolean; error: unknown; isPending: boolean } = {
  isError: false,
  error: null,
  isPending: false,
};

vi.mock("@/lib/api/hooks", () => ({
  useProviders: () => ({
    data: [
      { id: "gemini", label: "Google Gemini", needs_base_url: false, key_help_url: "https://aistudio.google.com/app/apikey" },
      { id: "openai_compatible", label: "OpenAI-compatible", needs_base_url: true, key_help_url: null },
    ],
  }),
  usePutKey: () => ({ mutate, ...putState }),
  useDeleteKey: () => ({ mutate: vi.fn(), isPending: false }),
  useValidateKey: () => ({ mutate: vi.fn(), isPending: false }),
  useModels: () => ({ isPending: true }),
  usePatchChat: () => ({ mutate: vi.fn(), isPending: false }),
}));

function chat(over: Partial<Chat> = {}): Chat {
  return {
    id: "c1",
    title: "T",
    role: "owner",
    owner: { id: "u1", name: "Ann", avatar_color: "#000" },
    status: "active",
    provider: null,
    model: null,
    max_output_tokens: 2048,
    member_count: 1,
    key: null,
    last_seq: 1,
    created_at: "",
    updated_at: "",
    ...over,
  };
}

beforeEach(() => {
  mutate.mockReset();
  Object.assign(putState, { isError: false, error: null, isPending: false });
});

describe("KeySetup", () => {
  it("shows the form with a Gemini free-key hint when no key is set", () => {
    render(<KeySetup chat={chat()} />);
    expect(screen.getByLabelText("API key")).toHaveAttribute("type", "password");
    expect(screen.getByText(/free Gemini key/i)).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("submits trimmed key and asks for base URL only when needed", () => {
    render(<KeySetup chat={chat()} />);
    expect(screen.queryByLabelText("Base URL")).toBeNull();
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "  AIzaKEY123  " } });
    fireEvent.click(screen.getByRole("button", { name: /save & verify/i }));
    expect(mutate).toHaveBeenCalledWith(
      { provider: "gemini", api_key: "AIzaKEY123", base_url: undefined },
      expect.anything(),
    );
    fireEvent.click(screen.getByLabelText("OpenAI-compatible"));
    expect(screen.getByLabelText("Base URL")).toBeRequired();
  });

  it("only ever shows the masked last4 once a key exists", () => {
    render(
      <KeySetup
        chat={chat({ key: { provider: "gemini", status: "ok", last4: "9xYz", validated_at: null } })}
      />,
    );
    expect(screen.getByText("9xYz")).toBeInTheDocument();
    expect(screen.getByText("Key ending in")).toHaveClass("sr-only");
    expect(screen.getByText("Verified")).toBeInTheDocument();
    expect(screen.queryByLabelText("API key")).toBeNull();
  });

  it("explains a rejected key", () => {
    render(
      <KeySetup
        chat={chat({ key: { provider: "gemini", status: "invalid", last4: "abcd", validated_at: null } })}
      />,
    );
    expect(screen.getByText(/no longer accepts this key/i)).toBeInTheDocument();
  });

  it("renders provider errors in plain language", () => {
    Object.assign(putState, { isError: true, error: new ApiError(422, "key_invalid", "x") });
    render(<KeySetup chat={chat()} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/rejected this key/i);
  });
});

describe("setup state", () => {
  it("is ready only with a usable key and a model", () => {
    const key = { provider: "gemini", status: "ok" as const, last4: "1", validated_at: null };
    expect(isChatReady(chat())).toBe(false);
    expect(isChatReady(chat({ key }))).toBe(false);
    expect(isChatReady(chat({ key, model: "m" }))).toBe(true);
    expect(isChatReady(chat({ key: { ...key, status: "invalid" }, model: "m" }))).toBe(false);
    expect(setupSteps(chat({ key, model: "m" })).map((s) => s.done)).toEqual([
      true,
      true,
      true,
      false,
    ]);
  });

  it("maps error codes", () => {
    expect(keyErrorMessage(new ApiError(422, "validation_error", ""))).toMatch(/doesn't look like/);
    expect(keyErrorMessage(new Error("x"))).toBe("Something went wrong.");
  });
});
