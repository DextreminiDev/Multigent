# Security

Multigent lets an **owner's API key** pay for a model that **other people** talk to. That shapes the whole threat model: collaborators are semi-trusted (they can spend the owner's quota and try to manipulate the model, but must never obtain the key or act outside their role), and everything else is untrusted.

This document describes the controls, where they live in the code, and which tests prove them. Design detail is in [ARCHITECTURE.md](ARCHITECTURE.md) (§11 especially).

## Threat model

| Asset | Threat | Main controls |
|---|---|---|
| Owner's API key | Read back via API/WS/logs/errors; stolen from the DB; prompt-injected out of the model | Write-only key slot, AES-256-GCM with AAD, decrypt only for the call, global redaction, key never in model context |
| Owner's money/quota | Collaborators (or a compromised collaborator account) spamming the model | Per-user message rate limit, daily chat cap, per-member caps, RPM ceiling, usage panel |
| Chat content | Read by non-members, removed members, other chats' members | Server-side authorization on every REST + WS action, 404 for non-members, live kick on removal |
| Accounts / sessions | Session theft, login CSRF, CSRF on writes, open redirects | HttpOnly SameSite session cookie, server-side revocable sessions, CSRF tokens + Origin allowlist, OAuth `state` bound to the browser, `next` path validation |
| Browsers of members | XSS through model output or messages | No raw HTML in markdown, sanitizer, CSP, link hardening, images not loaded |
| Internal network | SSRF through an owner-supplied model URL | Public-HTTPS-only base URLs, resolved-address check |
| Attribution | One user impersonating another to the model | Server-built speaker tags, escaping, sanitized names |

## API keys

**Lifecycle** (`app/chats/routes.py`, `app/security/keyvault.py`, `app/turns/runner.py`):

1. `PUT /chats/{id}/key` (owner only, CSRF-protected). The body field is a `SecretStr` (masked repr). The key is validated by **listing models**, which costs no tokens. If the provider rejects it, nothing is stored.
2. Encrypted with **AES-256-GCM**, a random 96-bit nonce per record, and **AAD = chat id + provider**, so a ciphertext copied to another chat or relabelled as another provider fails to decrypt. Master keys come from `KEY_ENCRYPTION_KEYS` with a key id per record, so they can be rotated (`LocalKeyVault.rewrap`). The `KeyVault` interface is ready for a KMS.
3. Stored: ciphertext, nonce, key id, provider, last 4 characters, validation status. **No API returns more than `last4` and status**, and only to the owner.
4. The worker decrypts **immediately before each provider call**, passes the key as a function argument (never an attribute or env var), and drops it afterwards.
5. Delete is a hard `DELETE` and **stops any reply using the key**.

**Never leaks** (`app/security/redaction.py`, `app/logging.py`, `app/errors.py`):

