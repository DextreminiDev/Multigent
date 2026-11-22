import { describe, expect, it } from "vitest";
import {
  applyEdit,
  insertMention,
  intentOf,
  mentionOptions,
  mentionQueryAt,
  mentionsMe,
  offWithAi,
  removeChipBefore,
  segments,
  type Mention,
  type MentionOption,
} from "./mentions";

const bob: Mention = { type: "user", offset: 4, length: 4, user_id: "u2" };
const ai: Mention = { type: "ai", offset: 0, length: 3 };
const PEOPLE = [
  { id: "u1", name: "Alice" },
  { id: "u2", name: "Bob" },
  { id: "u3", name: "Mary Jane" },
];

describe("intent", () => {
  it("replies only when the AI is mentioned; off hides it", () => {
    expect(intentOf([], false)).toBe("context");
    expect(intentOf([bob], false)).toBe("context");
    expect(intentOf([{ type: "everyone", offset: 0, length: 9 }], false)).toBe("context");
    expect(intentOf([ai, bob], false)).toBe("reply");
    expect(intentOf([bob], true)).toBe("off");
    expect(offWithAi([ai], true)).toBe(true);
    expect(offWithAi([bob], true)).toBe(false);
    expect(offWithAi([ai], false)).toBe(false);
  });
});

describe("editing keeps mentions in step", () => {
  const text = "@AI @Bob hi";
  it("typing before a chip shifts it; typing after keeps it", () => {
    expect(applyEdit(text, "x" + text, [ai, bob])).toEqual([
      { ...ai, offset: 1 },
      { ...bob, offset: 5 },
    ]);
    expect(applyEdit(text, text + "!", [ai, bob])).toEqual([ai, bob]);
    expect(applyEdit("@Bob", "@Bob!", [{ ...bob, offset: 0 }])).toEqual([{ ...bob, offset: 0 }]);
  });

  it("editing inside a chip turns it into plain text", () => {
    expect(applyEdit(text, "@AI @Bxob hi", [ai, bob])).toEqual([ai]);
    expect(applyEdit(text, "@AI @Bo hi", [ai, bob])).toEqual([ai]);
  });

  it("deleting everything drops every chip", () => {
    expect(applyEdit(text, "", [ai, bob])).toEqual([]);
  });

  it("works with UTF-16 offsets (emoji before a chip)", () => {
    const before = "🎉 @Bob";
    const m: Mention = { type: "user", offset: 3, length: 4, user_id: "u2" };
    expect(before.slice(3, 7)).toBe("@Bob");
    expect(applyEdit(before, "🎉🎉 @Bob", [m])).toEqual([{ ...m, offset: 5 }]);
  });
});

describe("the @ menu", () => {
  it("opens on @ at the start of a word, not inside emails", () => {
    expect(mentionQueryAt("hi @bo", 6)).toEqual({ start: 3, query: "bo" });
    expect(mentionQueryAt("@", 1)).toEqual({ start: 0, query: "" });
    expect(mentionQueryAt("mail bob@ai.com", 15)).toBeNull();
    expect(mentionQueryAt("@a\nb", 4)).toBeNull();
  });

  it("lists AI first, then everyone, then people (not me), filtered", () => {
    const labels = (q: string) => mentionOptions(q, PEOPLE, "u1").map((o) => o.label);
    expect(labels("")).toEqual(["AI", "everyone", "Bob", "Mary Jane"]);
    expect(labels("a")).toEqual(["AI", "everyone"]); // "all" alias; Alice is me
    expect(labels("ja")).toEqual(["Mary Jane"]); // any word of the name
    expect(labels("zz")).toEqual([]);
  });

  it("picking replaces the query with a chip and a space", () => {
    const option = mentionOptions("bo", PEOPLE, "u1")[0] as MentionOption;
    const out = insertMention("hi @bo", [], 3, 6, option);
    expect(out.text).toBe("hi @Bob ");
    expect(out.mentions).toEqual([{ type: "user", offset: 3, length: 4, user_id: "u2" }]);
    expect(out.caret).toBe(8);
  });

  it("Backspace right after a chip removes the whole chip", () => {
    const out = removeChipBefore("@AI @Bob hi", [ai, bob], 8);
    expect(out).toEqual({ text: "@AI  hi", mentions: [ai], caret: 4 });
    expect(removeChipBefore("@AI @Bob hi", [ai, bob], 9)).toBeNull();
  });
});

describe("rendering", () => {
  it("splits text into chips and ignores bad ranges", () => {
    expect(segments("@AI @Bob hi", [bob, ai, { type: "user", offset: 1, length: 3 }])).toEqual([
      { text: "@AI", mention: ai },
      { text: " ", mention: null },
      { text: "@Bob", mention: bob },
      { text: " hi", mention: null },
    ]);
  });

  it("'mentions you' is for the person mentioned and @everyone, never your own message", () => {
    expect(mentionsMe([bob], "u1", "u2")).toBe(true);
    expect(mentionsMe([bob], "u1", "u3")).toBe(false);
    expect(mentionsMe([{ type: "everyone", offset: 0, length: 9 }], "u1", "u3")).toBe(true);
    expect(mentionsMe([{ type: "everyone", offset: 0, length: 9 }], "u3", "u3")).toBe(false);
    expect(mentionsMe([ai], "u1", "u2")).toBe(false);
  });
});
