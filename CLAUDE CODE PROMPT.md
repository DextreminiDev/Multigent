# Project: "Multigent"

You are building a real-time collaborative LLM chat app. It looks and feels like ChatGPT/Claude.ai, but **each chat has an owner who plugs in their own LLM API key, and invited collaborators chat with the same model in the same live conversation, using the owner's key.** Everyone sees everyone's messages and the AI's streamed responses live.

This is intended to be a real product and a strong portfolio piece. Quality bar: clean architecture, correct concurrency behavior, real security around API keys, a polished UI, tests, and docs a recruiter could read in five minutes.

**How to work:**
1. **`docs/ARCHITECTURE.md` already exists and is the authoritative technical design.** Read it fully, then read this document. This document is the product spec and requirements; ARCHITECTURE.md is how to build it. If the two disagree, ARCHITECTURE.md wins (it was written later and refines this one). Do not rewrite it from scratch.
2. If you find a real problem with the architecture while building (a race it misses, a library limitation, a simpler way), stop and tell me in one short message with your proposed fix. Once I agree, update the relevant section of ARCHITECTURE.md so the doc always matches the code. Don't silently diverge. For small implementation choices (library picks, naming, folder details), just decide and keep going; don't write a separate log.
3. Ask me only about true blockers. Otherwise pick the sensible default and move on.
4. Build in the milestones at the bottom, in order. Commit at the end of each milestone with a clear message. After each milestone, run the tests and tell me what works and what doesn't.
5. Never put real secrets in the repo. Provide `.env.example`.

---

## 1. Core concepts

- **User**: signs in (OAuth). Has a display name, avatar color.
- **Chat**: a conversation. Has exactly one **owner**, zero or more **members**, a title, optional owner-written custom instructions (system prompt), a model selection, and exactly one **API key slot** (provider + encrypted key + model).
- **Roles per chat**: `owner` (everything), `editor` (can send prompts), `viewer` (read-only, sees live stream).
- **Message**: a single human prompt (has author) or an AI response.
- **Turn**: one LLM call. A turn consumes a *batch* of one or more pending human messages and produces exactly one AI message. Turns are the unit of queueing.

## 2. The hard problems and how to solve them (follow these designs)

### 2.1 "API keys are one-time use" — context storage
LLM provider APIs are **stateless**: the model has no memory between calls. "Context" is simply the message history we resend each time. So:
- Postgres is the single source of truth for all messages.
- For each turn, the backend **rebuilds the prompt from the DB** and sends it with the owner's key.
- The owner's key is only decrypted inside the worker at call time (see §2.6).
- Context overflow is handled by rolling summarization (§2.5).

### 2.2 Race conditions — simultaneous prompts
Requirement: if 2–3+ people send prompts at the same moment, nothing is lost, order is deterministic, and the AI handles them coherently.

Design:
- Every chat has a monotonically increasing `seq` number. Assign it atomically in Postgres (`UPDATE chats SET last_seq = last_seq + 1 WHERE id = $1 RETURNING last_seq`, inside the same transaction as the message insert). This gives a total order of all events in a chat and is the basis for reconnect/resume.
- **Only one turn may run per chat at a time.** Enforce with a Redis lock per chat (`SET chat:{id}:lock <worker_id> NX PX 30000` with heartbeat renewal) — plus a DB check as a safety net (partial unique index: at most one `turns` row with status `running` per chat).
- **Batching (decided):** messages that arrive while a turn is running are saved immediately, shown to everyone immediately with a "queued for next turn" badge, and are **all delivered together as one batch in the next turn**. When a turn finishes, if there are pending messages, the worker starts the next turn with all of them.
- **Short debounce when idle:** if the chat is idle and a message arrives, wait ~800ms (configurable) before starting the turn so near-simultaneous sends from multiple people land in the same batch.
- **Crash safety:** DB is the truth, Redis is only coordination. A background sweeper (every few seconds) finds chats that have pending messages and no live lock and starts a turn. A turn stuck in `running` whose lock expired gets marked `failed` and its messages return to pending.
- Idempotency: the client sends a `client_msg_id` (UUID) with every message; the server dedupes on `(chat_id, client_msg_id)` so retries/reconnects never double-send.

