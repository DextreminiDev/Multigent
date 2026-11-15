# Multigent

Real-time shared AI chats. A chat's **owner** plugs in their own LLM API key (Gemini, Anthropic, OpenAI, OpenRouter); invited **collaborators** prompt the same model in the same live conversation, and everyone watches the AI's answers stream.

> Status: milestone 2 (auth, chats, key vault) of 7. Full README (architecture diagram, concurrency model, key security model, demo) lands in milestone 7. The design lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Local setup

Requires Docker, [uv](https://docs.astral.sh/uv/), Node 22 and pnpm.

```bash
cp .env.example .env
docker compose up -d --build
```

- Web: http://localhost:3000 (dev sign-in: any name + email; pick "Demo (no key)" as the provider to try it without a real key)
- API: http://localhost:8000 (`/healthz`, `/readyz`, `/docs`)

### Develop without containers for api/web

```bash
docker compose up -d postgres redis
cd apps/api && uv sync && uv run alembic upgrade head && uv run uvicorn app.main:app --reload
cd apps/api && uv run python -m app.worker
pnpm install && pnpm dev:web
```

### Checks

```bash
cd apps/api && uv run ruff check . && uv run mypy app tests && uv run pytest
pnpm lint && pnpm typecheck && pnpm test
```

After changing API routes or schemas, regenerate the typed client with `pnpm --filter @multigent/web gen:api`.

Backend tests use the compose Postgres (`multigent_test` database) and Redis (db 15).

## Layout

```
apps/api   FastAPI API + worker (same codebase, two entrypoints)
apps/web   Next.js frontend
docs/      ARCHITECTURE.md (authoritative design)
```
