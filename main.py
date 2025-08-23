# main.py
import os
import asyncio
import logging

import discord
from discord.ext import commands

from weather_store import WxStore

TOKEN = os.getenv("DISCORD_TOKEN")
APP_ID = os.getenv("DISCORD_APP_ID")  # optional; if set we'll pass to the Bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

intents = discord.Intents.default()  # no message content needed for slash commands
bot_kwargs = {"intents": intents}
if APP_ID:
    # lets discord.py know the application id at construction time
    bot_kwargs["application_id"] = int(APP_ID)

bot = commands.Bot(command_prefix="!", **bot_kwargs)

# attach store so the weather cog can find it
bot.store = WxStore(os.getenv("WXBOT_DB_PATH", "data/wxbot.sqlite3"))

# prevent double-sync on reconnects
bot._did_sync = False

@bot.event
async def on_ready():
    logging.info("Logged in as %s (%s). In %d guild(s).", bot.user, bot.user.id, len(bot.guilds))
    if not bot._did_sync:
        try:
            # sync after login so application_id is guaranteed to exist
            await bot.tree.sync()
            bot._did_sync = True
            logging.info("Slash commands synced.")
        except Exception as e:
            logging.exception("Slash command sync failed: %s", e)

async def load_ext():
    # load the weather cog (async setup() inside weather.py)
    await bot.load_extension("weather")

async def main():
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in environment.")
    async with bot:
        await load_ext()
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        try:
            bot.store.close()
        except Exception:
            pass
