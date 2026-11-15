/**
 * Live chat state + a single pure reducer for every server event (ARCHITECTURE.md §13).
 *
 * Ordering rules:
 *  - persisted events carry `seq`; anything with seq <= lastSeq is a duplicate and
 *    ignored; seq > lastSeq + 1 means we missed something -> `needsResync`.
 *  - ephemeral frames (turn.delta / turn.snapshot) have no seq; deltas are deduped
 *    by (turn_id, idx), so snapshot/live overlap after a mid-stream join is harmless.
 *  - messages render by `anchor_seq` (AI replies sit where their turn started).
 */
import type { ChatMessage } from "@/lib/api/client";

export type ServerEvent = {
  type: string;
  chat_id?: string;
  seq?: number;
  payload?: Record<string, unknown>;
  replay?: boolean;
  last_seq?: number;
};

export type LiveTurn = {
  id: string;
  startedSeq: number | null;
  messageIds: string[];
  authorIds: string[];
  chunks: Record<number, string>;
  text: string;
};

export type FailedTurn = {
  id: string;
  anchorSeq: number;
  errorCode: string;
  errorMessage: string;
  /** provider's own explanation (redacted server-side), e.g. "model retired" */
  detail: string | null;
  partialText: string | null;
  requeued: boolean;
};

export type PendingSend = {
  clientMsgId: string;
  content: string;
  createdAt: string;
  state: "sending" | "failed";
  error?: string;
};

export type ChatMeta = {
  title?: string;
  model?: string | null;
  status?: string;
  keyStatus?: string;
};

export type LiveState = {
  lastSeq: number;
  messages: Record<string, ChatMessage>;
  turn: LiveTurn | null;
  failed: Record<string, FailedTurn>;
  pendingSends: Record<string, PendingSend>;
  meta: ChatMeta;
  needsResync: boolean;
  deleted: boolean;
  /** bumps when a reply finishes, for the aria-live announcer */
  completedCount: number;
};

export function initialState(
  lastSeq: number,
  messages: ChatMessage[],
  running?: {
    id: string;
    started_seq?: number | null;
    message_ids: string[];
    author_ids: string[];
  } | null,
): LiveState {
  return {
    lastSeq,
    messages: Object.fromEntries(messages.map((m) => [m.id, m])),
    turn: running
      ? {
          id: running.id,
          startedSeq: running.started_seq ?? null,
          messageIds: running.message_ids,
          authorIds: running.author_ids,
          chunks: {},
          text: "",
        }
      : null,
    failed: {},
    pendingSends: {},
    meta: {},
    needsResync: false,
    deleted: false,
    completedCount: 0,
  };
}

function textOf(chunks: Record<number, string>): string {
  return Object.keys(chunks)
    .map(Number)
    .sort((a, b) => a - b)
    .map((i) => chunks[i])
    .join("");
}

function setStatus(
  messages: Record<string, ChatMessage>,
  ids: string[],
  status: string,
  turnId: string | null | undefined,
): Record<string, ChatMessage> {
  const next = { ...messages };
  for (const id of ids) {
    const m = next[id];
    if (m) next[id] = { ...m, status, turn_id: turnId === undefined ? m.turn_id : turnId };
  }
  return next;
}

/** Apply a persisted event (has seq) after the seq checks passed. */
function applyPersisted(s: LiveState, ev: ServerEvent): LiveState {
  const p = (ev.payload ?? {}) as Record<string, unknown>;
  switch (ev.type) {
    case "message.created": {
      const m = p.message as ChatMessage;
      const pendingSends = { ...s.pendingSends };
      if (m.client_msg_id) delete pendingSends[m.client_msg_id];
      return { ...s, messages: { ...s.messages, [m.id]: m }, pendingSends };
    }
    case "turn.started": {
      const ids = p.message_ids as string[];
      // keep chunks if a snapshot for this turn already arrived
      const prev = s.turn && s.turn.id === p.turn_id ? s.turn : null;
      return {
        ...s,
        messages: setStatus(s.messages, ids, "batched", p.turn_id as string),
        turn: {
          id: p.turn_id as string,
          startedSeq: ev.seq ?? null,
          messageIds: ids,
          authorIds: p.author_ids as string[],
          chunks: prev?.chunks ?? {},
          text: prev?.text ?? "",
        },
      };
    }
    case "turn.completed": {
      const ai = p.message as ChatMessage;
      const ids = p.message_ids as string[];
      return {
        ...s,
        messages: { ...setStatus(s.messages, ids, "done", p.turn_id as string), [ai.id]: ai },
        turn: s.turn?.id === p.turn_id ? null : s.turn,
        completedCount: s.completedCount + 1,
      };
    }
    case "turn.failed": {
      const id = p.turn_id as string;
      const ids = (p.message_ids as string[]) ?? [];
      const requeued = Boolean(p.requeued);
      const anchorSeq = s.turn?.id === id && s.turn.startedSeq != null ? s.turn.startedSeq : ev.seq!;
      return {
        ...s,
        messages: requeued ? setStatus(s.messages, ids, "pending", null) : s.messages,
        turn: s.turn?.id === id ? null : s.turn,
        failed: {
          ...s.failed,
          [id]: {
            id,
            anchorSeq,
            errorCode: String(p.error_code ?? "unknown"),
            errorMessage: String(p.error_message ?? "Something went wrong."),
            detail: (p.detail as string | null | undefined) ?? null,
            partialText: (p.partial_text as string | null) ?? null,
            requeued,
          },
        },
      };
    }
    case "chat.updated": {
      const meta = { ...s.meta };
      if ("title" in p) meta.title = p.title as string;
      if ("model" in p) meta.model = p.model as string | null;
      if ("status" in p) meta.status = p.status as string;
      return { ...s, meta };
    }
    case "key.status":
      return {
        ...s,
        meta: {
          ...s.meta,
          keyStatus: p.status as string,
          status: (p.chat_status as string | undefined) ?? s.meta.status,
        },
      };
    default:
      return s; // chat.created and future types: seq still advances
  }
}

