# OpenSEO

A self-hosted alternative to Semrush/Ahrefs: keyword research, rank tracking, competitor and backlink data, site audits (up to 10,000 pages) and "AI visibility". It includes an MCP server so agents can run SEO research.

- Source: `repos/open-seo` (https://github.com/every-app/open-seo, MIT). Docs: `repos/open-seo/docs/`
- Docker images: `images/open-seo-images.tar` (app, storage, nginx; pinned to the exact versions the compose file names). The launcher loads it automatically if the images are missing, so no internet is needed to start it.

**It is not free to use.** The data comes from **DataForSEO**, which you pay directly per request (no OpenSEO markup). The app runs without a key, but keyword, backlink and rank features return nothing until you add one.

## Start

```bash
openseo up
```

The first run creates `repos/open-seo/deploy/standalone/.env` with a random storage secret, then starts the containers and follows the setup log. Wait for `OpenSEO initialized and ready.` (Ctrl+C stops following the log; the app keeps running). Then:

1. Open http://localhost:3001 and **create your admin account straight away**. The first visitor becomes admin, and sign-up closes after that.
2. Put your keys in `repos/open-seo/deploy/standalone/.env`:

| Key | Needed for | Get it |
|---|---|---|
| `DATAFORSEO_API_KEY` | keywords, backlinks, ranks, SERP data (the core) | dataforseo.com → API access. The value is **base64 of `login:password`**: `printf 'login:password' \| base64`. See `docs/DATAFORSEO_API_KEY.md` |
| `OPENROUTER_API_KEY` | "SAM" chat assistant, site research, summaries | openrouter.ai |
| `GOOGLE_CLIENT_ID` / `_SECRET` | optional Search Console + Analytics linking | see `docs/SELF_HOSTING_GOOGLE_SEARCH_CONSOLE.md` |
| `CONTEXT_API_KEY` | optional JavaScript rendering in site audits | |

3. Apply key changes: `openseo up -d --force-recreate init web crawl-gateway`

## Everyday commands

```bash
openseo up                 # start (and follow the init log)
openseo down               # stop
openseo status             # what's running
openseo logs web           # logs for one service
openseo backup ./backups/$(date +%F)   # snapshot all data (do this before updates)
```

Anything else goes straight to `docker compose`, e.g. `openseo restart web`.

## Power-user

- **MCP**: connect agents to `http://localhost:3001/mcp`. Create an API key under Settings in the app and use it as the bearer token. In OpenCode: add `"openseo": {"type": "remote", "url": "http://localhost:3001/mcp", "headers": {"Authorization": "Bearer <key>"}}` under `mcp` in `config/opencode/opencode.json`.
- **Agent skills** for Claude Code / Codex / Cursor ship in the repo (see its README).
- **No login on a private network**: `AUTH_MODE=local_noauth` in `.env`. Only use it behind Tailscale or a LAN you control.
- **Teammates**: Team settings → copy an invite link and send it yourself (there is no email sending).
- Reset a password from a terminal: `cd repos/open-seo && docker compose -f deploy/standalone/compose.yaml exec -T runtime node /opt/standalone/admin.mjs reset-password you@example.com`, then type the new password.

## Moving it to another machine

The **app** travels with this folder (repo + image tar). Your **OpenSEO data** lives in Docker volumes, not in the folder. To move it:

```bash
openseo backup ~/local-ai/backups/move        # on the old machine
# copy local-ai over, then on the new machine:
openseo restore ~/local-ai/backups/move       # needs Node; restores into an empty install
openseo up
```

Backups contain your credentials and data. Keep them private. Copy `deploy/standalone/.env` too; restore needs the same storage credentials.

## Updating

```bash
openseo backup ./backups/before-update
git -C repos/open-seo pull
openseo pull && openseo up
openseo save-images        # refresh the offline copy in images/
```