- A structlog processor and a stdlib logging filter (uvicorn's access log is routed through it) scrub key-shaped strings (`AIza…`, `sk-…`, `sk-ant-…`, `sk-or-…`, `?key=`, auth headers) plus **the literal key value registered for the current request/turn**, so even unusually shaped keys are caught.
- Every provider exception is redacted when constructed, and again before it is stored on the turn.
- FastAPI's default 422 echoes request input; ours never does.
- HTTP client logging is disabled.
- The key is never in the model's context, so prompt injection can't exfiltrate it.

**Proof:** `tests/test_key_never_leaks.py` uses a deliberately leaky fake provider that logs the key and echoes it in errors, drives every endpoint as owner, editor and viewer, and asserts the key appears in **no** response, header, pub/sub event, Redis value, DB column (other than the ciphertext) or log line. `tests/test_ws.py` asserts it never travels over the WebSocket; `tests/test_turns.py` covers provider errors that echo it.

**Production guards** (`app/config.py`): the API refuses to start with `ENV=production` and `DEV_LOGIN=1`, a short `SESSION_SECRET`, no master key, or the public dev master key from docker-compose.

## Authentication and sessions

- **OAuth** (Google, GitHub; `app/auth/oauth.py`): authorization-code flow with PKCE for Google, a single-use `state` (Redis GETDEL, 10 minutes) **bound to the browser** by an HttpOnly cookie (prevents login CSRF), **verified email required**, accounts linked by verified email only, and `next` restricted to same-origin paths.
- **Dev login** exists only with `DEV_LOGIN=1` and is rate-limited per IP.
- **Server-side sessions** in Redis. The cookie holds 256 random bits; Redis stores only its SHA-256, so a Redis dump can't be replayed. Cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` in production. Sessions can be revoked instantly ("log out everywhere" on account deletion).
- **CSRF:** every state-changing REST call needs `X-CSRF-Token` = HMAC(`SESSION_SECRET`, session id), handed out by `GET /me`, plus an `Origin` allowlist check. The WebSocket requires an allowed `Origin` and the session cookie; no tokens in URLs.

## Authorization

- One function, `authorize(role, action)` in `app/chats/permissions.py`, implements the permissions matrix. It is used by REST and WS alike. `tests/test_permissions.py` checks every role × action pair.
- Non-members get **404, not 403**, so chat ids can't be probed.
- Removing a member (or a member leaving) publishes on a per-user control channel; **every API instance immediately drops that user's live subscription** (`tests/test_ws.py::test_removed_member_is_kicked_from_live_socket`). Sends re-check membership in the database every time.
- Owner-only data (custom instructions, caps, base URL, key hint, usage, summary text) is filtered out of responses for collaborators.

## Invites

32-byte random tokens shown once, stored as SHA-256, role-scoped, with optional expiry and max uses. Accept is atomic (the row is locked, so `max_uses` holds under concurrent accepts), idempotent for existing members, and capped at 10 collaborators. Preview and accept are rate-limited per IP. Tokens are scrubbed from logged URLs, and the invite page sends `Referrer-Policy: no-referrer`.

## Model output and the browser

- Markdown is rendered with `react-markdown` (**no raw HTML**) plus `rehype-sanitize`; links open with `noopener noreferrer nofollow`, and images render as links instead of loading (no tracking pixels). Code highlighting runs on text only.
- **CSP:** `default-src 'self'`, connections only to the API and its WebSocket, `frame-ancestors 'none'`, `object-src 'none'`. Also sent: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`. API responses are `Cache-Control: no-store`.
- Tests: `conversation.test.tsx` (script tags, `javascript:` links, images).

## Prompt integrity

Every human message reaches the model inside a server-built `<message from="NAME" role="ROLE">` tag. Content is XML-escaped and display names are restricted to letters, digits, space, `.`, `_` and `-`, so nobody can close a tag or forge another speaker. Namesakes are disambiguated ("Sam (2)"). The fixed preamble tells the model that the tag is the only source of identity and that participants can't override the system message. Golden and spoofing tests live in `tests/test_prompt.py`.

This is **mitigation, not a guarantee**: a model can still be talked into ignoring the owner's custom instructions. The owner's key, other chats and other users' data are never in context, so the blast radius is limited to that chat's own conversation and the owner's spend.

## Abuse and cost controls

Per-user message rate limit (token bucket, 6 per minute), daily chat token cap (pre-check before a turn, post-check after), per-member caps, a requests-per-minute ceiling per key, a maximum message length (8,000 characters), a maximum WS frame size (64 KB), bounded per-socket outbound queues (slow consumers are disconnected, not buffered forever), and IP rate limits on login, OAuth start and invite endpoints.

## SSRF

For OpenAI-compatible providers the owner supplies a base URL that the server calls with their key. It must be `https`, carry no credentials or query string, and **resolve only to public addresses** (no loopback, private ranges, link-local or cloud-metadata IPs; `app/security/urls.py`). Residual risk: DNS rebinding between the check and the request; see the limitations below.

## Data retention

- Messages are kept until their chat is deleted.
- **Deleting a chat** hard-deletes everything in one transaction: key, messages, events, summaries, invites, usage.
- **Deleting an account** (`DELETE /me`) hard-deletes the chats the user owns, removes them from other chats (their past messages stay, attributed by the name they had when sending, so the conversation still reads correctly), scrubs their email and name, deletes their sign-in identities, and revokes all sessions.
- Presence, typing, stream buffers, locks and rate-limit counters live only in Redis with TTLs.

## Known limitations and recommendations

- **DNS rebinding** on custom base URLs: resolution is checked before the call, but the HTTP client re-resolves. For hostile multi-tenant deployments, route provider traffic through an egress proxy that enforces the same allowlist, or disable `openai_compatible`.
- **The master key lives in an environment variable.** For production, use a managed KMS (envelope encryption) behind the existing `KeyVault` interface, and rotate.
- **CSP allows `'unsafe-inline'` scripts** because Next.js injects inline bootstrap code. Moving to nonce-based CSP via middleware would tighten this.
- **Rate limits are per IP and per user.** Distributed abuse across many accounts is bounded by the owner's caps rather than prevented.
- **No end-to-end encryption:** the server can read chat content (by design; it must send it to the model).

## Reporting

Please report vulnerabilities privately to the maintainer instead of opening a public issue. Include steps to reproduce and the affected version or commit.
