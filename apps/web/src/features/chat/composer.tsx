"use client";

import { ArrowUp } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export const MAX_MESSAGE_CHARS = 8000;

/** Enter sends, Shift+Enter adds a newline. `disabledReason` explains why the
 *  user can't send (viewer, no key, paused...), shown in place of the input. */
export function Composer({
  onSend,
  onType,
  disabledReason,
  hint,
}: {
  onSend: (text: string) => void;
  onType?: () => void;
  disabledReason?: string | null;
  hint?: string | null;
}) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);

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
  const submit = () => {
    const t = text.trim();
    if (!t || tooLong) return;
    onSend(t);
    setText("");
    ref.current?.focus();
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className="rounded-2xl border bg-card p-2 shadow-sm focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/30"
    >
      <label htmlFor="composer" className="sr-only">
        Message
      </label>
      <textarea
        id="composer"
        ref={ref}
        rows={1}
        value={text}
        placeholder="Message everyone…"
        onChange={(e) => {
          setText(e.target.value);
          if (e.target.value.trim()) onType?.();
        }}
        onCompositionStart={() => (composing.current = true)}
        onCompositionEnd={() => (composing.current = false)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !composing.current && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
        className="block max-h-60 w-full resize-none bg-transparent px-2 py-1.5 text-[15px] outline-none placeholder:text-muted-foreground"
      />
      <div className="flex items-center justify-between gap-2 px-1 pt-1">
        <span className={cn("text-xs text-muted-foreground", tooLong && "text-destructive")}>
          {tooLong
            ? `${text.length.toLocaleString()} / ${MAX_MESSAGE_CHARS.toLocaleString()} characters`
            : (hint ?? "Enter to send · Shift+Enter for a new line")}
        </span>
        <Button
          type="submit"
          size="icon"
          className="rounded-full"
          aria-label="Send message"
          disabled={!text.trim() || tooLong}
        >
          <ArrowUp />
        </Button>
      </div>
    </form>
  );
}