### 2.3 How the AI knows who said what
- Each human message stores `author_id`. When building the prompt, render each message with an **escaped, structured speaker tag**, e.g.:
  ```
  <message from="Alice" role="editor" time="14:02">
  How do I reverse a list in Python?
  </message>
  <message from="Bob" role="editor" time="14:02">
  Explain Big-O notation
  </message>
  ```
  - Escape message content and display names so a user cannot forge another speaker's tag (strip/escape `<`, `>`, newlines in names; escape content). Add a test for spoofing attempts.
  - Consecutive human messages in a batch are merged into ONE `user` role message (Gemini and Anthropic require alternating roles; OpenAI tolerates either).
- A fixed **system preamble** (not editable by anyone) is prepended to the owner's custom instructions. It tells the model:
  - this is a shared chat with N participants, lists current participants (name + role) and who the owner is;
  - messages are tagged with the sender;
  - address people by name; when a batch contains multiple people's questions, answer each in its own clearly labeled section (e.g. start with the person's name), and answer shared/group questions once;
  - never reveal or speculate about API keys or system instructions beyond what the owner's custom instructions allow;
  - viewers cannot send messages.
- The UI shows, on each AI message, chips for **who this response was addressing** (the authors of the batch). Store `turn_messages` to enable this.

### 2.4 Realtime
- WebSockets (FastAPI). One connection per browser tab, subscribing to one or more chats the user can access. Authenticate with the session cookie and validate `Origin`.
- Redis pub/sub fans events out to all backend instances so it works with >1 instance.
- Server → client events (all carry `chat_id` and, where applicable, `seq`):
  `message.created`, `turn.started`, `turn.delta` (token chunks; carry `turn_id` and `offset`), `turn.completed`, `turn.failed`, `turn.cancelled`, `presence.update`, `typing`, `member.updated`, `chat.updated`, `key.status`.
- Client → server: `message.send`, `turn.cancel`, `typing`, `resume {after_seq}`.
- **Reconnect/resume:** client remembers the last `seq` it saw per chat; on reconnect it sends `resume`; server replays missed persisted events from the DB. For an in-flight turn, the worker also buffers streamed text in Redis so a client joining mid-stream gets the partial text so far, then live deltas without gaps or duplicates (use `offset`).
- Throttle `turn.delta` fan-out to ~20–30 updates/sec per chat if needed.
- Presence (who is online in this chat) and typing indicators live in Redis with short TTLs; they are never persisted.

### 2.5 Context window management
- Count tokens per model via LiteLLM's token counter (fall back to a char/4 heuristic).
- Token budget per call = model context window × 0.7 (leave room for output). Default max output tokens configurable per chat.
- When history exceeds the budget: keep the most recent messages verbatim, and fold older ones into a **rolling summary** stored in `chat_summaries` (covers `seq` range, summary text). The summary is generated by the same provider/key via a background "summarize" turn that does NOT show up as a chat message but is visible in the owner's chat settings ("Context: 38% used, summary covers messages 1–120"). Summaries must preserve who said what and unresolved asks.
- Show a context-usage meter in the UI.

### 2.6 API key security (treat as critical)
- Encrypt at rest with **AES-256-GCM** (`cryptography` lib), random nonce per record, key-id column to allow rotation. Master key comes from env (`KEY_ENCRYPTION_SECRET`) behind a small `KeyVault` interface so a KMS can be dropped in later.
- Store only: ciphertext, nonce, key_id, provider, `last4` hint. The plaintext key is **never**: returned by any API, sent over WebSocket, logged, included in exceptions/Sentry, or readable by collaborators. Not even the owner can read it back after saving (they can replace or delete it).
- Decrypt only in the worker immediately before the provider call; keep in memory only for that call.
- Validate on save with a cheap provider call; store validation status.
- Redact secrets in logs globally (log filter for key-shaped strings + never log request headers/bodies to providers).
- If the provider returns 401/403 → mark chat `key_invalid`, pause queue, broadcast `key.status` to everyone ("The owner's API key needs attention"), notify the owner. Messages stay queued and run once the key is fixed.
- Prompt-injection note: the key is never in model context, so the model can't leak it.
- Owner can delete the key at any time; in-flight turn is cancelled.

### 2.7 Cost and abuse protection (collaborators spend the owner's money/quota)
- Per-user-per-chat message rate limit (default 6/min) — token bucket in Redis.
- Max message length (default 8k chars). Max collaborators per chat (default 10).
- Owner-set **per-chat daily token cap** and per-member optional cap; when hit, queue pauses with a clear message.
- Show running usage (tokens in/out per member, estimated cost where pricing is known) in the owner's settings panel. Store usage on each turn.
- Gemini free-tier keys have very low rate limits (low RPM/TPM/RPD). On HTTP 429 or quota errors: exponential backoff with jitter (max ~3 retries), a `turn.status: "rate_limited, retrying in Ns"` event to all clients, and a friendly final error if it keeps failing. Do not lose queued messages.

