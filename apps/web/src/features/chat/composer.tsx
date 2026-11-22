"use client";

import { ArrowUp, EyeOff, MessageSquareOff, Sparkles } from "lucide-react";
import { useId, useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { INTENT_LABEL, parseIntent, tagQueryAt, TAGS, type Intent, type ReplyMode } from "@/lib/tags";
import { cn } from "@/lib/utils";

export const MAX_MESSAGE_CHARS = 8000;

const INTENT_ICON: Record<Intent, React.ComponentType<{ className?: string }>> = {
  reply: Sparkles,
  context: MessageSquareOff,
  off: EyeOff,
};

export function placeholderFor(mode: ReplyMode): string {
  return mode === "tags" ? "Message everyone… add @ai to ask the AI" : "Message everyone…";
}

export function defaultHint(mode: ReplyMode): string {
  return mode === "tags"
    ? "Only @ai messages get a reply · @off hides a message from the AI"
    : "@skip: no reply · @off: hidden from the AI · Shift+Enter for a new line";
}

/** Shows what sending will do: the AI replies, sees it as context, or never sees it. */
export function IntentPreview({ intent }: { intent: Intent }) {
  const Icon = INTENT_ICON[intent];
  return (
    <span
      data-testid="intent-preview"
      data-intent={intent}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
        intent === "reply" && "bg-primary/10 text-primary",
        intent === "context" && "bg-muted text-muted-foreground",
        intent === "off" && "bg-amber-500/15 text-amber-700 dark:text-amber-300",
      )}
    >
      <Icon className="size-3" aria-hidden />
      {INTENT_LABEL[intent]}
    </span>
  );
}

/** Enter sends, Shift+Enter adds a newline. Typing "@" suggests @ai / @skip / @off.
 *  `disabledReason` explains why the user can't send (viewer...), shown instead. */
export function Composer({
  onSend,
  onType,
  disabledReason,
  hint,
  replyMode = "always",
}: {
  onSend: (text: string) => void;
  onType?: () => void;
  disabledReason?: string | null;
  hint?: string | null;
  replyMode?: ReplyMode;
}) {
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState<number | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);
  const listId = useId();

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [text]);

  if (disabledReason) {
    return (
      <div className="rounded-2xl border border-dashed px-4 py-3 text-center text-sm text-muted-foreground">
        {disabledReason}
      </div>
    );
  }

  const tooLong = text.length > MAX_MESSAGE_CHARS;
  const intent = parseIntent(text, replyMode);
  const q = tagQueryAt(text, caret);
  const options = q && dismissed !== q.start ? TAGS.filter((t) => t.tag.startsWith(q.query)) : [];
  const open = options.length > 0;
  const activeIdx = Math.min(active, options.length - 1);

  const submit = () => {
    const t = text.trim();
    if (!t || tooLong) return;
    onSend(t);
    setText("");
    setCaret(0);
    ref.current?.focus();
  };

  const pick = (tag: string) => {
    if (!q) return;
    const after = text.slice(caret);
    const insert = `@${tag}${after.startsWith(" ") ? "" : " "}`;
    const next = text.slice(0, q.start) + insert + after;
    const pos = q.start + insert.length;
    setText(next);
    setCaret(pos);
    setActive(0);
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.setSelectionRange(pos, pos);
    });
  };

  const syncCaret = (el: HTMLTextAreaElement) => setCaret(el.selectionStart ?? el.value.length);

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className="relative rounded-2xl border bg-card p-2 shadow-sm focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/30"
    >
      {open && (
        <ul
          id={listId}
          role="listbox"
          aria-label="Message tags"
          className="absolute bottom-full left-2 z-10 mb-2 w-72 overflow-hidden rounded-xl border bg-popover p-1 text-popover-foreground shadow-md"
        >
          {options.map((o, i) => (
            <li
              key={o.tag}
              id={`${listId}-${o.tag}`}
              role="option"
              aria-selected={i === activeIdx}
              onMouseDown={(e) => {
                e.preventDefault(); // keep focus in the textarea
                pick(o.tag);
              }}
              onMouseEnter={() => setActive(i)}
              className={cn(
                "flex cursor-pointer items-baseline gap-2 rounded-lg px-2.5 py-1.5 text-sm",
                i === activeIdx && "bg-accent text-accent-foreground",
              )}
            >
              <span className="font-mono font-medium">{o.label}</span>
              <span className="text-xs text-muted-foreground">{o.help}</span>
            </li>
          ))}
        </ul>
      )}
      <label htmlFor="composer" className="sr-only">
        Message
      </label>
      <textarea
        id="composer"
        ref={ref}
        rows={1}
        value={text}
        placeholder={placeholderFor(replyMode)}
        aria-describedby={`${listId}-preview`}
        onChange={(e) => {
          setText(e.target.value);
          syncCaret(e.target);
          setActive(0);
          setDismissed(null);
          if (e.target.value.trim()) onType?.();
        }}
        onSelect={(e) => syncCaret(e.currentTarget)}
        onCompositionStart={() => (composing.current = true)}
        onCompositionEnd={() => (composing.current = false)}
        onKeyDown={(e) => {
          if (open) {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              const d = e.key === "ArrowDown" ? 1 : -1;
              setActive((activeIdx + d + options.length) % options.length);
              return;
            }
            if ((e.key === "Enter" || e.key === "Tab") && !e.shiftKey) {
              e.preventDefault();
              pick(options[activeIdx].tag);
              return;
            }
            if (e.key === "Escape") {
              e.preventDefault();
              setDismissed(q?.start ?? null);
              return;
            }
          }
          if (e.key === "Enter" && !e.shiftKey && !composing.current && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
        className="block max-h-60 w-full resize-none bg-transparent px-2 py-1.5 text-[15px] outline-none placeholder:text-muted-foreground"
      />
      <div className="flex items-center justify-between gap-2 px-1 pt-1">
        <span id={`${listId}-preview`} className="flex min-w-0 items-center gap-2">
          {text.trim() && !tooLong && <IntentPreview intent={intent} />}
          <span
            className={cn("truncate text-xs text-muted-foreground", tooLong && "text-destructive")}
          >
            {tooLong
              ? `${text.length.toLocaleString()} / ${MAX_MESSAGE_CHARS.toLocaleString()} characters`
              : text.trim()
                ? null
                : (hint ?? defaultHint(replyMode))}
          </span>
        </span>
        <Button
          type="submit"
          size="icon"
          className="shrink-0 rounded-full"
          aria-label="Send message"
          disabled={!text.trim() || tooLong}
        >
          <ArrowUp />
        </Button>
      </div>
    </form>
  );
}
