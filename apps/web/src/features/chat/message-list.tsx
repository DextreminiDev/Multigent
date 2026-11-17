"use client";

import { AlertTriangle, ArrowDown, Clock, RotateCw, Sparkles, X } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Avatar } from "@/components/avatar";
import { CopyButton, Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import type { ChatMessage } from "@/lib/api/client";
import type { FailedTurn, LiveState, LiveTurn, PendingSend } from "@/lib/ws/reducer";
import { timeline } from "@/lib/ws/reducer";
import { cn } from "@/lib/utils";

function time(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function AiFrame({
  children,
  replyingTo,
  footer,
  label,
}: {
  children: React.ReactNode;
  replyingTo: string[];
  footer?: React.ReactNode;
  label: string;
}) {
  return (
    <article aria-label={label} className="group/ai flex gap-3 py-3">
      <span
        aria-hidden
        className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-full bg-accent text-accent-foreground"
      >
        <Sparkles className="size-3.5" />
      </span>
      <div className="min-w-0 flex-1">
        {replyingTo.length > 0 && (
          <div className="mb-1.5 flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
            <span>Replying to</span>
            {replyingTo.map((n) => (
              <span key={n} className="rounded-full bg-muted px-2 py-0.5 text-foreground/80">
                {n}
              </span>
            ))}
          </div>
        )}
        {children}
        {footer}
      </div>
    </article>
  );
}

function HumanMessage({
  m,
  mine,
  queued,
  former,
}: {
  m: ChatMessage;
  mine: boolean;
  queued: "next" | "waiting" | null;
  former: boolean;
}) {
  return (
    <article
      aria-label={`${m.author_name ?? "Someone"} said`}
      className={cn("flex gap-3 py-2", mine && "flex-row-reverse")}
    >
      <Avatar name={m.author_name ?? "?"} color={m.author_color ?? "#777"} className="mt-0.5 size-7" />
      <div className={cn("flex min-w-0 max-w-[85%] flex-col", mine ? "items-end" : "items-start")}>
        <div className="mb-0.5 flex items-baseline gap-2 text-xs text-muted-foreground">
          <span className="font-medium text-foreground/90">{mine ? "You" : m.author_name}</span>
          <time dateTime={m.created_at}>{time(m.created_at)}</time>
          {former && <span>(former member)</span>}
        </div>
        <div
          className={cn(
            "whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-[15px] leading-relaxed",
            mine ? "rounded-tr-sm bg-primary/15 text-foreground" : "rounded-tl-sm bg-muted",
          )}
        >
          {m.status === "deleted" ? <em className="text-muted-foreground">Message deleted</em> : m.content}
        </div>
        {queued && (
          <span className="mt-1 inline-flex items-center gap-1 text-xs text-muted-foreground">
            <Clock className="size-3" aria-hidden />
            {queued === "next" ? "Queued · will be answered next" : "Waiting to be answered"}
          </span>
        )}
      </div>
    </article>
  );
}

function SendingMessage({
  p,
  onRetry,
  onDiscard,
}: {
  p: PendingSend;
  onRetry: () => void;
  onDiscard: () => void;
}) {
  return (
    <article className="flex flex-row-reverse gap-3 py-2 opacity-80">
      <span className="size-7 shrink-0" aria-hidden />
      <div className="flex max-w-[85%] flex-col items-end">
        <div className="whitespace-pre-wrap break-words rounded-2xl rounded-tr-sm bg-primary/10 px-3.5 py-2 text-[15px]">
          {p.content}
        </div>
        {p.state === "sending" ? (
          <span className="mt-1 text-xs text-muted-foreground">Sending…</span>
        ) : (
          <span role="alert" className="mt-1 flex items-center gap-2 text-xs text-destructive">
            {p.error}
            <button type="button" className="underline" onClick={onRetry}>
              Retry
            </button>
            <button type="button" aria-label="Discard message" onClick={onDiscard}>
              <X className="size-3.5" />
            </button>
          </span>
        )}
      </div>
    </article>
  );
}

function StreamingMessage({ turn, names }: { turn: LiveTurn; names: string[] }) {
  return (
    <AiFrame replyingTo={names} label="Assistant is replying">
      {turn.text ? (
        <div>
          <Markdown text={turn.text} streaming />
          <span className="streaming-caret" aria-hidden />
        </div>
      ) : (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <span className="inline-flex gap-1" aria-hidden>
            <span className="size-1.5 animate-bounce rounded-full bg-primary [animation-delay:-0.2s]" />
            <span className="size-1.5 animate-bounce rounded-full bg-primary [animation-delay:-0.1s]" />
            <span className="size-1.5 animate-bounce rounded-full bg-primary" />
          </span>
          Thinking…
        </p>
      )}
    </AiFrame>
  );
}

function FailedMessage({ f, names }: { f: FailedTurn; names: string[] }) {
  return (
    <AiFrame replyingTo={names} label="Assistant reply failed">
      {f.partialText && <Markdown text={f.partialText} className="opacity-70" />}
      <p className="mt-2 flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 p-2.5 text-sm text-destructive">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span>
          {f.errorMessage}
          {f.detail && (
            <span className="mt-1 block text-xs text-destructive/80">Provider said: {f.detail}</span>
          )}
          {f.requeued && (
            <span className="mt-0.5 flex items-center gap-1 text-xs text-muted-foreground">
              <RotateCw className="size-3" /> The messages will be answered once this is resolved.
            </span>
          )}
        </span>
      </p>
    </AiFrame>
  );
}

export function MessageList({
  state,
  meId,
  canSend,
  paused,
  onRetrySend,
  onDiscardSend,
  memberIds = null,
  header,
  empty,
}: {
  state: LiveState;
  meId: string;
  canSend: boolean;
  paused: boolean;
  onRetrySend: (id: string) => void;
  onDiscardSend: (id: string) => void;
  /** current member ids (null while loading): others are "former members" */
  memberIds?: Set<string> | null;
  header?: React.ReactNode;
  empty: React.ReactNode;
}) {
  const items = useMemo(() => timeline(state), [state]);
  const scroller = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(true);
  const [announce, setAnnounce] = useState("");

  // authors of each turn's batch, for "Replying to" chips
  const namesByTurn = useMemo(() => {
    const out: Record<string, string[]> = {};
    for (const m of Object.values(state.messages)) {
      if (m.kind !== "human" || !m.turn_id || !m.author_name) continue;
      const list = (out[m.turn_id] ??= []);
      if (!list.includes(m.author_name)) list.push(m.author_name);
    }
    return out;
  }, [state.messages]);

  const streamingNames = useMemo(() => {
    if (!state.turn) return [];
    return [
      ...new Set(
        state.turn.messageIds
          .map((id) => state.messages[id]?.author_name)
          .filter((n): n is string => Boolean(n)),
      ),
    ];
  }, [state.turn, state.messages]);

  // stick to bottom while the user is at the bottom
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stuck) el.scrollTop = el.scrollHeight;
  }, [items, state.turn?.text, stuck]);

  useEffect(() => {
    if (state.completedCount > 0) setAnnounce(`Assistant replied (${state.completedCount})`);
  }, [state.completedCount]);

  const turnRunning = Boolean(state.turn);

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          setStuck(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
        }}
        className="h-full overflow-y-auto"
        role="log"
        aria-label="Messages"
        aria-live="off"
      >
        <div className="mx-auto w-full max-w-3xl px-4 py-4">
          {header}
          {items.length === 0 && empty}
          {items.map((it) => {
            switch (it.kind) {
              case "message": {
                const m = it.message;
                if (m.kind === "ai") {
                  return (
                    <AiFrame
                      key={it.key}
                      label="Assistant said"
                      replyingTo={m.turn_id ? (namesByTurn[m.turn_id] ?? []) : []}
                      footer={
                        <div className="mt-1 opacity-0 transition-opacity group-hover/ai:opacity-100 focus-within:opacity-100">
                          <CopyButton text={m.content} />
                        </div>
                      }
                    >
                      <Markdown text={m.content} />
                    </AiFrame>
                  );
                }
                if (m.kind === "system_event") {
                  return (
                    <p key={it.key} className="py-2 text-center text-xs text-muted-foreground">
                      {m.content}
                    </p>
                  );
                }
                const queued =
                  m.status === "pending" ? (turnRunning && !paused ? "next" : "waiting") : null;
                const former =
                  !m.author_id || (memberIds !== null && !memberIds.has(m.author_id));
                return (
                  <HumanMessage
                    key={it.key}
                    m={m}
                    mine={m.author_id === meId}
                    queued={queued}
                    former={former}
                  />
                );
              }
              case "streaming":
                return <StreamingMessage key={it.key} turn={it.turn} names={streamingNames} />;
              case "failed":
                return (
                  <FailedMessage
                    key={it.key}
                    f={it.failed}
                    names={namesByTurn[it.failed.id] ?? []}
                  />
                );
              case "sending":
                return canSend || it.send.state === "failed" ? (
                  <SendingMessage
                    key={it.key}
                    p={it.send}
                    onRetry={() => onRetrySend(it.send.clientMsgId)}
                    onDiscard={() => onDiscardSend(it.send.clientMsgId)}
                  />
                ) : null;
            }
          })}
        </div>
      </div>
      {!stuck && (
        <Button
          size="sm"
          variant="secondary"
          className="absolute bottom-3 left-1/2 -translate-x-1/2 shadow"
          onClick={() => {
            const el = scroller.current;
            if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
            setStuck(true);
          }}
        >
          <ArrowDown /> Latest
        </Button>
      )}
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {announce}
      </div>
    </div>
  );
}
