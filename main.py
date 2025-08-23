import os
import asyncio
import logging

import discord
from discord.ext import commands

from weather_store import WxStore

TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

intents = discord.Intents.default()  # slash commands don't need message content
bot = commands.Bot(command_prefix="!", intents=intents)

# attach store so the weather cog can find it
bot.store = WxStore(os.getenv("WXBOT_DB_PATH", "data/wxbot.sqlite3"))

@bot.event
async def on_ready():
    logging.info("Logged in as %s (%s). In %d guild(s).", bot.user, bot.user.id, len(bot.guilds))

async def load_ext():
    # load the weather cog (async setup() inside weather.py)
    await bot.load_extension("weather")
    # sync commands globally
    await bot.tree.sync()
    logging.info("Slash commands synced.")

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
