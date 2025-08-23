# Standalone Discord Weather Bot (Docker)

This is a minimal, production-ready Discord bot that runs in Docker and provides weather commands, daily/weekly DM subscriptions, and NWS alert DMs.

It **reuses your existing `weather.py` cog** and wires in a tiny SQLite-backed store so it works standalone.

---

## Quick Start

1. **Create a Discord application & bot** at <https://discord.com/developers/applications>  
   - Add a Bot, copy its token
   - Scopes when inviting: `bot` and `applications.commands`
   - Bot permissions: sending messages + embed links are sufficient

2. **Configure env**  
   Copy `.env.example` to `.env` and set your token:
   ```env
   DISCORD_TOKEN=your_bot_token_here
   ```

3. **Build & run with Compose**
   ```bash
   docker compose up -d --build
   ```

4. **Invite the bot** to your server using the OAuth2 URL from the Developer Portal (Scopes: `bot`, `applications.commands`).

Data is stored in `./data/wxbot.sqlite3` on the host.

---

## Commands (slash)

- `/weather [zip]` – current weather + today's details (uses saved ZIP if omitted)
- `/weather_set_zip <zip>` – save your default ZIP
- `/weather_subscribe time:<HH:MM or 7:30pm> cadence:<daily|weekly> [zip] [weekly_days:3..10]` – schedule DMs in **Chicago time**
- `/weather_subscriptions` – list IDs and next run
- `/weather_unsubscribe <id>` – remove a subscription you own
- `/wx_alerts <on|off> [zip] [min_severity: advisory|watch|warning]` – enable/disable NWS alerts

### Notes
- Schedules are in **America/Chicago**; DMs are sent around your chosen minute.
- APIs used: Zippopotam (ZIP → coords), Open‑Meteo (forecast), and NWS (alerts).

---

## Project Layout

```text
wxbot/
├─ Dockerfile
├─ docker-compose.yml
├─ requirements.txt
├─ main.py             # bot entrypoint (loads weather cog and syncs commands)
├─ weather.py          # your weather cog (unchanged)
├─ weather_store.py    # tiny SQLite store
├─ .env.example
└─ data/               # persisted by docker-compose (created at runtime)
```

---

## Local (non-Docker) Run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export DISCORD_TOKEN=xxxxx
python main.py
```

---

## Troubleshooting

- **Commands not appearing**: global slash commands can take some time to show up the first time after sync. You can also invite the bot to a test guild and use a guild-specific sync if you prefer.
- **Time zone**: The cog schedules in America/Chicago. That is intentional to mirror original behavior.
- **Persistence**: Ensure the `./data` folder is writable on the host.
