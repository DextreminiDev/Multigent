import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "@/lib/api/client";
import { parseIntent, splitTags, tagQueryAt, tagsIn } from "@/lib/tags";
import { Composer } from "./composer";
import { IntentBadge, TaggedText } from "./message-list";

describe("parseIntent (mirrors the server)", () => {
  const cases: [string, string, string][] = [
    ["hello", "reply", "context"],
    ["@ai what's 2+2?", "reply", "reply"],
    ["what's 2+2? @AI", "reply", "reply"],
    ["@skip just noting", "context", "context"],
    ["@off lunch?", "off", "off"],
    ["@ai and @skip", "context", "context"],
    ["@ai @off", "off", "off"],
    ["mail bob@ai.com", "reply", "context"],
    ["@aid @skipper @offline", "reply", "context"],
    ["(@ai) please", "reply", "reply"],
    ["@ai, thoughts?", "reply", "reply"],
  ];
  it.each(cases)("%s → always:%s tags:%s", (text, always, tags) => {
    expect(parseIntent(text, "always")).toBe(always);
    expect(parseIntent(text, "tags")).toBe(tags);
  });

  it("finds tags case-insensitively, not inside words", () => {
    expect([...tagsIn("@Ai hi @OFF x@skip")].sort()).toEqual(["ai", "off"]);
  });

  it("splits text into tag segments", () => {
    expect(splitTags("hey @ai, and @skip")).toEqual([
      { text: "hey ", tag: null },
      { text: "@ai", tag: "ai" },
      { text: ", and ", tag: null },
      { text: "@skip", tag: "skip" },
    ]);
  });

  it("detects a tag being typed at the caret", () => {
    expect(tagQueryAt("hi @s", 5)).toEqual({ start: 3, query: "s" });
    expect(tagQueryAt("@", 1)).toEqual({ start: 0, query: "" });
    expect(tagQueryAt("bob@a", 5)).toBeNull(); // email
    expect(tagQueryAt("hi @x", 5)).toBeNull(); // can't become a tag
  });
});

describe("Composer tags", () => {
  it("previews what sending will do in each mode", () => {
    const { rerender } = render(<Composer onSend={vi.fn()} replyMode="always" />);
    const box = screen.getByLabelText("Message");
    expect(screen.queryByTestId("intent-preview")).toBeNull();
    fireEvent.change(box, { target: { value: "hello" } });
    expect(screen.getByTestId("intent-preview")).toHaveTextContent("AI will reply");
    fireEvent.change(box, { target: { value: "hello @skip" } });
    expect(screen.getByTestId("intent-preview")).toHaveTextContent("No reply");
    fireEvent.change(box, { target: { value: "@off hello @ai" } });
    expect(screen.getByTestId("intent-preview")).toHaveTextContent("hidden from AI");

    rerender(<Composer onSend={vi.fn()} replyMode="tags" />);
    fireEvent.change(box, { target: { value: "hello" } });
    expect(screen.getByTestId("intent-preview")).toHaveAttribute("data-intent", "context");
    fireEvent.change(box, { target: { value: "@ai hello" } });
    expect(screen.getByTestId("intent-preview")).toHaveAttribute("data-intent", "reply");
  });

  it("suggests tags after @ and inserts the picked one", () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const box = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: "lunch? @", selectionStart: 8 } });
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
      "@aiAsk the AI to reply",
      "@skipNo reply, but the AI sees it",
      "@offNo reply, hidden from the AI",
    ]);
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "Enter" }); // picks, doesn't send
    expect(onSend).not.toHaveBeenCalled();
    expect(box).toHaveValue("lunch? @off ");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("filters suggestions and Escape closes them", () => {
    const onSend = vi.fn();
    render(<Composer onSend={onSend} />);
    const box = screen.getByLabelText("Message");
    fireEvent.change(box, { target: { value: "@s", selectionStart: 2 } });
    expect(screen.getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.keyDown(box, { key: "Enter" }); // closed: Enter sends as typed
    expect(onSend).toHaveBeenCalledWith("@s");
  });
});

describe("Message tags in the list", () => {
  it("highlights tags and explains no-reply messages", () => {
    const { container } = render(<TaggedText text="@off see you at 6" />);
    expect(container.querySelector('[data-tag="off"]')).toHaveTextContent("@off");
    render(<IntentBadge intent={"off" as ChatMessage["intent"]} />);
    expect(screen.getByText(/hidden from the AI/)).toBeInTheDocument();
    render(<IntentBadge intent="context" />);
    expect(screen.getByText(/No reply/)).toBeInTheDocument();
    const { container: none } = render(<IntentBadge intent="reply" />);
    expect(none).toBeEmptyDOMElement();
  });
});
