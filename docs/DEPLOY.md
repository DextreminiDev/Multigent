# Deploying Multigent

Three processes, two managed datastores:

| Piece | What it is | Where |
|---|---|---|
| `web` | Next.js app (UI only) | Vercel (or any Node host / the `apps/web` Docker image) |
| `api` | FastAPI: REST + **WebSockets** | A long-running container host: Fly.io, Render, Railway… (not serverless: WebSockets need a persistent process) |
| `worker` | Same image as `api`, command `python -m app.worker`: runs the turns | Same host as `api`, separate service, scale independently |
| Postgres 16 | Source of truth | Managed Postgres (Fly Postgres, Neon, Supabase, RDS…) |
| Redis 7 | Locks, pub/sub, queues, sessions, rate limits | Managed Redis with **pub/sub and Lua** (Upstash, Redis Cloud, Fly Redis…) |

## 1. Domains: put web and API on one site

The session cookie is `HttpOnly; Secure; SameSite=Lax`. Browsers only send it on the web app's `fetch`/WebSocket calls to the API when both are on the **same registrable domain**:

```
https://app.example.com   → web (Vercel custom domain)
https://api.example.com   → api (custom domain on your container host)
COOKIE_DOMAIN=.example.com
```

Default hostnames like `*.vercel.app` + `*.fly.dev` are different sites, so the cookie would be dropped. Use custom domains for both.

## 2. Secrets

```bash
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
```
Use the output as a master key: `KEY_ENCRYPTION_KEYS={"k1":"<that value>"}`, `KEY_ENCRYPTION_ACTIVE_ID=k1`. **Back it up.** Losing it makes every stored API key unreadable (owners would have to re-enter them).

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
Use the output as `SESSION_SECRET`.

The API refuses to start in production with dev login on, a short session secret, no master key, or the public dev key from docker-compose.

## 3. Environment variables (api and worker)

| Variable | Example | Notes |
|---|---|---|
| `ENV` | `production` | Enables the production guards and `Secure` cookies |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:5432/multigent` | Note the `+asyncpg` driver |
| `REDIS_URL` | `rediss://default:pass@host:6379/0` | TLS (`rediss://`) where offered |
| `KEY_ENCRYPTION_KEYS` | `{"k1":"base64…"}` | JSON; several ids allowed during rotation |
| `KEY_ENCRYPTION_ACTIVE_ID` | `k1` | Id used for new keys |
| `SESSION_SECRET` | 48+ random chars | Signs CSRF tokens |
| `ALLOWED_ORIGINS` | `https://app.example.com` | Comma-separated; CORS + WebSocket Origin check |
| `COOKIE_DOMAIN` | `.example.com` | Shared parent domain |
| `PUBLIC_API_URL` | `https://api.example.com` | Builds OAuth redirect URIs |
| `WEB_URL` | `https://app.example.com` | Where users land after OAuth |
| `OAUTH_GOOGLE_CLIENT_ID` / `_SECRET` | | Optional; Google sign-in appears when both are set |
| `OAUTH_GITHUB_CLIENT_ID` / `_SECRET` | | Optional; GitHub sign-in appears when both are set |
| `DEV_LOGIN` | `0` | Never `1` in production |
| `DEBOUNCE_MS` | `800` | Batching window when idle |
| `WORKER_MAX_CONCURRENT_TURNS` | `20` | Per worker process |
| `MESSAGE_RATE_PER_MIN` | `6` | Per user per chat |
| `SHUTDOWN_GRACE_S` | `30` | Worker drain time on deploy |
| `LOG_LEVEL` | `INFO` | JSON logs, secrets redacted |

**web (build time):** `NEXT_PUBLIC_API_URL=https://api.example.com`. It is baked into the bundle and the CSP, so rebuild when it changes.

## 4. OAuth apps

**Google:** Google Cloud Console → APIs & Services → Credentials → *Create OAuth client ID* (Web application).
- Authorized redirect URI: `https://api.example.com/auth/google/callback`
- Scopes used: `openid email profile`

**GitHub:** Settings → Developer settings → OAuth Apps → *New OAuth App*.
- Homepage URL: `https://app.example.com`
- Authorization callback URL: `https://api.example.com/auth/github/callback`
- Scopes used: `read:user user:email`

For local development, use `http://localhost:8000/auth/{google,github}/callback`, leave `PUBLIC_API_URL`/`WEB_URL` at their localhost defaults, and put the client id/secret in `.env`.

## 5. Example: Fly.io for api + worker

```bash
cd apps/api
fly launch --no-deploy                         # creates fly.toml from the Dockerfile
fly secrets set ENV=production DATABASE_URL=... REDIS_URL=... \
  KEY_ENCRYPTION_KEYS='{"k1":"..."}' KEY_ENCRYPTION_ACTIVE_ID=k1 SESSION_SECRET=... \
  ALLOWED_ORIGINS=https://app.example.com COOKIE_DOMAIN=.example.com \
  PUBLIC_API_URL=https://api.example.com WEB_URL=https://app.example.com
```

In `fly.toml`, define two process groups from the same image and run migrations before new machines take traffic:

```toml
[deploy]
  release_command = "alembic upgrade head"

[processes]
  api = "uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*"
  worker = "python -m app.worker"

[http_service]
  internal_port = 8000
  processes = ["api"]
  [[http_service.checks]]
    path = "/readyz"
    interval = "10s"
```

`--proxy-headers` makes per-IP rate limits see the real client IP behind Fly's proxy. Scale with `fly scale count api=2 worker=2`. Both scale horizontally: API instances fan out through Redis pub/sub, and workers coordinate through per-chat locks.

Render or Railway work the same way: one web service (the api command, health check `/readyz`), one background worker (the worker command), and a pre-deploy command `alembic upgrade head`.

## 6. Web on Vercel

Import the repo, set the **root directory** to `apps/web`, set `NEXT_PUBLIC_API_URL`, and add the custom domain `app.example.com`. No server-side secrets live in the web app.

## 7. After deploying

- `GET https://api.example.com/readyz` → `{"status":"ok","postgres":true,"redis":true}`
- The worker container's healthcheck is `python -m app.worker --check` (a fresh heartbeat in Redis).
- Sign in, create a chat, add a key, and send a message. Open an invite link in a private window to check the second-user path.
- Logs are JSON with chat, turn and user ids; keys and invite tokens are redacted.

## Operations notes

- **Deploys are safe mid-conversation:** a worker that gets SIGTERM stops claiming new turns and drains for `SHUTDOWN_GRACE_S`. Anything still running is recovered by the sweeper on another worker after its lock expires (≤30 s), and the messages are answered exactly once.
- **Redis loss** degrades latency, not correctness: sessions are lost (users sign in again), and the sweeper rebuilds the work queue from Postgres within seconds.
- **Master key rotation:** add `k2` to `KEY_ENCRYPTION_KEYS`, set `KEY_ENCRYPTION_ACTIVE_ID=k2`, deploy, re-encrypt existing rows with `LocalKeyVault.rewrap` (a one-off script over `chat_keys`), then remove `k1`.
- **Backups:** Postgres holds everything that matters; back it up together with the master key.
