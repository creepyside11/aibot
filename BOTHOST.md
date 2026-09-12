# Bothost: PostgreSQL + Google login from phone

This repository includes a custom Dockerfile for Bothost.

## Environment variables

Required:

- `BOT_TOKEN` — Telegram bot token. Bothost can provide this automatically.
- `DATABASE_URL` — shared PostgreSQL connection string. It may be the same database used by Emerald.

Recommended:

- `ADMIN_ID` — Telegram user id that can open `/admin`.
- `BROWSER_PASSWORD` — password for the remote Chrome/noVNC page.
- `AIBOT_DB_SCHEMA=aibot` — PostgreSQL schema used by this bot. Keep the default when sharing a database with Emerald.
- `TEACHER=web`
- `GEMINI_WEB_HEADLESS=false`

`DATA_DIR` does not need to be set on Bothost. The Docker image uses `/app/data`, which is the persistent directory.

## Deploy

1. Create/update the bot from this Git repository.
2. Choose the repository Dockerfile / Docker stack.
3. Add `DATABASE_URL` and, preferably, `BROWSER_PASSWORD` to environment variables.
4. Enable a domain for the bot. The container listens on Bothost's `PORT` automatically.
5. Deploy.

## Sign in to Google from a phone

Open:

`https://YOUR_BOTHOST_DOMAIN/vnc.html?autoconnect=true&resize=remote`

Enter `BROWSER_PASSWORD`. You will see the real Chrome instance running in the bot container. Sign in to Google and open Gemini normally.

The Chrome profile is stored in `/app/data/gemini_profile`, so it survives bot restarts and redeploys. You do not need SSH or a terminal and you do not need to copy Google cookies manually.

If `BROWSER_PASSWORD` is not set, the container generates one, stores it in `/app/data/.browser_password`, and prints it in the Bothost logs.

## Shared database

When `DATABASE_URL` is set, the bot uses PostgreSQL instead of SQLite. It creates tables inside the `aibot` schema by default, so generic table names such as `users`, `messages`, and `settings` do not collide with Emerald tables in the same database.

For local development without `DATABASE_URL`, the old SQLite fallback remains available at `data/brain.db`.
