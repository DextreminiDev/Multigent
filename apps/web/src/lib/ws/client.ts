/**
 * Singleton WebSocket client (ARCHITECTURE.md §8, §13).
 *
 * - One socket per tab, multiplexing every open chat.
 * - Auto-reconnect with exponential backoff + jitter; on reconnect each chat is
 *   re-subscribed from its current `lastSeq`, so the server replays exactly what
 *   was missed.
 * - Every client frame carries a `ref`; `ack`/`error` frames resolve it.
 * - Outbox: `message.send` frames stay queued until acked and are re-sent after a
 *   reconnect. The server dedupes on `client_msg_id`, so this is safe.
 */
import type { Mention } from "@/lib/mentions";
import type { ServerEvent } from "./reducer";

export type ConnStatus = "idle" | "connecting" | "open" | "reconnecting";

export type Ack = { type: "ack"; ref: string; [k: string]: unknown };

export class WsRequestError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "WsRequestError";
  }
}

type Sub = { chatId: string; afterSeq: () => number; onEvent: (ev: ServerEvent) => void };
type Pending = { resolve: (a: Ack) => void; reject: (e: Error) => void; send?: string };
type OutboxEntry = { frame: Record<string, unknown>; pending: Pending };

type WsLike = {
  readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
};
export type WsFactory = (url: string) => WsLike;

const OPEN = 1;

export class WsClient {
  private ws: WsLike | null = null;
  private status: ConnStatus = "idle";
  private attempt = 0;
  private refN = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private subs = new Map<string, Sub>();
  private pending = new Map<string, Pending>();
  private outbox = new Map<string, OutboxEntry>(); // client_msg_id -> entry
  private statusListeners = new Set<(s: ConnStatus) => void>();

  constructor(
    private readonly url: string,
    private readonly factory: WsFactory = (u) => new WebSocket(u) as unknown as WsLike,
    private readonly random: () => number = Math.random,
  ) {}

  // ---------------------------------------------------------------- status

  getStatus(): ConnStatus {
    return this.status;
  }

  onStatus(fn: (s: ConnStatus) => void): () => void {
    this.statusListeners.add(fn);
    return () => this.statusListeners.delete(fn);
  }

  private setStatus(s: ConnStatus) {
    this.status = s;
    for (const fn of this.statusListeners) fn(s);
  }

  // ---------------------------------------------------------------- lifecycle

  private connect() {
    if (this.ws) return;
    this.setStatus(this.attempt === 0 ? "connecting" : "reconnecting");
    const ws = this.factory(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.setStatus("open");
      for (const sub of this.subs.values()) this.sendSubscribe(sub);
      for (const entry of this.outbox.values()) this.transmit(entry.frame, entry.pending);
    };
    ws.onmessage = (e) => this.handle(String(e.data));
    ws.onerror = () => {};
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      // in-flight non-send requests can't be answered any more
      for (const [ref, p] of this.pending) {
        if (!p.send) p.reject(new WsRequestError("disconnected", "Connection lost"));
        this.pending.delete(ref);
      }
      if (this.subs.size === 0 && this.outbox.size === 0) {
        this.setStatus("idle");
        return;
      }
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect() {
    this.setStatus("reconnecting");
    const base = Math.min(30_000, 500 * 2 ** this.attempt);
    const delay = base / 2 + this.random() * (base / 2); // jitter
    this.attempt += 1;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  /** Reconnect now (e.g. browser came back online). */
  kick() {
    if (this.ws || this.status === "idle") return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.connect();
  }

  close() {
    if (this.timer) clearTimeout(this.timer);
    this.subs.clear();
    this.outbox.clear();
    const ws = this.ws;
    this.ws = null;
    ws?.close();
    this.setStatus("idle");
  }

  // ---------------------------------------------------------------- frames

  private nextRef() {
    this.refN += 1;
    return `c${this.refN}`;
  }

  private transmit(frame: Record<string, unknown>, pending?: Pending) {
    if (!this.ws || this.ws.readyState !== OPEN) return false;
    const ref = this.nextRef();
    if (pending) this.pending.set(ref, pending);
    this.ws.send(JSON.stringify({ ...frame, ref }));
    return true;
  }

  private sendSubscribe(sub: Sub) {
    this.transmit({ type: "subscribe", chat_id: sub.chatId, after_seq: sub.afterSeq() });
  }

  private handle(raw: string) {
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(raw);
    } catch {
      return;
    }
    const ref = frame.ref as string | undefined;
    if ((frame.type === "ack" || frame.type === "error") && ref && this.pending.has(ref)) {
      const p = this.pending.get(ref)!;
      this.pending.delete(ref);
      if (p.send) this.outbox.delete(p.send);
      if (frame.type === "ack") p.resolve(frame as Ack);
      else p.reject(new WsRequestError(String(frame.code), String(frame.message ?? "Request failed")));
      return;
    }
    if (frame.type === "ack" || frame.type === "error") return;
    const chatId = frame.chat_id as string | undefined;
    if (chatId) this.subs.get(chatId)?.onEvent(frame as ServerEvent);
  }

  // ---------------------------------------------------------------- public API

  /** Subscribe to a chat's live events. `afterSeq` is read at every (re)subscribe. */
  subscribe(chatId: string, afterSeq: () => number, onEvent: (ev: ServerEvent) => void) {
    const sub: Sub = { chatId, afterSeq, onEvent };
    this.subs.set(chatId, sub);
    if (this.ws?.readyState === OPEN) this.sendSubscribe(sub);
    else this.connect();
    return () => {
      if (this.subs.get(chatId) !== sub) return;
      this.subs.delete(chatId);
      this.transmit({ type: "unsubscribe", chat_id: chatId });
    };
  }

  /** Re-run the subscribe/replay handshake (after a resync reload). */
  resubscribe(chatId: string) {
    const sub = this.subs.get(chatId);
    if (sub) this.sendSubscribe(sub);
  }

  /** Fire-and-forget "I'm typing" (dropped while disconnected; it's ephemeral). */
  typing(chatId: string) {
    this.transmit({ type: "typing", chat_id: chatId });
  }

  /** Send a message; survives reconnects until the server acks it. */
  sendMessage(
    chatId: string,
    clientMsgId: string,
    content: string,
    mentions: Mention[] = [],
    off = false,
  ): Promise<Ack> {
    return new Promise<Ack>((resolve, reject) => {
      const pending: Pending = { resolve, reject, send: clientMsgId };
      const frame = {
        type: "message.send",
        chat_id: chatId,
        client_msg_id: clientMsgId,
        content,
        mentions: mentions.map(({ type, offset, length, user_id }) =>
          user_id ? { type, offset, length, user_id } : { type, offset, length },
        ),
        off,
      };
      this.outbox.set(clientMsgId, { frame, pending });
      if (!this.transmit(frame, pending)) this.connect();
    });
  }
}

let singleton: WsClient | null = null;

export function wsUrl(apiUrl: string): string {
  return `${apiUrl.replace(/^http/, "ws").replace(/\/$/, "")}/ws`;
}

export function getWsClient(apiUrl: string): WsClient {
  if (!singleton) {
    singleton = new WsClient(wsUrl(apiUrl));
    if (typeof window !== "undefined") {
      window.addEventListener("online", () => singleton?.kick());
    }
  }
  return singleton;
}