## 3. Provider layer (decided: max flexibility)

- Use **LiteLLM** (or a thin adapter around it) so one code path supports **Google Gemini (first-class target — owner starts with a free Gemini API key), Anthropic, OpenAI, and OpenRouter / any OpenAI-compatible base URL**.
- Define an internal `LLMProvider` protocol: `stream_chat(messages, system, model, max_tokens, api_key, base_url?) -> AsyncIterator[Chunk]`, `validate_key()`, `list_models()` (where available), `count_tokens()`. Normalize errors into: `AuthError`, `RateLimitError`, `QuotaExceeded`, `ContextTooLong`, `ProviderUnavailable`, `ContentBlocked`, `Unknown`.
- Handle provider quirks: Gemini uses `model` instead of `assistant` role and a separate system instruction; safety blocks can return empty/blocked responses → surface as `ContentBlocked` with a readable message; different finish reasons; usage metadata may arrive only at the end.
- Owner picks provider + model in chat settings; changing it mid-chat is allowed (history is provider-agnostic in our DB).
- Include a `FakeProvider` (deterministic, configurable delay/chunks/failures) used by all tests and a "demo mode" for local dev with no key.

## 4. Tech stack (decided)

**Frontend:** Next.js (App Router) + TypeScript, Tailwind CSS, shadcn/ui, TanStack Query for REST data, a small typed WebSocket client with auto-reconnect + resume, `react-markdown` + `remark-gfm` + syntax highlighting (shiki) for AI output, Zustand (or similar) for live chat state.

**Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async) + Alembic, asyncpg, `redis.asyncio`, LiteLLM, `cryptography`, `structlog`. Run API (uvicorn) and **worker** as separate processes from the same codebase (worker consumes wake-ups and runs turns). Use `uv` for deps. Ruff + mypy (strict where reasonable).

**Data:** Postgres 16 (truth), Redis 7 (locks, pub/sub, rate limits, presence, stream buffers).

**Auth:** OAuth login (Google + GitHub) handled by the backend, httpOnly + Secure + SameSite=Lax session cookie (server-side session in Redis or signed JWT with rotation — pick and justify). CSRF protection for state-changing REST calls. Also support a `DEV_LOGIN` bypass behind an env flag for local dev/tests only.

**Infra:** `docker-compose.yml` for Postgres + Redis + api + worker + web for local dev; Dockerfiles for backend/worker. Deploy target documented: frontend on Vercel, backend+worker on Fly.io/Railway/Render with managed Postgres and Redis (note: WebSockets need a long-running server, not Vercel serverless functions).

**CI:** GitHub Actions: lint, typecheck, backend tests, frontend tests, e2e.

## 5. Data model (starting point — refine as needed)

- `users(id, email, name, avatar_color, created_at)`
- `chats(id, owner_id, title, custom_instructions, provider, model, max_output_tokens, daily_token_cap, status[active|key_invalid|paused], last_seq, created_at, archived_at)`
- `chat_keys(chat_id PK, ciphertext, nonce, key_id, provider, base_url?, last4, validated_at, status)`
- `chat_members(chat_id, user_id, role[owner|editor|viewer], member_token_cap?, joined_at)`
- `chat_invites(id, chat_id, token_hash, role, expires_at, max_uses, uses, revoked_at, created_by)`
- `messages(id, chat_id, seq, author_id NULL for AI, kind[human|ai|system_event], content, client_msg_id, status[pending|batched|done|failed|deleted], turn_id NULL, created_at)` — unique `(chat_id, client_msg_id)`, unique `(chat_id, seq)`
- `turns(id, chat_id, status[pending|running|completed|failed|cancelled], started_at, finished_at, provider, model, tokens_in, tokens_out, error_code, error_detail, ai_message_id)` — partial unique index: one `running` per chat
- `turn_messages(turn_id, message_id)` (which human messages were in the batch)
- `chat_summaries(id, chat_id, from_seq, to_seq, content, created_at)`
- `usage_events(id, chat_id, user_id NULL, turn_id, tokens_in, tokens_out, est_cost, created_at)`

## 6. Permissions matrix

