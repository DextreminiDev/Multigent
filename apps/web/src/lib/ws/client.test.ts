import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WsClient, WsRequestError, wsUrl, type WsFactory } from "./client";

class FakeSocket {
  static all: FakeSocket[] = [];
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: ((e: unknown) => void) | null = null;
  onclose: ((e: unknown) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: unknown }) => void) | null = null;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.drop();
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  drop() {
    this.readyState = 3;
    this.onclose?.({});
  }
  push(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
  last(type: string) {
    return [...this.sent].reverse().find((f) => f.type === type)!;
  }
}

const factory: WsFactory = (u) => new FakeSocket(u);

beforeEach(() => {
  FakeSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

const latest = () => FakeSocket.all[FakeSocket.all.length - 1];

describe("WsClient", () => {
  it("derives the ws url", () => {
    expect(wsUrl("http://localhost:8000")).toBe("ws://localhost:8000/ws");
    expect(wsUrl("https://api.example.com/")).toBe("wss://api.example.com/ws");
  });

  it("subscribes on open and routes events by chat", () => {
    const c = new WsClient("ws://x/ws", factory, () => 0.5);
    const got: unknown[] = [];
    c.subscribe("c1", () => 7, (e) => got.push(e));
    latest().open();
    expect(latest().last("subscribe")).toMatchObject({ chat_id: "c1", after_seq: 7 });
    latest().push({ type: "message.created", chat_id: "c1", seq: 8, payload: {} });
    latest().push({ type: "message.created", chat_id: "other", seq: 1, payload: {} });
    expect(got).toHaveLength(1);
  });

  it("reconnects with backoff and resubscribes from the current lastSeq", () => {
    const c = new WsClient("ws://x/ws", factory, () => 0.5);
    let seq = 3;
    c.subscribe("c1", () => seq, () => {});
    latest().open();
    seq = 10;
    const statuses: string[] = [];
    c.onStatus((s) => statuses.push(s));
    latest().drop();
    expect(statuses).toEqual(["reconnecting"]);
    expect(FakeSocket.all).toHaveLength(1);
    vi.advanceTimersByTime(400); // base 500ms * jitter 0.75 = 375ms
    expect(FakeSocket.all).toHaveLength(2);
    latest().open();
    expect(latest().last("subscribe")).toMatchObject({ after_seq: 10 });
    expect(c.getStatus()).toBe("open");
  });

  it("sends mentions and the off-the-record flag, and re-sends them unchanged", () => {
    const c = new WsClient("ws://x/ws", factory, () => 0.5);
    c.subscribe("c1", () => 0, () => {});
    latest().open();
    void c.sendMessage(
      "c1",
      "m1",
      "@Bob hi",
      [{ type: "user", offset: 0, length: 4, user_id: "u2", name: "Bob" }],
      true,
    );
    const frame = latest().last("message.send");
    // the server sets names; the client only sends the range and who
    expect(frame).toMatchObject({
      content: "@Bob hi",
      mentions: [{ type: "user", offset: 0, length: 4, user_id: "u2" }],
      off: true,
    });
    expect((frame.mentions as Record<string, unknown>[])[0]).not.toHaveProperty("name");
    latest().drop();
    vi.advanceTimersByTime(1000);
    latest().open();
    expect(latest().last("message.send")).toMatchObject({ off: true, mentions: frame.mentions });
  });

  it("re-sends unacked messages after reconnect and resolves on ack", async () => {
    const c = new WsClient("ws://x/ws", factory, () => 0.5);
    c.subscribe("c1", () => 0, () => {});
    latest().open();
    const p = c.sendMessage("c1", "m1", "hello");
    const first = latest().last("message.send");
    expect(first).toMatchObject({ client_msg_id: "m1", content: "hello" });
    latest().drop();
    vi.advanceTimersByTime(1000);
    latest().open();
    const resent = latest().last("message.send");
    expect(resent).toMatchObject({ client_msg_id: "m1" });
    latest().push({ type: "ack", ref: resent.ref, created: false, message: { id: "x" } });
    await expect(p).resolves.toMatchObject({ created: false });
    // acked -> not re-sent on the next reconnect
    latest().drop();
    vi.advanceTimersByTime(1000);
    latest().open();
    expect(latest().sent.filter((f) => f.type === "message.send")).toHaveLength(0);
  });

  it("rejects a send the server refuses", async () => {
    const c = new WsClient("ws://x/ws", factory);
    c.subscribe("c1", () => 0, () => {});
    latest().open();
    const p = c.sendMessage("c1", "m1", "x");
    latest().push({ type: "error", ref: latest().last("message.send").ref, code: "forbidden", message: "nope" });
    await expect(p).rejects.toBeInstanceOf(WsRequestError);
    await expect(p).rejects.toMatchObject({ code: "forbidden" });
  });

  it("queues a send made while disconnected", () => {
    const c = new WsClient("ws://x/ws", factory);
    void c.sendMessage("c1", "m1", "early");
    expect(latest().sent).toHaveLength(0);
    latest().open();
    expect(latest().last("message.send")).toMatchObject({ client_msg_id: "m1" });
  });
});
