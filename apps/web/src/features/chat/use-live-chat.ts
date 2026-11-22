"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, unwrap } from "@/lib/api/client";
import { qk, qkm, qku } from "@/lib/api/hooks";
import { API_URL } from "@/lib/config";
import { getWsClient, WsRequestError, type ConnStatus } from "@/lib/ws/client";
import {
  ackSend,
  addPendingSend,
  applyEvent,
  failPendingSend,
  initialState,
  removePendingSend,
  type LiveState,
  type ServerEvent,
} from "@/lib/ws/reducer";
import type { ChatMessage } from "@/lib/api/client";

function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  // RFC 4122 v4 fallback
  return "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
    (Number(c) ^ (Math.random() * 16) >> (Number(c) / 4)).toString(16),
  );
}

export type LiveChat = {
  state: LiveState | null;
  error: string | null;
  connection: ConnStatus;
  send: (content: string) => void;
  retrySend: (clientMsgId: string) => void;
  discardSend: (clientMsgId: string) => void;
  /** call on composer input; throttled to one frame per 2 s */
  notifyTyping: () => void;
};

const TYPING_THROTTLE_MS = 2000;

/**
 * Live state for one chat: load a consistent snapshot (`GET /chats/{id}/state`),
 * subscribe from its `last_seq`, apply every event through the pure reducer, and
 * reload + resubscribe whenever the reducer detects a gap.
 */
export function useLiveChat(chatId: string): LiveChat {
  const qc = useQueryClient();
  const client = getWsClient(API_URL);
  const ref = useRef<LiveState | null>(null);
  const [state, setState] = useState<LiveState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnStatus>(client.getStatus());
  const reloading = useRef(false);

  const commit = useCallback((next: LiveState) => {
    ref.current = next;
    setState(next);
  }, []);

  const load = useCallback(async () => {
    const st = await unwrap(api.GET("/chats/{chat_id}/state", { params: { path: { chat_id: chatId } } }));
    const prev = ref.current;
    let next = initialState(st.last_seq, st.messages, st.running_turn);
    if (prev) next = { ...next, pendingSends: prev.pendingSends, meta: prev.meta };
    commit(next);
  }, [chatId, commit]);

  const onEvent = useCallback(
    (ev: ServerEvent) => {
      const cur = ref.current;
      if (!cur) return; // events before the snapshot loaded are covered by the replay
      const next = applyEvent(cur, ev);
      if (next === cur) return;
      commit(next);
      if (ev.type === "chat.updated" || ev.type === "key.status" || ev.type === "member.updated") {
        qc.invalidateQueries({ queryKey: qk.chat(chatId) });
        qc.invalidateQueries({ queryKey: qk.chats, exact: true });
      }
      if (ev.type === "member.updated") {
        qc.invalidateQueries({ queryKey: qkm.members(chatId) });
      }
      if (ev.type === "context.update") {
        qc.invalidateQueries({ queryKey: qku.context(chatId) }); // summary + queued flag
      }
      if (ev.type === "turn.completed" || ev.type === "turn.cancelled") {
        qc.invalidateQueries({ queryKey: qku.usage(chatId) });
      }
    },
    [chatId, commit, qc],
  );

  useEffect(() => client.onStatus(setConnection), [client]);

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: (() => void) | null = null;
    ref.current = null;
    setState(null);
    setError(null);
    load()
      .then(() => {
        if (cancelled) return;
        unsubscribe = client.subscribe(chatId, () => ref.current?.lastSeq ?? 0, onEvent);
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
      unsubscribe?.();
    };
  }, [chatId, client, load, onEvent]);

  // gap or resync_required -> reload the snapshot and replay from its last_seq
  useEffect(() => {
    if (!state?.needsResync || reloading.current) return;
    reloading.current = true;
    load()
      .then(() => client.resubscribe(chatId))
      .catch(() => {})
      .finally(() => {
        reloading.current = false;
      });
  }, [state?.needsResync, load, client, chatId]);

  const transmit = useCallback(
    (clientMsgId: string, content: string) => {
      client
        .sendMessage(chatId, clientMsgId, content)
        .then((ack) => {
          if (ref.current) commit(ackSend(ref.current, ack.message as ChatMessage));
        })
        .catch((e: unknown) => {
          const msg =
            e instanceof WsRequestError ? e.message : "Couldn't send. Check your connection.";
          if (ref.current) commit(failPendingSend(ref.current, clientMsgId, msg));
        });
    },
    [chatId, client, commit],
  );

  const send = useCallback(
    (content: string) => {
      if (!ref.current) return;
      const clientMsgId = newId();
      lastTyping.current = 0;
      commit(
        addPendingSend(ref.current, {
          clientMsgId,
          content,
          createdAt: new Date().toISOString(),
          state: "sending",
        }),
      );
      transmit(clientMsgId, content);
    },
    [commit, transmit],
  );

  const retrySend = useCallback(
    (clientMsgId: string) => {
      const p = ref.current?.pendingSends[clientMsgId];
      if (!p || !ref.current) return;
      commit(addPendingSend(ref.current, { ...p, state: "sending", error: undefined }));
      transmit(clientMsgId, p.content); // same id: the server dedupes
    },
    [commit, transmit],
  );

  const discardSend = useCallback(
    (clientMsgId: string) => {
      if (ref.current) commit(removePendingSend(ref.current, clientMsgId));
    },
    [commit],
  );

  const lastTyping = useRef(0);
  const notifyTyping = useCallback(() => {
    const now = Date.now();
    if (now - lastTyping.current < TYPING_THROTTLE_MS) return;
    lastTyping.current = now;
    client.typing(chatId);
  }, [chatId, client]);

  return { state, error, connection, send, retrySend, discardSend, notifyTyping };
}
