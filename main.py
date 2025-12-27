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
log = logging.getLogger("wxbot")

intents = discord.Intents.default()  # no message content needed for slash commands


async def main():
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in environment.")

    bot_kwargs = dict(intents=intents)
    if APP_ID:
        try:
            bot_kwargs["application_id"] = int(APP_ID)
        except ValueError:
            log.warning("DISCORD_APP_ID is set but not an int; ignoring.")

    bot = commands.Bot(command_prefix="!", **bot_kwargs)

    # Attach store to bot so cogs can use it
    bot.store = WxStore(os.getenv("WXBOT_DB_PATH", "data/wxbot.sqlite3"))

    @bot.event
    async def on_ready():
        log.info("Logged in as %s (%s)", bot.user, bot.user.id)

    async def load_ext():
        await bot.load_extension("weather")
        try:
            synced = await bot.tree.sync()
            log.info("Synced %d app commands globally.", len(synced))
        except Exception:
            log.exception("Failed to sync app commands.")

    async with bot:
        await load_ext()
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
