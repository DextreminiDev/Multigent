# Plan: mentions and off the record (replaces reply modes and text tags)

Status: built (commits 3ff3713, 0df380d and the e2e/docs commit after them). Replaces the "reply modes + @ai/@skip/@off text tags" feature
(commits bde7544, dc88f6c).

## 1. Behaviour (the whole spec)

One rule for replies: **the AI replies only when it is mentioned.** One switch for privacy:
**off the record hides a message from the AI.** There are no chat modes and no text tags.

| Sender does | AI replies | AI sees it | `to=` in the prompt | Highlighted for |
|---|---|---|---|---|
| plain message | no | yes | `(everyone)` | nobody |
| mentions `@Bob` | no | yes | `Bob` | Bob |
| mentions `@everyone` | no | yes | `(everyone)` | every member except the sender |
| mentions `@ai` | **yes** | yes | `(assistant)` | nobody |
| mentions `@ai` + `@Bob` | **yes** | yes | `(assistant), Bob` | Bob |
| off the record (any mentions except `@ai`) | no | **no** | not in the prompt | the mentioned people, as above |
| off the record + `@ai` | blocked by the composer; rejected by the server (`422 off_with_ai`) | | | |

- A mention only counts if it was **picked from the `@` menu**. Typing "@ai" by hand is plain text.
- The sender's own messages are never highlighted. You can't mention yourself (not offered).
- Everyone in the chat can be mentioned, including viewers. Removed members can't be picked.
- `(everyone)` / `(assistant)` are written in parentheses because display names can't contain
  parentheses, so no human name can ever collide with them. No names are reserved or changed.
- No emojis anywhere in the UI; lucide icons only.

## 2. How a mention is stored: ranges, not tokens

Two industry approaches:

- **Inline tokens** (Slack `<@U123>`, Discord `<@123>`): the text itself contains markup.
  Downsides here: every place that shows or sends text must translate tokens, existing messages
  and the prompt would mix two formats, and a user typing a token-shaped string by hand creates a
  real mention unless we add escaping (Slack escapes `<`, `>`, `&` in all text for this reason).
- **Entities with ranges** (Telegram `MessageEntity`, Teams `entities`): the text stays plain
  ("@Bob can you pay?") and a side list says "characters 0-4 are a mention of user X".

**Chosen: entities.** The text stays human-readable everywhere (prompt, old messages, copy and
paste), "typed by hand = plain text" is automatic, and nothing needs escaping.

```jsonc
// messages.mentions (jsonb), example for "@AI @Bob what do you both think?"
[
  { "type": "ai",   "offset": 0, "length": 3 },
  { "type": "user", "offset": 4, "length": 4, "user_id": "018f…", "name": "Bob" }
]
// type is "ai" | "everyone" | "user"; name = the user's name at send time (snapshot)
```

- Offsets and lengths are in **UTF-16 code units** (what JavaScript strings use, as Telegram
  does). The server converts when it validates and renders, so emoji or other non-BMP characters
  in the text can't shift ranges.
- The chip text in the content is "@" + the name at send time ("@AI", "@everyone", "@Bob").
  When displaying, a user chip shows the person's current name (falling back to the stored name
  for former members); the prompt uses the participant label (see 5).

## 3. Database (migration 0005)

- `messages.mentions jsonb NOT NULL DEFAULT '[]'`.
- Drop `chats.reply_mode` and its check constraint (downgrade re-adds it with `'always'`).
- Keep `messages.intent` (`reply` | `context` | `off`) and its check. Existing rows keep their
  intent and text; old text like "@skip hi" simply shows as text.
- No data rewrite. Existing chats that were in "every message" mode stop auto-replying.

## 4. API and send path

**Request** (REST `POST /chats/{id}/messages` and WS `message.send`):

```jsonc
{
  "client_msg_id": "…",
  "content": "@AI @Bob what do you both think?",
  "mentions": [{ "type": "ai", "offset": 0, "length": 3 },
               { "type": "user", "offset": 4, "length": 4, "user_id": "018f…" }],
  "off": false
}
```

`mentions` defaults to `[]` and `off` to `false`, so old clients still work (their messages
become context-only).

**Server rules** (`app/messages/mentions.py`, replaces `tags.py`), in order:

1. Validate content as today (non-empty, at most 8000 characters).
2. Normalize mentions. An entry is **dropped** (the text stays, as plain text) if:
   - its range is out of bounds or overlaps another entry,
   - the text at the range doesn't start with "@",
   - it's a `user` who isn't a current member of the chat, or is the sender,
   - it's beyond the 50th entry.

   Dropping, not rejecting, covers the honest race where someone is removed between being
   picked and the message being sent. A forged entry can only point at real members.
