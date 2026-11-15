import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Markdown } from "@/components/markdown";
import type { ChatMessage } from "@/lib/api/client";
import { applyEvent, initialState } from "@/lib/ws/reducer";
import { Composer } from "./composer";
import { MessageList } from "./message-list";

describe("Composer", () => {
  it("Enter sends trimmed text, Shift+Enter does not", () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const box = screen.getByLabelText("Message");
    fireEvent.change(box, { target: { value: "  hello\nworld  " } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello\nworld");
    expect(box).toHaveValue("");
  });

  it("won't send blank or over-long messages", () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const box = screen.getByLabelText("Message");
    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.change(box, { target: { value: "x".repeat(8001) } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByText(/8,001 \/ 8,000/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  });

  it("shows the reason instead of an input when sending is blocked", () => {
    render(<Composer onSend={vi.fn()} disabledReason="You're a viewer here" />);
    expect(screen.queryByLabelText("Message")).toBeNull();
    expect(screen.getByText("You're a viewer here")).toBeInTheDocument();
  });
});

describe("Markdown (untrusted model output)", () => {
  it("never renders raw HTML or script", () => {
    const { container } = render(
      <Markdown text={'Hi <script>alert(1)</script> <img src=x onerror="alert(2)"> <b>bold?</b>'} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });

  it("neutralizes javascript: links and hardens real links", () => {
    const { container } = render(
      <Markdown text={"[bad](javascript:alert(1)) and [good](https://example.com)"} />,
    );
    const links = [...container.querySelectorAll("a")];
    expect(links.some((a) => (a.getAttribute("href") ?? "").startsWith("javascript"))).toBe(false);
    const good = links.find((a) => a.textContent === "good")!;
    expect(good.getAttribute("rel")).toContain("noopener");
    expect(good.getAttribute("target")).toBe("_blank");
  });

  it("shows images as links rather than loading them", () => {
    const { container } = render(<Markdown text={"![tracker](https://evil.test/p.gif)"} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("[image: tracker]")).toBeInTheDocument();
  });

  it("renders GFM tables and fenced code with a copy button", () => {
    render(<Markdown streaming text={"| a | b |\n|---|---|\n| 1 | 2 |\n\n```py\nprint('<hi>')\n```"} />);
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("print('<hi>')")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
  });
});

function m(over: Partial<ChatMessage> & { id: string; seq: number }): ChatMessage {
  return {
    anchor_seq: over.seq,
    kind: "human",
    author_id: "u1",
    author_name: "Alice",
    author_color: "#e5484d",
    content: "hi",
    status: "pending",
    turn_id: null,
    created_at: "2026-11-15T14:00:00Z",
    client_msg_id: null,
    ...over,
  };
}

describe("MessageList", () => {
  const props = {
    meId: "u1",
    canSend: true,
    paused: false,
    onRetrySend: vi.fn(),
    onDiscardSend: vi.fn(),
    empty: <p>empty</p>,
  };

  it("streams with Replying-to chips and marks mid-turn messages as queued", () => {
    let s = initialState(1, []);
    s = applyEvent(s, { type: "message.created", seq: 2, payload: { message: m({ id: "a", seq: 2 }) } });
    s = applyEvent(s, {
      type: "message.created",
      seq: 3,
      payload: { message: m({ id: "b", seq: 3, author_id: "u2", author_name: "Bob" }) },
    });
    s = applyEvent(s, {
      type: "turn.started",
      seq: 4,
      payload: { turn_id: "t1", message_ids: ["a", "b"], author_ids: ["u1", "u2"] },
    });
    s = applyEvent(s, { type: "turn.delta", payload: { turn_id: "t1", idx: 0, text: "**Alice**, hi" } });
    s = applyEvent(s, {
      type: "message.created",
      seq: 5,
      payload: { message: m({ id: "c", seq: 5, author_id: "u2", author_name: "Bob", content: "also" }) },
    });
    render(<MessageList state={s} {...props} />);
    const reply = screen.getByRole("article", { name: "Assistant is replying" });
    expect(reply).toHaveTextContent("Replying toAliceBob");
    expect(reply).toHaveTextContent("Alice, hi");
    expect(screen.getByText("Queued · will be answered next")).toBeInTheDocument();
    expect(screen.getByText("You")).toBeInTheDocument(); // own message labelled
  });

  it("shows a friendly failure with partial text", () => {
    let s = initialState(1, []);
    s = applyEvent(s, { type: "message.created", seq: 2, payload: { message: m({ id: "a", seq: 2 }) } });
    s = applyEvent(s, {
      type: "turn.started",
      seq: 3,
      payload: { turn_id: "t1", message_ids: ["a"], author_ids: ["u1"] },
    });
    s = applyEvent(s, {
      type: "turn.failed",
      seq: 4,
      payload: {
        turn_id: "t1",
        error_code: "rate_limit",
        error_message: "The model's rate limit was hit.",
        partial_text: "Half an",
        message_ids: ["a"],
        requeued: false,
      },
    });
    render(<MessageList state={s} {...props} />);
    expect(screen.getByText("The model's rate limit was hit.")).toBeInTheDocument();
    expect(screen.getByText("Half an")).toBeInTheDocument();
  });

  it("renders the empty state", () => {
    render(<MessageList state={initialState(0, [])} {...props} />);
    expect(screen.getByText("empty")).toBeInTheDocument();
  });
});
