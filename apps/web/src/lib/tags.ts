/** Message tags (mirrors apps/api/app/messages/tags.py; the server decides).
 *
 *  @ai   → the AI replies (and sees it)
 *  @skip → no reply, but the AI sees it as context
 *  @off  → no reply, hidden from the AI (humans only)
 *  none  → "always" mode: reply · "tags" mode: context only
 *
 *  The most restrictive tag wins: @off > @skip > @ai. */

export type ReplyMode = "always" | "tags";
export type Intent = "reply" | "context" | "off";
export type TagName = "ai" | "skip" | "off";

// not after a word char, "@" or "." (so bob@ai.com isn't a tag); whole words only
// (no lookbehind: the build targets ES2017, so the boundary is captured in group 1)
const TAG_RE = /(^|[^\w@.])@(ai|skip|off)\b/gi;

function* matches(text: string): Generator<{ index: number; raw: string; tag: TagName }> {
  for (const m of text.matchAll(TAG_RE)) {
    const index = (m.index ?? 0) + m[1].length;
    yield { index, raw: m[0].slice(m[1].length), tag: m[2].toLowerCase() as TagName };
  }
}

export function tagsIn(text: string): Set<TagName> {
  const out = new Set<TagName>();
  for (const m of matches(text)) out.add(m.tag);
  return out;
}

export function parseIntent(text: string, mode: ReplyMode): Intent {
  const tags = tagsIn(text);
  if (tags.has("off")) return "off";
  if (tags.has("skip")) return "context";
  if (tags.has("ai")) return "reply";
  return mode === "always" ? "reply" : "context";
}

/** Split text into plain and tag segments, for highlighting tags in messages. */
export function splitTags(text: string): { text: string; tag: TagName | null }[] {
  const out: { text: string; tag: TagName | null }[] = [];
  let last = 0;
  for (const m of matches(text)) {
    if (m.index > last) out.push({ text: text.slice(last, m.index), tag: null });
    out.push({ text: m.raw, tag: m.tag });
    last = m.index + m.raw.length;
  }
  if (last < text.length) out.push({ text: text.slice(last), tag: null });
  return out;
}

export const TAGS: { tag: TagName; label: string; help: string }[] = [
  { tag: "ai", label: "@ai", help: "Ask the AI to reply" },
  { tag: "skip", label: "@skip", help: "No reply, but the AI sees it" },
  { tag: "off", label: "@off", help: "No reply, hidden from the AI" },
];

export const INTENT_LABEL: Record<Intent, string> = {
  reply: "AI will reply",
  context: "No reply · AI sees it",
  off: "Off the record · hidden from AI",
};

/** The "@word" being typed at the caret, if it could still become a tag. */
export function tagQueryAt(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const m = /(^|[^\w@.])@(\w*)$/.exec(before);
  if (!m) return null;
  const query = m[2].toLowerCase();
  if (!TAGS.some((t) => t.tag.startsWith(query))) return null;
  return { start: caret - query.length - 1, query };
}