3. If `off` and any `ai` entry survives: **reject** `422 off_with_ai` ("The AI can't be mentioned
   in an off-the-record message."). The message is not saved and the AI is never called.
4. Intent: `off` if off; else `reply` if any `ai` entry; else `context`.
5. Store the user entries' `name` snapshots from the current member list.
6. Everything after is as today: `reply` uses the 6/min bucket and the member cap check, is
   inserted `pending` and wakes the scheduler; `context`/`off` use the 30/min bucket and are
   inserted `done`.

Idempotent retries (same `client_msg_id`) still return the original message.

**Responses and events:** `message_payload` / `MessageView` gain `mentions` (with names) next to
`intent`. `ChatView`, `ChatPatch` and `chat.updated` lose `reply_mode`.

## 5. Prompt

`render_human` builds each message from its stored text and mentions:

```xml
<message from="Alice" role="owner" time="18:40" to="(assistant), Bob">
@assistant @Bob what do you both think?
</message>
<message from="Alice" role="owner" time="18:41" to="(everyone)">
hotel's booked
</message>
<message from="Bob" role="editor" time="18:42" to="Carol">
@Carol can you pay me back?
</message>
```

- Each mention's range is replaced by its prompt label: `@assistant`, `@everyone`, or the
  person's participant label (`Bob`, `Sam (2)`), or for a former member their stored name. The rest
  of the text is XML-escaped as today.
- `to=` lists the recipients in order of first mention, de-duplicated: `(assistant)`,
  `(everyone)` and people's labels. With no mentions it is `(everyone)`.
- **Old messages** (no entities): `intent = reply` gets `to="(assistant)"`, since they asked the
  AI; anything else gets `to="(everyone)"`.
- `off` messages stay excluded from history, summaries and the cap estimate (already done).
- `HistoryItem.to_ai` is replaced by `intent` + `mentions`.

**Preamble** (replaces the to="group" paragraph):

> Every human message has a `to` attribute saying who it was addressed to: `(assistant)` is you,
> `(everyone)` means the whole group (no one in particular), anything else is a participant's
> label. Reply only to messages whose `to` includes `(assistant)`. All other messages are still
> fully part of the conversation: remember them, and use, quote or count them whenever someone
> asks (for example "what did Alice say to Bob?" or "list my last messages"). When asked about
> earlier messages, recount them from the conversation itself; don't reuse a list you gave
> before, as newer messages may have arrived since.

**Summary prompt** gains: "Keep who said what to whom (each message's `to`), so questions like
'what did Alice ask Bob?' stay answerable."

**Demo model** (`FakeProvider`) answers only messages whose `to` includes `(assistant)`.

## 6. Web app

### Composer (`composer.tsx`, rewritten)
- A plain `<textarea>` with a **highlight backdrop**: a mirrored div behind the transparent
  textarea renders the same text with mention ranges drawn as chips. Same font, padding and
  wrapping; scroll is synced.
- **Mention state:** a list of `{type, userId?, offset, length}` beside the text. On every change,
  the old and new text are compared (common prefix and suffix) to find the edited span. Mentions
  after it shift, and mentions overlapping it are dropped, so editing inside a chip turns it into
  plain text. Backspace right after a chip deletes the whole chip. Pasted text is plain.
- **`@` menu:** opens when "@" is typed at the start of a word. Options, filtered by what's
  typed after "@":
  1. **AI** ("Ask the AI to reply"),
  2. **everyone** ("Notify everyone in the chat"),
  3. members except you, with avatars, matching any word of their name.

  Arrow keys, Enter or Tab to pick, Escape to close, mouse supported. Picking replaces "@query"
  with "@Name " and records the mention. ARIA: listbox with options; the textarea keeps the
  textbox role (as today, so selectors and axe stay happy).
- **Off-the-record toggle:** an icon button (lucide `EyeOff`) left of Send, `aria-pressed`,
  label "Off the record", tooltip "Hidden from the AI". When it's on, the button uses the amber
  accent. It resets after each send.
- **Conflict:** if the toggle is on and an AI chip is present, Send is disabled and the hint line
  says "The AI can't be mentioned in an off-the-record message." This is the only client-side
  block.
- **Preview pill**, from the mentions and the toggle: "AI will reply" (`Sparkles`) /
  "No reply · AI sees it" / "Off the record · hidden from AI" (`EyeOff`).
- Placeholder "Message everyone… mention @AI for a reply". Hint: "@ to mention · Shift+Enter for
  a new line".
- Enter sends unless the menu is open (as today). Viewers still see the read-only notice.

### Sending (`use-live-chat.ts`, `lib/ws/client.ts`, `lib/ws/reducer.ts`)
- `sendMessage(chatId, clientMsgId, content, mentions, off)`: the outbox frame carries both, so
  a resend after a reconnect is identical.
- `PendingSend` gains `mentions` and `off`, so the optimistic bubble shows chips and the retry
  sends the same payload.

### Messages (`message-list.tsx`)
- `MessageText` replaces `TaggedText`: it renders text with mention ranges as chips (AI chip with
  a `Sparkles` accent; person and everyone chips neutral).
- **Mentions you:** a message that mentions you or `@everyone`, written by someone else, gets a
  very light accent (a faint primary tint on the bubble plus a thin primary left edge) and
  screen-reader text "Mentions you". Never on your own messages.
- **Off the record:** the existing small `EyeOff` icon by the timestamp (accessible label, tooltip).
  Nothing else; no text line, no border.
- The "no reply" icon is removed (no reply is now the normal case).

### Removed
- `lib/tags.ts` (text-tag parser), the "AI replies" settings section, the collaborator "AI
  replies" row, `reply_mode` from `ChatPatch`/fixtures, and mode-dependent empty-state and hint
  text.
- Empty chat text: "Say hi. Everyone sees the conversation live. Mention @AI when you want it to
  answer; it reads along either way."

## 7. Tests

**Backend** (new `tests/test_mentions.py`, replacing `test_tags.py`):
- Intent table: plain, @user, @everyone, @ai, @ai+@user, off, off+@user, off+@ai (422).
- Normalization drops: out of bounds, overlap, range not on "@", non-member, removed member,
  self, more than 50; the text is kept.
- UTF-16 offsets with emoji before a mention.
- Name snapshots stored; a former member's mention still renders by snapshot.
- Prompt: `to=` for every row of the table in 1, labels for namesakes (`Sam (2)`), a human named
  "assistant" or "everyone" doesn't collide, legacy rows (intent only), off excluded, escaping
  still applied around mentions.
- A three-person who-said-what-to-whom transcript renders the right `to` on each line.
- Rate-limit split and member cap only for `reply` (already covered; adapted).
- `reply_mode` gone from views; sending it in a PATCH has no effect (unknown fields are ignored).

**Existing backend tests** that expect a reply from plain text (turns, queue, batching, stress,
limits, WebSocket, collab, key-never-leaks, auth/chats/keys) switch to a shared helper
`ai(text)` that builds `content` + an `ai` mention, used by every `send` helper and raw call.

**Web (Vitest):** mention state on edit (shift, drop on edit inside, backspace removes chip),
menu filtering and keyboard, UTF-16 offsets, off toggle and the AI conflict message, preview
pill, `MessageText` chips, "mentions you" accent (not for own messages, yes for @everyone),
off icon, WS client frame carries `mentions` and `off`, reducer pending send keeps them.

**E2E (Playwright):** the helper `send(p, text, {ai: true})` picks AI from the menu. The
multiplayer spec's batching and queue tests mention the AI. A new spec replaces `tags.spec.ts`:
plain message gets no reply; @Bob highlights for Bob only; @everyone highlights for others, not
the sender; off the record shows the icon and the AI can't be mentioned with it on; a batched
@AI reply addresses each person. The axe check runs on the new composer.

## 8. Docs
- ARCHITECTURE: rewrite §6.2b as "Mentions and off the record" (rules, entity format,
  normalization, prompt `to=`); update the data model, §7.2 rendering and §12 rate limits.
- API.md: request fields `mentions` and `off`, the entity format, `422 off_with_ai`, the
  `MessageView.mentions` payload, `reply_mode` removed.
- README: the highlight bullet.

## 9. Build order (each step green before the next; one commit each, nothing pushed)

1. **Backend:** migration 0005, model, `mentions.py`, send path (REST + WS), payloads, prompt
   and preamble, summary prompt, demo model, remove `reply_mode`. Port all backend tests and add
   `test_mentions.py`. Regenerate OpenAPI types.
2. **Web:** composer, send path, message rendering, removals, Vitest.
3. **E2E and docs:** helpers and specs, a full Playwright run against a rebuilt stack, docs.
   A manual check in the browser with three users.

## 10. Risks and how they're handled
- **Composer complexity** (highlight backdrop, range tracking): kept in a small pure module
  (`lib/mentions.ts`) with thorough unit tests; the component only wires it up.
- **UTF-16 vs Python string indexing:** one tested helper on each side.
- **The model still misreads `to=`** (it's probabilistic): explicit values and the preamble rule
  make it much less likely; a stronger model helps further.
- **Behaviour change for existing chats** (no more auto-replies): intended; old messages are
  unchanged.