| Action | owner | editor | viewer |
|---|---|---|---|
| Read messages / watch live | ✓ | ✓ | ✓ |
| Send prompts | ✓ | ✓ | ✗ |
| Delete own message (only while still pending) | ✓ | ✓ | ✗ |
| Cancel running turn | ✓ | only if their message is in the batch | ✗ |
| Regenerate last AI message | ✓ | ✗ | ✗ |
| Edit title / instructions / model / key / caps | ✓ | ✗ | ✗ |
| Create/revoke invites, change roles, remove members | ✓ | ✗ | ✗ |
| Leave chat | ✗ (must delete or transfer) | ✓ | ✓ |
| Delete chat | ✓ | ✗ | ✗ |

Every REST and WebSocket action must check membership + role server-side. Removing a member immediately closes their live subscriptions to that chat.

## 7. Edge cases that MUST be handled (write tests for the starred ones)

1. ★ Three users send within the same 100ms → all saved, ordered by `seq`, delivered in one batch (or two, if debounce boundary), no duplicates, no loss.
2. ★ Messages sent during a running turn → badge "queued", included in the next turn exactly once.
3. ★ Duplicate send (client retry with same `client_msg_id`) → one message.
4. ★ Worker crash mid-turn → lock expires, sweeper recovers, partial AI text is discarded or kept and marked `failed`, batch messages return to pending and rerun.
5. ★ Client disconnects mid-stream and reconnects → sees partial text + continues without gaps/dupes.
6. ★ Speaker-tag spoofing attempts in names/content are neutralized.
7. ★ Collaborator tries to read/send in a chat they're not in / a viewer tries to send / a removed member keeps a socket open → rejected.
8. ★ API key never appears in any API response, WS event, log line, or error message (add an automated test that greps logs/responses for the key).
9. Key invalid / revoked mid-turn; key replaced while messages are queued.
10. Provider rate limit (429) → backoff + status event; provider outage; content blocked; context too long (trigger summarization then retry once).
11. Owner changes model/provider mid-chat; owner deletes the key while a turn runs.
12. Owner is offline — collaborators can still chat (key is server-side). Make this explicit in the UI.
13. Invite link: expiry, max uses, revoked link, already-a-member, link used by logged-out user (redirect to login then back).
14. User removed while their message is queued (keep the message, mark author as "former member").
15. Two tabs for the same user; very long messages; very long AI output; markdown/code rendering; XSS in markdown (sanitize; no raw HTML).
16. Clock skew: use server timestamps and `seq` for ordering, never client time.
17. Cancel pressed right as the turn completes (idempotent, no double state transition).
18. Daily token cap reached mid-batch.
19. Regenerate: replaces the last AI message in a way all clients render consistently (keep old as hidden/versioned).
20. Account deletion / chat deletion: hard delete keys immediately; messages per a documented retention rule.

## 8. REST API surface (starting point)

Auth: `GET /auth/{provider}/login`, `GET /auth/{provider}/callback`, `POST /auth/logout`, `GET /me`.
Chats: `POST /chats`, `GET /chats`, `GET /chats/{id}`, `PATCH /chats/{id}`, `DELETE /chats/{id}`, `GET /chats/{id}/messages?before_seq=&limit=`.
Key: `PUT /chats/{id}/key` (write-only), `DELETE /chats/{id}/key`, `POST /chats/{id}/key/validate`.
Members/invites: `GET /chats/{id}/members`, `PATCH /chats/{id}/members/{user_id}`, `DELETE /chats/{id}/members/{user_id}`, `POST /chats/{id}/invites`, `DELETE /invites/{id}`, `POST /invites/{token}/accept`.
Usage: `GET /chats/{id}/usage`.
Realtime: `GET /ws`.
Meta: `GET /healthz`, `GET /readyz`.
Generate an OpenAPI schema and a typed TS client from it for the frontend.

## 9. UX and design direction

Goal: feels as polished as ChatGPT/Claude.ai, but unmistakably *multiplayer*. Avoid a generic template look: choose a distinct but restrained visual identity (type pairing, accent color, spacing), dark mode by default with a light mode, fully responsive (mobile is a real target: people join via invite link on phones).

**Layout:** left sidebar (chat list with "Owned by me" / "Shared with me", unread dots, new chat) → main conversation → collapsible right panel (participants with presence dots, invite button, chat settings, usage/context meter).

