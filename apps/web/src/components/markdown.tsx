"use client";

import { Check, Copy } from "lucide-react";
import { memo, useEffect, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

/**
 * Model output is untrusted. react-markdown never renders raw HTML (no rehype-raw),
 * rehype-sanitize strips dangerous URLs/attributes, links open in a new tab with
 * noopener/noreferrer, and images are shown as links instead of being loaded.
 */

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        });
      }}
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
      aria-label={done ? "Copied" : label}
    >
      {done ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      <span className="sr-only sm:not-sr-only">{done ? "Copied" : label}</span>
    </button>
  );
}

type Highlighter = (code: string, lang: string) => Promise<string>;
let highlighterPromise: Promise<Highlighter> | null = null;

function getHighlighter(): Promise<Highlighter> {
  highlighterPromise ??= import("shiki").then(({ codeToHtml }) => (code, lang) =>
    codeToHtml(code, {
      lang,
      themes: { light: "github-light-default", dark: "github-dark-default" },
      defaultColor: false,
    }).catch(() =>
      codeToHtml(code, {
        lang: "text",
        themes: { light: "github-light-default", dark: "github-dark-default" },
        defaultColor: false,
      }),
    ),
  );
  return highlighterPromise;
}

function CodeBlock({ code, lang, highlight }: { code: string; lang: string; highlight: boolean }) {
  const [html, setHtml] = useState<string | null>(null);
  useEffect(() => {
    if (!highlight) return;
    let alive = true;
    // shiki tokenizes plain text and escapes it; the output contains no user HTML.
    getHighlighter()
      .then((h) => h(code, lang || "text"))
      .then((out) => alive && setHtml(out))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [code, lang, highlight]);

  return (
    <div className="group/code relative my-3 overflow-hidden rounded-lg border bg-muted/40">
      <div className="flex items-center justify-between border-b px-3 py-1 text-xs text-muted-foreground">
        <span className="font-mono">{lang || "text"}</span>
        <CopyButton text={code} label="Copy code" />
      </div>
      {html ? (
        <div className="shiki-wrap overflow-x-auto text-[13px]" dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <pre className="overflow-x-auto p-3 font-mono text-[13px] leading-relaxed">
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
}

function makeComponents(highlight: boolean): Components {
  return {
    a: ({ href, children }) => (
      <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-primary underline underline-offset-2">
        {children}
      </a>
    ),
    img: ({ src, alt }) => (
      <a href={typeof src === "string" ? src : undefined} target="_blank" rel="noopener noreferrer nofollow" className="text-primary underline">
        [image{alt ? `: ${alt}` : ""}]
      </a>
    ),
    pre: ({ children }) => <>{children}</>,
    code: ({ className, children }) => {
      const text = String(children ?? "");
      const lang = /language-([\w+#-]+)/.exec(className ?? "")?.[1] ?? "";
      const block = Boolean(lang) || text.includes("\n");
      if (!block) {
        return <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">{children}</code>;
      }
      return <CodeBlock code={text.replace(/\n$/, "")} lang={lang} highlight={highlight} />;
    },
    table: ({ children }) => (
      <div className="my-3 overflow-x-auto">
        <table className="w-full border-collapse text-sm">{children}</table>
      </div>
    ),
    th: ({ children }) => <th className="border px-2 py-1 text-left font-medium">{children}</th>,
    td: ({ children }) => <td className="border px-2 py-1">{children}</td>,
  };
}

const finalComponents = makeComponents(true);
const streamingComponents = makeComponents(false);

/** `streaming`: skip syntax highlighting until the message is complete (cheap re-renders). */
export const Markdown = memo(function Markdown({
  text,
  streaming = false,
  className,
}: {
  text: string;
  streaming?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("prose-chat", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        components={streaming ? streamingComponents : finalComponents}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
