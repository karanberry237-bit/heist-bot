# Heist-style Discord bot

Slash-command, user-installable economy bot (discord.py + Postgres).

## Discord Developer Portal
1. Create an application, add a Bot, copy the token.
2. **Installation** tab: enable both **User Install** and **Guild Install**.
   Guild Install scope: `applications.commands` and `bot`.
   User Install scope: `applications.commands`.
3. Use the Install Link to add it to your account / a server.
No privileged intents are needed.

## Deploy on Railway
1. Push this folder to a GitHub repo.
2. New Project -> Deploy from GitHub repo (Railway detects the Dockerfile).
3. Add a **PostgreSQL** database to the project.
4. On the bot service, add variables:
   - `DISCORD_TOKEN` = your token
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
5. Deploy. Tables are created automatically on first start.
6. After the first successful start, set `SYNC_ON_START=false`
   (re-sync only when you add or change commands).

Global commands can take up to an hour to appear the first time.

## Run locally
    pip install -r requirements.txt
    cp .env.example .env   # fill in, then export the variables
    python bot.py

## Commands (phase 1)
`/balance` `/daily` `/work` `/pay` `/leaderboard`

## Adding features
Create `cogs/yourcog.py` with an `async def setup(bot)` and add it to
`EXTENSIONS` in `bot.py`. Put `@everywhere` under `@app_commands.command()`
so the command works in DMs and user installs. Use `bot.db.change_balance()`
for all bets and payouts; it can't go below zero.
