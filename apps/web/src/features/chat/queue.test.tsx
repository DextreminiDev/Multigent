import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "@/lib/api/client";
import {
  applyEvent,
  initialState,
  lastAiMessageId,
  timeline,
  type LiveState,
  type ServerEvent,
} from "@/lib/ws/reducer";
import { MessageList, type TurnControls } from "./message-list";

function m(over: Partial<ChatMessage> & { id: string; seq: number }): ChatMessage {
  return {
    anchor_seq: over.seq, kind: "human", author_id: "u1", author_name: "Alice",
    author_color: "#000", content: "hi", status: "pending", turn_id: null,
    created_at: "2026-11-17T10:00:00Z", client_msg_id: null, ...over,
  };
}

const run = (s: LiveState, evs: ServerEvent[]) => evs.reduce((a, e) => applyEvent(a, e), s);

function stoppedState(): LiveState {
  return run(initialState(1, []), [
    { type: "message.created", seq: 2, payload: { message: m({ id: "a", seq: 2 }) } },
    { type: "turn.started", seq: 3, payload: { turn_id: "t1", message_ids: ["a"], author_ids: ["u1"] } },
    { type: "turn.delta", payload: { turn_id: "t1", idx: 0, text: "Half" } },
    {
      type: "turn.cancelled",
      seq: 4,
      payload: { turn_id: "t1", message_ids: ["a"], partial_text: "Half", cancelled_by: "u1" },
    },
  ]);
}

describe("reducer: stop / retry / regenerate / delete", () => {
  it("stop keeps the partial text as a 'stopped' card at the turn position", () => {
    const s = stoppedState();
    expect(s.turn).toBeNull();
    expect(s.messages.a.status).toBe("done");
    expect(s.failed.t1).toMatchObject({ kind: "stopped", partialText: "Half", anchorSeq: 3 });
    expect(timeline(s).map((i) => i.key)).toEqual(["a", "failed-t1"]);
  });

  it("retry removes the card and re-queues the batch", () => {
    const s = applyEvent(stoppedState(), {
      type: "turn.retried",
      seq: 5,
      payload: { turn_id: "t1", message_ids: ["a"] },
    });
    expect(s.failed.t1).toBeUndefined();
    expect(s.messages.a.status).toBe("pending");
  });

  it("regenerate hides the old reply and re-queues its batch", () => {
    let s = initialState(5, [
      m({ id: "a", seq: 2, status: "done", turn_id: "t1" }),
      m({ id: "x", seq: 4, anchor_seq: 3, kind: "ai", status: "done", turn_id: "t1" }),
    ]);
    expect(lastAiMessageId(s)).toBe("x");
    s = applyEvent(s, {
      type: "message.superseded",
      seq: 6,
      payload: { message_id: "x", turn_id: "t1", message_ids: ["a"] },
    });
    expect(timeline(s).map((i) => i.key)).toEqual(["a"]);
    expect(s.messages.a.status).toBe("pending");
    expect(lastAiMessageId(s)).toBeNull();
  });

  it("a deleted queued message disappears from the timeline", () => {
    let s = initialState(2, [m({ id: "a", seq: 2 })]);
    s = applyEvent(s, {
      type: "message.updated",
      seq: 3,
      payload: { message: m({ id: "a", seq: 2, status: "deleted", content: "" }) },
    });
    expect(timeline(s)).toEqual([]);
  });
});

describe("MessageList controls", () => {
  const base = {
    canSend: true,
    paused: false,
    onRetrySend: vi.fn(),
    onDiscardSend: vi.fn(),
    empty: null,
  };
  const controls = (isOwner: boolean): TurnControls => ({
    isOwner,
    onRetry: vi.fn(),
    onRegenerate: vi.fn(),
    onDelete: vi.fn(),
  });

  it("shows Retry on a stopped reply to its author, not to others", () => {
    const c = controls(false);
    const { unmount } = render(
      <MessageList {...base} state={stoppedState()} meId="u1" controls={c} />,
    );
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Retry/ }));
    expect(c.onRetry).toHaveBeenCalledWith("t1");
    unmount();
    render(<MessageList {...base} state={stoppedState()} meId="someone-else" controls={controls(false)} />);
    expect(screen.queryByRole("button", { name: /Retry/ })).toBeNull();
  });

  it("offers Regenerate to the owner on the last reply only", () => {
    const s = initialState(9, [
      m({ id: "a", seq: 2, status: "done", turn_id: "t1" }),
      m({ id: "x", seq: 4, anchor_seq: 3, kind: "ai", status: "done", turn_id: "t1", content: "old" }),
      m({ id: "b", seq: 5, status: "done", turn_id: "t2" }),
      m({ id: "y", seq: 7, anchor_seq: 6, kind: "ai", status: "done", turn_id: "t2", content: "new" }),
    ]);
    const c = controls(true);
    render(<MessageList {...base} state={s} meId="u1" controls={c} />);
    const buttons = screen.getAllByRole("button", { name: /Regenerate/ });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(c.onRegenerate).toHaveBeenCalledWith("y");
  });

  it("lets authors delete their own queued message", () => {
    const s = initialState(3, [m({ id: "a", seq: 2 }), m({ id: "b", seq: 3, author_id: "u2", author_name: "Bob" })]);
    const c = controls(false);
    render(<MessageList {...base} state={s} meId="u1" controls={c} />);
    const del = screen.getAllByRole("button", { name: "Delete this queued message" });
    expect(del).toHaveLength(1); // only mine
    fireEvent.click(del[0]);
    expect(c.onDelete).toHaveBeenCalledWith("a");
  });
});