export function applyEvent(s: LiveState, ev: ServerEvent): LiveState {
  if (ev.type === "resync_required") return { ...s, needsResync: true };
  if (ev.type === "chat.deleted") return { ...s, deleted: true };

  if (typeof ev.seq === "number") {
    if (ev.seq <= s.lastSeq) return s; // duplicate (replay overlap)
    if (ev.seq > s.lastSeq + 1) return { ...s, needsResync: true }; // gap
    return { ...applyPersisted(s, ev), lastSeq: ev.seq };
  }

  const p = (ev.payload ?? {}) as Record<string, unknown>;
  switch (ev.type) {
    case "turn.snapshot": {
      const id = p.turn_id as string;
      const chunks: Record<number, string> = { ...(s.turn?.id === id ? s.turn.chunks : {}) };
      for (const c of (p.chunks as { idx: number; text: string }[]) ?? []) chunks[c.idx] = c.text;
      const base: LiveTurn = s.turn?.id === id
        ? s.turn
        : { id, startedSeq: null, messageIds: [], authorIds: [], chunks: {}, text: "" };
      return { ...s, turn: { ...base, chunks, text: textOf(chunks) } };
    }
    case "turn.delta": {
      const id = p.turn_id as string;
      if (!s.turn || s.turn.id !== id) return s; // stale or not yet known (snapshot follows)
      const idx = p.idx as number;
      if (s.turn.chunks[idx] !== undefined) return s;
      const chunks = { ...s.turn.chunks, [idx]: p.text as string };
      return { ...s, turn: { ...s.turn, chunks, text: textOf(chunks) } };
    }
    default:
      return s;
  }
}

// ------------------------------------------------------------------ local sends

export function addPendingSend(s: LiveState, send: PendingSend): LiveState {
  return { ...s, pendingSends: { ...s.pendingSends, [send.clientMsgId]: send } };
}

export function failPendingSend(s: LiveState, clientMsgId: string, error: string): LiveState {
  const cur = s.pendingSends[clientMsgId];
  if (!cur) return s;
  return { ...s, pendingSends: { ...s.pendingSends, [clientMsgId]: { ...cur, state: "failed", error } } };
}

export function removePendingSend(s: LiveState, clientMsgId: string): LiveState {
  const pendingSends = { ...s.pendingSends };
  delete pendingSends[clientMsgId];
  return { ...s, pendingSends };
}

/** Server ack for our own send: insert the message even if the event hasn't arrived yet. */
export function ackSend(s: LiveState, m: ChatMessage): LiveState {
  if (s.messages[m.id]) return removePendingSend(s, m.client_msg_id ?? "");
  const pendingSends = { ...s.pendingSends };
  if (m.client_msg_id) delete pendingSends[m.client_msg_id];
  return { ...s, messages: { ...s.messages, [m.id]: m }, pendingSends };
}

// ------------------------------------------------------------------ selectors

export type TimelineItem =
  | { kind: "message"; key: string; order: [number, number, number]; message: ChatMessage }
  | { kind: "streaming"; key: string; order: [number, number, number]; turn: LiveTurn }
  | { kind: "failed"; key: string; order: [number, number, number]; failed: FailedTurn }
  | { kind: "sending"; key: string; order: [number, number, number]; send: PendingSend };

const INF = Number.MAX_SAFE_INTEGER;

export function timeline(s: LiveState): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const m of Object.values(s.messages)) {
    if (m.status === "superseded") continue;
    const rank = m.kind === "ai" ? 1 : 0;
    items.push({ kind: "message", key: m.id, order: [m.anchor_seq, rank, m.seq], message: m });
  }
  if (s.turn) {
    const at = s.turn.startedSeq ?? s.lastSeq;
    items.push({ kind: "streaming", key: `turn-${s.turn.id}`, order: [at, 1, INF], turn: s.turn });
  }
  for (const f of Object.values(s.failed)) {
    items.push({ kind: "failed", key: `failed-${f.id}`, order: [f.anchorSeq, 1, INF], failed: f });
  }
  for (const p of Object.values(s.pendingSends)) {
    items.push({ kind: "sending", key: `send-${p.clientMsgId}`, order: [INF, 0, Date.parse(p.createdAt) || 0], send: p });
  }
  return items.sort(
    (a, b) => a.order[0] - b.order[0] || a.order[1] - b.order[1] || a.order[2] - b.order[2],
  );
}

/** Messages waiting for the next turn (sent while the AI was busy, or requeued). */
export function queuedCount(s: LiveState): number {
  return Object.values(s.messages).filter((m) => m.kind === "human" && m.status === "pending").length;
}
