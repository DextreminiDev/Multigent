# API

The REST API is described by its OpenAPI schema: [`apps/web/openapi.json`](../apps/web/openapi.json), generated from the code (`pnpm --filter @multigent/web gen:api`) and checked in CI. A running API serves interactive docs at `/docs` (non-production only). The web app's typed client is generated from the same schema.

## Conventions

- **Auth:** the `sid` session cookie (HttpOnly). Browsers send it with `credentials: "include"`.
- **CSRF:** every non-GET request needs `X-CSRF-Token` (from `GET /me`).
- **Errors:** `{"error": {"code": "...", "message": "..."}}` with a meaningful HTTP status. Codes are stable, for example `key_invalid`, `rate_limited`, `member_cap`, `invite_expired`, `chat_full`, `already_sent`, `not_found`.
- **Non-members** get `404` for any chat route, so chat ids can't be probed.

## REST overview

| Area | Endpoints |
|---|---|
| Auth | `GET /auth/providers`, `GET /auth/{google,github}/login`, `GET /auth/{provider}/callback`, `POST /auth/dev/login` (dev only), `POST /auth/logout`, `GET /me`, `DELETE /me` |
| Chats | `POST /chats`, `GET /chats`, `GET /chats/{id}`, `PATCH /chats/{id}` (owner: title, instructions, model, caps), `DELETE /chats/{id}` |
| Messages | `GET /chats/{id}/state` (consistent snapshot for subscribing), `GET /chats/{id}/messages?before_seq=&limit=`, `POST /chats/{id}/messages`, `DELETE /chats/{id}/messages/{msg_id}` (own, still queued) |
| Turn controls | `POST /chats/{id}/turns/{turn_id}/cancel`, `POST /chats/{id}/turns/{turn_id}/retry`, `POST /chats/{id}/messages/{ai_id}/regenerate` |
| Key slot | `PUT /chats/{id}/key` (write-only), `DELETE /chats/{id}/key`, `POST /chats/{id}/key/validate`, `GET /chats/{id}/models`, `GET /providers` |
| Members | `GET /chats/{id}/members`, `PATCH /chats/{id}/members/{user_id}` (role, member cap), `DELETE /chats/{id}/members/{user_id}` (remove or leave) |
| Invites | `POST /chats/{id}/invites`, `GET /chats/{id}/invites`, `DELETE /invites/{id}`, `GET /invites/{token}/preview` (public), `POST /invites/{token}/accept` |
| Context & usage | `GET /chats/{id}/context`, `POST /chats/{id}/context/compact`, `DELETE /chats/{id}/context/summary`, `GET /chats/{id}/usage` |
| Meta | `GET /healthz`, `GET /readyz` |

## WebSocket: `GET /ws`

One socket per browser tab, multiplexing any number of chats. It is authenticated by the session cookie; the `Origin` must be in `ALLOWED_ORIGINS`. Frames are JSON.

### Client → server

Every frame may carry a `ref`; the server answers `{"type":"ack","ref":...}` or `{"type":"error","ref":...,"code":...,"message":...}`.

| type | fields | notes |
|---|---|---|
| `subscribe` | `chat_id`, `after_seq` | Replays persisted events after `after_seq`, then a `turn.snapshot` if a reply is streaming, then live events. The ack carries the resulting `seq`. |
| `unsubscribe` | `chat_id` | |
| `message.send` | `chat_id`, `client_msg_id` (UUID), `content`, `mentions` (optional), `off` (optional) | Idempotent on `client_msg_id`; the ack includes the saved message and `created`. See *Mentions* below |
| `turn.cancel` | `chat_id`, `turn_id` | Same rules as the REST cancel |
| `typing` | `chat_id` | Editors/owner only; throttled |

### Server → client

Persisted events carry a per-chat `seq` (strictly increasing, gapless). Clients apply them in order, ignore `seq <= last`, and resubscribe on a gap.

| type | persisted | payload |
|---|---|---|
| `chat.created`, `chat.updated` | yes | public chat fields that changed, `status` |
| `key.status` | yes | `status` (`ok`/`invalid`/`missing`…), `chat_status` |
| `message.created` | yes | `message` (with `anchor_seq`, `client_msg_id`, and for human messages `intent` (`reply`, `context` or `off`) and `mentions`) |
| `message.updated` | yes | `message` (e.g. a queued message deleted by its author) |
| `message.superseded` | yes | `message_id`, `message_ids` re-queued (regenerate) |
| `member.updated` | yes | `member` (`user_id`, `name`, `role`, `removed`) |
| `turn.started` | yes | `turn_id`, `trigger`, `message_ids`, `author_ids` |
| `turn.completed` | yes | `turn_id`, AI `message`, `message_ids`, tokens |
| `turn.failed` | yes | `turn_id`, `error_code`, friendly `error_message`, `detail`, `partial_text`, `requeued` |
| `turn.cancelled` | yes | `turn_id`, `partial_text`, `cancelled_by` |
| `turn.retried` | yes | `turn_id`, `message_ids` back in the queue |
| `turn.delta` | no | `turn_id`, `idx`, `text` (dedupe by `idx`) |
| `turn.snapshot` | no | `turn_id`, `chunks[]` streamed so far (late joiners) |
| `turn.status` | no | `turn_id`, `status` (`rate_limited`, `unavailable`, `waiting_rate`, `compacting`, `retrying`), `retry_in`, `attempt`, `max_attempts`, `detail` |
| `context.update` | no | `window`, `budget`, `used`, `percent`, `source`, `summary_through` |
| `presence.update` | no | `online` (user ids) |
| `typing` | no | `user_id`, `name`, `ttl_s` |
| `chat.deleted` | no | |
| `kicked` | no | `chat_id`, `reason` (removed/left/account_deleted) |
| `resync_required` | no | `last_seq`: refetch `GET /chats/{id}/state` and resubscribe |

**Resume guarantee:** the server subscribes to the chat's pub/sub channel *before* reading the event log, buffers live events during the replay, and drops any whose `seq` was already replayed. Together with `idx`-deduplicated deltas, a reconnecting client sees every event exactly once (see `tests/test_ws.py` and the Playwright reconnect test).

## Mentions

`POST /chats/{id}/messages` and WS `message.send` take the same fields:

```jsonc
{
  "client_msg_id": "…",
  "content": "@AI @Bob what do you both think?",
  "mentions": [
    { "type": "ai", "offset": 0, "length": 3 },
    { "type": "user", "offset": 4, "length": 4, "user_id": "018f…" }
  ],
  "off": false
}
```

- `type` is `ai`, `everyone` or `user` (`user` needs `user_id`). `offset`/`length` are UTF-16 code units over `content`, and the range must start with "@". At most 50.
- Only a mention of `ai` asks for a reply. `off: true` sends the message off the record (never shown to the AI).
- Invalid mentions (bad ranges, the sender, non-members) are dropped and their text stays plain. `off: true` with an `ai` mention returns `422 off_with_ai` and nothing is saved.
- Saved messages carry `intent` and `mentions`; user mentions include `name`, the name at send time.
