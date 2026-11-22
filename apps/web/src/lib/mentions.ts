/** Mentions and off the record (mirrors apps/api/app/messages/mentions.py).
 *
 *  The AI replies only to messages that mention it. Mentions are ranges over the
 *  plain message text, picked from the composer's @ menu; typing "@ai" by hand is
 *  just text. Offsets are UTF-16 code units, which is what JS string indexes are. */

export type MentionType = "ai" | "everyone" | "user";

export type Mention = {
  type: MentionType;
  offset: number;
  length: number;
  user_id?: string | null;
  /** the user's name when the message was sent (set by the server) */
  name?: string | null;
};

export type Intent = "reply" | "context" | "off";

export type Person = { id: string; name: string; color?: string };

export type MentionOption =
  | { type: "ai"; label: "AI"; help: string }
  | { type: "everyone"; label: "everyone"; help: string }
  | { type: "user"; label: string; help?: undefined; person: Person };

export const AI_OPTION: MentionOption = { type: "ai", label: "AI", help: "Ask the AI to reply" };
export const EVERYONE_OPTION: MentionOption = {
  type: "everyone",
  label: "everyone",
  help: "Address the whole chat",
};

export function intentOf(mentions: Mention[], off: boolean): Intent {
  if (off) return "off";
  return mentions.some((m) => m.type === "ai") ? "reply" : "context";
}

/** Off the record + a mention of the AI can't be sent. */
export function offWithAi(mentions: Mention[], off: boolean): boolean {
  return off && mentions.some((m) => m.type === "ai");
}

/** Valid, sorted, non-overlapping mentions (bad ranges are ignored, as on the server). */
export function validMentions(text: string, mentions: readonly Mention[] | null | undefined): Mention[] {
  const sorted = [...(mentions ?? [])]
    .filter(
      (m) =>
        m.offset >= 0 &&
        m.length > 0 &&
        m.offset + m.length <= text.length &&
        text[m.offset] === "@",
    )
    .sort((a, b) => a.offset - b.offset);
  const out: Mention[] = [];
  for (const m of sorted) {
    const prev = out[out.length - 1];
    if (prev && m.offset < prev.offset + prev.length) continue;
    out.push(m);
  }
  return out;
}

export type Segment = { text: string; mention: Mention | null };

/** Split text into plain and mention segments for rendering. */
export function segments(text: string, mentions: readonly Mention[] | null | undefined): Segment[] {
  const out: Segment[] = [];
  let pos = 0;
  for (const m of validMentions(text, mentions)) {
    if (m.offset > pos) out.push({ text: text.slice(pos, m.offset), mention: null });
    out.push({ text: text.slice(m.offset, m.offset + m.length), mention: m });
    pos = m.offset + m.length;
  }
  if (pos < text.length) out.push({ text: text.slice(pos), mention: null });
  return out;
}

/** Keep mentions in step with an edit: those before the edited span stay, those
 *  after shift, and any the edit touched are dropped (they become plain text). */
export function applyEdit(before: string, after: string, mentions: Mention[]): Mention[] {
  if (before === after) return mentions;
  let start = 0;
  const max = Math.min(before.length, after.length);
  while (start < max && before[start] === after[start]) start++;
  let endBefore = before.length;
  let endAfter = after.length;
  while (endBefore > start && endAfter > start && before[endBefore - 1] === after[endAfter - 1]) {
    endBefore--;
    endAfter--;
  }
  const delta = endAfter - endBefore;
  const out: Mention[] = [];
  for (const m of mentions) {
    let moved: Mention | null = null;
    if (m.offset + m.length <= start) moved = m; // before the edit (typing right after a chip)
    else if (m.offset >= endBefore) moved = { ...m, offset: m.offset + delta }; // after it
    // otherwise the edit touched the chip: drop it (it becomes plain text)
    const same =
      moved &&
      after.slice(moved.offset, moved.offset + moved.length) ===
        before.slice(m.offset, m.offset + m.length);
    if (moved && same) out.push(moved);
  }
  return out;
}

/** The "@query" being typed at the caret, if any: "@" at the start of a word, no
 *  line break since. */
export function mentionQueryAt(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const at = before.lastIndexOf("@");
  if (at < 0) return null;
  if (at > 0 && !/[\s([{"']/.test(before[at - 1])) return null; // emails etc.
  const query = before.slice(at + 1);
  if (/[\n@]/.test(query) || query.length > 40) return null;
  return { start: at, query };
}

function matches(name: string, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const n = name.toLowerCase();
  return n.startsWith(q) || n.split(/\s+/).some((w) => w.startsWith(q));
}

/** @ menu options for a query: AI first, then everyone, then people (not me). */
export function mentionOptions(query: string, people: Person[], meId: string): MentionOption[] {
  const out: MentionOption[] = [];
  if (matches("AI", query) || matches("assistant", query)) out.push(AI_OPTION);
  if (matches("everyone", query) || matches("all", query)) out.push(EVERYONE_OPTION);
  for (const p of people) {
    if (p.id !== meId && matches(p.name, query)) out.push({ type: "user", label: p.name, person: p });
  }
  return out;
}

/** Replace "@query" (from `start` to the caret) with the picked chip plus a space. */
export function insertMention(
  text: string,
  mentions: Mention[],
  start: number,
  caret: number,
  option: MentionOption,
): { text: string; mentions: Mention[]; caret: number } {
  const chip = `@${option.label}`;
  const after = text.slice(caret);
  const insert = chip + (after.startsWith(" ") ? "" : " ");
  const next = text.slice(0, start) + insert + after;
  const shifted = applyEdit(text, next, mentions);
  const mention: Mention =
    option.type === "user"
      ? { type: "user", offset: start, length: chip.length, user_id: option.person.id }
      : { type: option.type, offset: start, length: chip.length };
  return {
    text: next,
    mentions: [...shifted, mention].sort((a, b) => a.offset - b.offset),
    caret: start + insert.length,
  };
}

/** Backspace with the caret right after a chip deletes the whole chip. */
export function removeChipBefore(
  text: string,
  mentions: Mention[],
  caret: number,
): { text: string; mentions: Mention[]; caret: number } | null {
  const m = mentions.find((x) => x.offset + x.length === caret);
  if (!m) return null;
  const next = text.slice(0, m.offset) + text.slice(caret);
  return {
    text: next,
    mentions: mentions
      .filter((x) => x !== m)
      .map((x) => (x.offset > m.offset ? { ...x, offset: x.offset - m.length } : x)),
    caret: m.offset,
  };
}

/** Whether a message should get the "mentions you" accent for this viewer. */
export function mentionsMe(
  mentions: readonly Mention[] | null | undefined,
  authorId: string | null | undefined,
  meId: string,
): boolean {
  if (!mentions?.length || authorId === meId) return false;
  return mentions.some((m) => m.type === "everyone" || (m.type === "user" && m.user_id === meId));
}

export const INTENT_LABEL: Record<Intent, string> = {
  reply: "AI will reply",
  context: "No reply · AI sees it",
  off: "Off the record · hidden from AI",
};
