import { describe, expect, it } from "vitest";
import type { ChatMessage } from "@/lib/api/client";
import {
  ackSend,
  addPendingSend,
  applyEvent,
  initialState,
  queuedCount,
  timeline,
  type LiveState,
  type ServerEvent,
} from "./reducer";

function msg(over: Partial<ChatMessage> & { id: string; seq: number }): ChatMessage {
  return {
    anchor_seq: over.seq,
    kind: "human",
    author_id: "u1",
    author_name: "Alice",
    author_color: "#000",
    content: "hi",
    status: "pending",
    turn_id: null,
    created_at: "2026-11-15T14:00:00Z",
    client_msg_id: null,
    ...over,
  };
}

const run = (s: LiveState, evs: ServerEvent[]) => evs.reduce(applyEvent, s);

const created = (seq: number, m: ChatMessage): ServerEvent => ({
  type: "message.created",
  seq,
  payload: { message: m },
});

describe("seq handling", () => {
  it("ignores duplicates and flags gaps", () => {
    const s0 = initialState(5, []);
    const s1 = applyEvent(s0, created(6, msg({ id: "a", seq: 6 })));
    expect(s1.lastSeq).toBe(6);
    expect(applyEvent(s1, created(6, msg({ id: "a", seq: 6 })))).toBe(s1); // replay overlap
    expect(applyEvent(s1, created(4, msg({ id: "z", seq: 4 })))).toBe(s1);
    const gap = applyEvent(s1, created(9, msg({ id: "b", seq: 9 })));
    expect(gap.needsResync).toBe(true);
    expect(gap.messages.b).toBeUndefined();
  });

  it("resync_required sets the flag", () => {
    expect(applyEvent(initialState(0, []), { type: "resync_required" }).needsResync).toBe(true);
  });
});

describe("turn lifecycle", () => {
  const a = msg({ id: "a", seq: 2, author_name: "Alice" });
  const b = msg({ id: "b", seq: 3, author_name: "Bob", author_id: "u2" });
  const started: ServerEvent = {
    type: "turn.started",
    seq: 4,
    payload: { turn_id: "t1", message_ids: ["a", "b"], author_ids: ["u1", "u2"] },
  };
  const delta = (idx: number, text: string): ServerEvent => ({
    type: "turn.delta",
    payload: { turn_id: "t1", idx, text },
  });

  it("streams, dedupes deltas by idx, and completes", () => {
    let s = run(initialState(1, []), [created(2, a), created(3, b), started]);
    expect(s.messages.a.status).toBe("batched");
    s = run(s, [delta(0, "Hel"), delta(1, "lo"), delta(1, "lo"), delta(0, "Hel")]);
    expect(s.turn?.text).toBe("Hello");
    const ai = msg({ id: "x", seq: 6, anchor_seq: 4, kind: "ai", content: "Hello", status: "done" });
    s = applyEvent(s, {
      type: "turn.completed",
      seq: 5,
      payload: { turn_id: "t1", message: ai, message_ids: ["a", "b"] },
    });
    // NB: seq 5 is the event; the AI row's own seq is irrelevant to ordering here
    expect(s.turn).toBeNull();
    expect(s.messages.a.status).toBe("done");
    expect(s.messages.x.content).toBe("Hello");
    expect(s.completedCount).toBe(1);
  });

  it("joins mid-stream: snapshot then overlapping live deltas, no gaps or dupes", () => {
    let s = run(initialState(1, []), [created(2, a), created(3, b), started]);
    expect(s.needsResync).toBe(false);
    s = applyEvent(s, {
      type: "turn.snapshot",
      payload: { turn_id: "t1", chunks: [{ idx: 0, text: "A" }, { idx: 1, text: "B" }] },
    });
    s = run(s, [delta(1, "B"), delta(2, "C"), delta(3, "D")]);
    expect(s.turn?.text).toBe("ABCD");
  });

  it("snapshot before turn.started keeps its chunks", () => {
    let s = applyEvent(initialState(3, [a]), {
      type: "turn.snapshot",
      payload: { turn_id: "t1", chunks: [{ idx: 0, text: "A" }] },
    });
    s = applyEvent(s, started);
    expect(s.turn?.text).toBe("A");
    expect(s.turn?.messageIds).toEqual(["a", "b"]);
  });

  it("requeued failure puts the batch back to pending", () => {
    let s = run(initialState(1, []), [created(2, a), created(3, b), started]);
    s = applyEvent(s, {
      type: "turn.failed",
      seq: 5,
      payload: { turn_id: "t1", error_code: "auth", error_message: "Key", message_ids: ["a"], requeued: true },
    });
    expect(s.turn).toBeNull();
    expect(s.messages.a.status).toBe("pending");
    expect(s.failed.t1.anchorSeq).toBe(4);
    expect(queuedCount(s)).toBe(1);
  });
});

describe("timeline order", () => {
  it("places an AI reply at its turn start, before messages sent mid-turn", () => {
    const first = msg({ id: "a", seq: 2, status: "done" });
    const mid = msg({ id: "b", seq: 4, status: "pending" }); // sent while streaming
    const ai = msg({ id: "x", seq: 5, anchor_seq: 3, kind: "ai", status: "done" });
    const s = initialState(5, [mid, ai, first]);
    expect(timeline(s).map((i) => i.key)).toEqual(["a", "x", "b"]);
  });

  it("puts the streaming bubble at the turn start and local sends last", () => {
    let s = initialState(1, []);
    s = applyEvent(s, created(2, msg({ id: "a", seq: 2 })));
    s = applyEvent(s, {
      type: "turn.started",
      seq: 3,
      payload: { turn_id: "t1", message_ids: ["a"], author_ids: ["u1"] },
    });
    s = applyEvent(s, created(4, msg({ id: "b", seq: 4 })));
    s = addPendingSend(s, { clientMsgId: "c1", content: "x", createdAt: "", state: "sending" });
    expect(timeline(s).map((i) => i.key)).toEqual(["a", "turn-t1", "b", "send-c1"]);
  });

  it("acked send replaces the optimistic copy exactly once", () => {
    let s = addPendingSend(initialState(1, []), {
      clientMsgId: "c1",
      content: "hi",
      createdAt: "",
      state: "sending",
    });
    const m = msg({ id: "a", seq: 2, client_msg_id: "c1" });
    s = ackSend(s, m);
    expect(s.pendingSends).toEqual({});
    s = applyEvent(s, created(2, m)); // event arrives after the ack
    expect(Object.keys(s.messages)).toEqual(["a"]);
    expect(s.lastSeq).toBe(2);
  });
});
