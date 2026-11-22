"use client";

import { ArrowUp, EyeOff, MessageSquare, Sparkles, Users } from "lucide-react";
import { useId, useLayoutEffect, useRef, useState } from "react";
import { Avatar } from "@/components/avatar";
import { Button } from "@/components/ui/button";
import {
  applyEdit,
  insertMention,
  INTENT_LABEL,
  intentOf,
  mentionOptions,
  mentionQueryAt,
  offWithAi,
  removeChipBefore,
  segments,
  type Intent,
  type Mention,
  type Person,
} from "@/lib/mentions";
import { cn } from "@/lib/utils";

export const MAX_MESSAGE_CHARS = 8000;
export const OFF_WITH_AI = "The AI can't be mentioned in an off-the-record message.";
const PLACEHOLDER = "Message everyone… mention @AI for a reply";
const HINT = "@ to mention · Shift+Enter for a new line";

const INTENT_ICON: Record<Intent, React.ComponentType<{ className?: string }>> = {
  reply: Sparkles,
  context: MessageSquare,
  off: EyeOff,
};

/** Shows what sending will do: the AI replies, sees it as context, or never sees it. */
export function IntentPreview({ intent }: { intent: Intent }) {
  const Icon = INTENT_ICON[intent];
  return (
    <span
      data-testid="intent-preview"
      data-intent={intent}
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
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

// Shared by the textarea and the highlight backdrop behind it: both must lay text
// out identically so the chip highlights line up with the typed characters.
const TEXT = "px-2 py-1.5 text-[15px] leading-6 whitespace-pre-wrap break-words";

/** Enter sends, Shift+Enter adds a newline. "@" opens the mention menu (AI,
 *  everyone, people); picked mentions are chips tracked as ranges over the text.
 *  The eye button sends the message off the record (hidden from the AI). */
export function Composer({
  onSend,
  onType,
  disabledReason,
  people = [],
  meId = "",
}: {
  onSend: (text: string, mentions: Mention[], off: boolean) => void;
  onType?: () => void;
  disabledReason?: string | null;
  people?: Person[];
  meId?: string;
}) {
  const [text, setText] = useState("");
  const [mentions, setMentions] = useState<Mention[]>([]);
  const [off, setOff] = useState(false);
  const [caret, setCaret] = useState(0);
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState<number | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const composing = useRef(false);
  const listId = useId();

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (!text) {
      // one row; don't measure (Chrome counts a wrapped placeholder in scrollHeight)
      el.style.height = "";
      return;
    }
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
  const conflict = offWithAi(mentions, off);
  const intent = intentOf(mentions, off);
  const q = mentionQueryAt(text, caret);
  const insideChip = q && mentions.some((m) => m.offset === q.start);
  const options = q && !insideChip && dismissed !== q.start ? mentionOptions(q.query, people, meId) : [];
  const open = options.length > 0;
  const activeIdx = Math.min(active, options.length - 1);
  const canSend = Boolean(text.trim()) && !tooLong && !conflict;

  const setAll = (next: { text: string; mentions: Mention[]; caret: number }) => {
    setText(next.text);
    setMentions(next.mentions);
    setCaret(next.caret);
    setActive(0);
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.setSelectionRange(next.caret, next.caret);
    });
  };

  const submit = () => {
    if (!canSend) return;
    // trim like the server does, keeping mention offsets in step
    const lead = text.length - text.trimStart().length;
    const body = text.trim();
    const kept = mentions
      .map((m) => ({ ...m, offset: m.offset - lead }))
      .filter((m) => m.offset >= 0 && m.offset + m.length <= body.length);
    onSend(body, kept, off);
    setText("");
    setMentions([]);
    setOff(false);
    setCaret(0);
    ref.current?.focus();
  };

  const pick = (i: number) => {
    if (!q) return;
    setAll(insertMention(text, mentions, q.start, caret, options[i]));
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
          aria-label="Mention someone"
          className="absolute bottom-full left-2 z-10 mb-2 max-h-64 w-72 overflow-y-auto rounded-xl border bg-popover p-1 text-popover-foreground shadow-md"
        >
          {options.map((o, i) => (
            <li
              key={o.type === "user" ? o.person.id : o.type}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === activeIdx}
              onMouseDown={(e) => {
                e.preventDefault(); // keep focus in the textarea
                pick(i);
              }}
              onMouseEnter={() => setActive(i)}
              className={cn(
                "flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm",
                i === activeIdx && "bg-accent text-accent-foreground",
              )}
            >
              {o.type === "ai" ? (
                <span className="grid size-5 place-items-center rounded-full bg-primary/15 text-primary">
                  <Sparkles className="size-3" aria-hidden />
                </span>
              ) : o.type === "everyone" ? (
                <span className="grid size-5 place-items-center rounded-full bg-muted">
                  <Users className="size-3" aria-hidden />
                </span>
              ) : (
                <Avatar name={o.person.name} color={o.person.color ?? "#777"} size="sm" className="size-5" />
              )}
              <span className="font-medium">{o.label}</span>
              {o.help && <span className="truncate text-xs text-muted-foreground">{o.help}</span>}
            </li>
          ))}
        </ul>
      )}
      <label htmlFor="composer" className="sr-only">
        Message
      </label>
      <div className="relative">
        <div ref={backdrop} aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
          <div className={cn(TEXT, "text-transparent")}>
            {segments(text, mentions).map((s, i) =>
              s.mention ? (
                <mark
                  key={i}
                  data-chip={s.mention.type}
                  className={cn(
                    "rounded-[3px] text-transparent",
                    s.mention.type === "ai"
                      ? conflict
                        ? "bg-destructive/25"
                        : "bg-primary/25"
                      : "bg-foreground/12",
                  )}
                >
                  {s.text}
                </mark>
              ) : (
                <span key={i}>{s.text}</span>
              ),
            )}
            {"\n "}
          </div>
        </div>
        <textarea
          id="composer"
          ref={ref}
          rows={1}
          value={text}
          placeholder={PLACEHOLDER}
          aria-describedby={`${listId}-status`}
          onScroll={(e) => {
            if (backdrop.current) backdrop.current.scrollTop = e.currentTarget.scrollTop;
          }}
          onChange={(e) => {
            const next = e.target.value;
            setMentions((ms) => applyEdit(text, next, ms));
            setText(next);
            syncCaret(e.target);
            setActive(0);
            setDismissed(null);
            if (next.trim()) onType?.();
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
                pick(activeIdx);
                return;
              }
              if (e.key === "Escape") {
                e.preventDefault();
                setDismissed(q?.start ?? null);
                return;
              }
            }
            const el = e.currentTarget;
            if (e.key === "Backspace" && el.selectionStart === el.selectionEnd) {
              const removed = removeChipBefore(text, mentions, el.selectionStart);
              if (removed) {
                e.preventDefault();
                setAll(removed);
                return;
              }
            }
            if (e.key === "Enter" && !e.shiftKey && !composing.current && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          className={cn(
            TEXT,
            "relative block max-h-60 w-full resize-none bg-transparent outline-none placeholder:text-muted-foreground",
          )}
        />
      </div>
      <div className="flex items-center justify-between gap-2 px-1 pt-1">
        <span id={`${listId}-status`} className="flex min-w-0 items-center gap-2" aria-live="polite">
          {text.trim() && !tooLong && !conflict && <IntentPreview intent={intent} />}
          <span
            className={cn(
              "text-xs text-muted-foreground",
              conflict ? "leading-snug" : "truncate", // the warning must be readable in full
              (tooLong || conflict) && "text-destructive",
            )}
          >
            {conflict
              ? OFF_WITH_AI
              : tooLong
                ? `${text.length.toLocaleString()} / ${MAX_MESSAGE_CHARS.toLocaleString()} characters`
                : text.trim()
                  ? null
                  : HINT}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-1">
          <Button
            type="button"
            size="icon"
            variant="ghost"
            aria-label="Off the record"
            aria-pressed={off}
            title={off ? "Off the record: hidden from the AI" : "Send off the record (hidden from the AI)"}
            onClick={() => {
              setOff((v) => !v);
              ref.current?.focus();
            }}
            className={cn(
              "rounded-full text-muted-foreground",
              off && "bg-amber-500/15 text-amber-700 hover:bg-amber-500/20 dark:text-amber-300",
            )}
          >
            <EyeOff />
          </Button>
          <Button
            type="submit"
            size="icon"
            className="rounded-full"
            aria-label="Send message"
            disabled={!canSend}
          >
            <ArrowUp />
          </Button>
        </span>
      </div>
    </form>
  );
}