**Conversation view:**
- Human messages show avatar (deterministic color), name, time; your own messages visually distinct. AI messages are full-width, markdown-rendered, with streaming caret.
- Each AI message shows "Replying to: Alice, Bob" chips.
- Messages sent while the AI is busy get a subtle "Queued · will be answered next" badge; a banner shows "2 messages waiting".
- Live typing indicators ("Bob is typing…") and presence avatars in the header.
- Composer: Enter to send, Shift+Enter newline; disabled with a clear reason for viewers, key-invalid chats, rate-limited users, or capped chats. Stop button while generating (shown per permission matrix).
- Turn states shown inline: thinking → streaming → done / failed (retry) / rate-limited-retrying (with countdown).
- Copy button, code block copy, regenerate (owner).

**Owner onboarding (important for the Gemini free key flow):** a first-run checklist in a new chat: choose provider → paste key (with a short "where to get a free Gemini key" hint and a link to Google AI Studio) → validate → pick model → invite people. Key field is write-only with a masked `•••• last4` display afterwards.

**Invite flow:** shareable link with role selector and expiry; landing page shows chat title, owner, and member count before joining; login redirect preserves the invite.

**Empty/error states** are designed, not afterthoughts. Keyboard accessible, ARIA live regions for streaming, respects reduced motion.

## 10. Testing requirements

- **Backend unit/integration (pytest, pytest-asyncio):** use real Postgres + Redis via docker/testcontainers, `FakeProvider` for LLM. Cover the ★ edge cases above, especially the concurrency ones (use `asyncio.gather` to fire simultaneous sends).
- **Property/stress test:** N concurrent users × M messages → assert no loss, no dupes, strictly increasing `seq`, at most one running turn per chat at any time.
- **Frontend:** component tests for the message list/composer states; a WS-client test for reconnect + resume.
- **E2E (Playwright):** two or three browser contexts (owner + 2 collaborators) in the same chat: simultaneous send → all see consistent order and the AI's batched, per-person answers; reconnect mid-stream; viewer cannot send.
- **Security tests:** key-leak grep test, authorization matrix tests, spoofing tests, rate-limit tests.

## 11. Observability and ops

Structured JSON logs with request/chat/turn IDs and secret redaction; Prometheus-style metrics (turns/sec, queue depth, turn latency, provider errors by type, WS connections); `/healthz` and `/readyz`; graceful shutdown (finish or requeue in-flight turn); documented env vars; database migrations run on deploy.

## 12. Repo layout (suggested)

```
/apps/web            Next.js frontend
/apps/api            FastAPI app (+ worker entrypoint)
/packages/types      generated API types (optional)
/docs                ARCHITECTURE.md (already written), SECURITY.md, API.md
/docker-compose.yml
/.github/workflows
```
README must include: what it is, architecture diagram (Mermaid), the concurrency/batching explanation, the security model for API keys, local setup in <5 commands, and a short demo GIF placeholder.

## 13. Milestones (build in this order)

1. **Scaffold:** monorepo, docker-compose, CI, lint/typecheck, health endpoints, migrations (schema from ARCHITECTURE.md §4), `FakeProvider`.
2. **Auth + chats + key vault:** OAuth (+dev login), chat CRUD, encrypted write-only key with validation, permissions framework, tests for key-secrecy.
3. **Single-user generation:** provider layer (Gemini, Anthropic, OpenAI, OpenRouter via LiteLLM), prompt builder with speaker tags + system preamble, worker + streaming over WebSocket, markdown UI. Works end-to-end for one user.
4. **Collaboration:** invites, roles, members, presence, typing, live message fan-out, multi-user UI.
5. **Queue and concurrency:** per-chat lock, debounce + batching, sweeper/crash recovery, idempotency, cancel, reconnect/resume, stress tests.
6. **Context + limits:** token counting, rolling summaries, context meter, rate limits, caps, usage panel, 429 backoff UX.
7. **Polish and ship:** error/empty states, mobile pass, accessibility pass, e2e suite, security review (`docs/SECURITY.md`), deploy guide, README.

## 14. Definition of done

- Three browser sessions can chat live in one chat; simultaneous sends are batched and the AI replies addressing each person by name.
- Owner's key is demonstrably never exposed (tests prove it).
- Works with a free Gemini key out of the box, with graceful handling of its rate limits.
- Kill the worker mid-turn → system recovers without losing messages.
- `docker compose up` gives a working local environment; CI is green; docs are complete.

Start now: read `docs/ARCHITECTURE.md` and this document, give me a 5-line summary of your understanding plus any blockers, then begin milestone 1.