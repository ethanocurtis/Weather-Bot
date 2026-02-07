
import os
import re
import html
import json
import aiohttp
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

from astral import moon

import discord
from discord.ext import tasks, commands
from discord import app_commands

# ---- Constants & styling helpers ----
DEFAULT_TZ_NAME = "America/Chicago"
HTTP_HEADERS = {
    "User-Agent": "UtilaBot/1.0 (+https://github.com/ethanocurtis/Utilabot)",
    "Accept": "application/json",
}
# ---- Feedback routing (set via env) ----
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or 0)  # your Discord user id
FEEDBACK_CHANNEL_ID = int(os.getenv("FEEDBACK_CHANNEL_ID", "0") or 0)  # optional: send feedback to this channel id



WX_CODE_MAP = {
    0: ("\u2600\ufe0f", "Clear sky"),
    1: ("\U0001F324\ufe0f", "Mainly clear"),
    2: ("\u26C5", "Partly cloudy"),
    3: ("\u2601\ufe0f", "Overcast"),
    45: ("\U0001F32B\ufe0f", "Fog"),
    48: ("\U0001F32B\ufe0f", "Depositing rime fog"),
    51: ("\U0001F326\ufe0f", "Light drizzle"),
    53: ("\U0001F326\ufe0f", "Drizzle"),
    55: ("\U0001F327\ufe0f", "Heavy drizzle"),
    56: ("\U0001F327\ufe0f", "Freezing drizzle"),
    57: ("\U0001F327\ufe0f", "Heavy freezing drizzle"),
    61: ("\U0001F326\ufe0f", "Light rain"),
    63: ("\U0001F327\ufe0f", "Rain"),
    65: ("\U0001F327\ufe0f", "Heavy rain"),
    66: ("\U0001F328\ufe0f", "Freezing rain"),
    67: ("\U0001F328\ufe0f", "Heavy freezing rain"),
    71: ("\U0001F328\ufe0f", "Light snow"),
    73: ("\U0001F328\ufe0f", "Snow"),
    75: ("\u2744\ufe0f", "Heavy snow"),
    77: ("\u2744\ufe0f", "Snow grains"),
    80: ("\U0001F327\ufe0f", "Rain showers"),
    81: ("\U0001F327\ufe0f", "Heavy rain showers"),
    82: ("\u26C8\ufe0f", "Violent rain showers"),
    85: ("\U0001F328\ufe0f", "Snow showers"),
    86: ("\u2744\ufe0f", "Heavy snow showers"),
    95: ("\u26C8\ufe0f", "Thunderstorm"),
    96: ("\u26C8\ufe0f", "Thunderstorm with hail"),
    99: ("\u26C8\ufe0f", "Severe thunderstorm with hail"),
}


# ---- Moon phase helpers (Astral) ----
# Astral's moon.phase() returns a number on ~0..28 scale for the given date.
# We'll map that to 8 familiar phases for display.
_MOON_PHASES_8 = [
    ("New Moon", "🌑"),
    ("Waxing Crescent", "🌒"),
    ("First Quarter", "🌓"),
    ("Waxing Gibbous", "🌔"),
    ("Full Moon", "🌕"),
    ("Waning Gibbous", "🌖"),
    ("Last Quarter", "🌗"),
    ("Waning Crescent", "🌘"),
]

def moon_phase_info_for_date(d: datetime) -> Tuple[str, str, float]:
    """Return (name, emoji, age_days) for the date in d (local date is used)."""
    # Use local date component
    date = d.date()
    p = float(moon.phase(date))  # 0..~28
    idx = int((p / 28.0) * 8 + 0.5) % 8
    name, emoji = _MOON_PHASES_8[idx]
    age_days = round(p, 1)
    return name, emoji, age_days
def wx_icon_desc(code: int):
    icon, desc = WX_CODE_MAP.get(int(code), ("\U0001F321\ufe0f", "Weather"))
    return icon, desc

def wx_color_from_temp_f(temp_f: float):
    if temp_f is None:
        return discord.Colour.blurple()
    t = float(temp_f)
    if t <= 32:   return discord.Colour.from_rgb(80, 150, 255)
    if t <= 45:   return discord.Colour.from_rgb(100, 180, 255)
    if t <= 60:   return discord.Colour.from_rgb(120, 200, 200)
    if t <= 75:   return discord.Colour.from_rgb(255, 205, 120)
    if t <= 85:   return discord.Colour.from_rgb(255, 160, 80)
    if t <= 95:   return discord.Colour.from_rgb(255, 120, 80)
    return discord.Colour.from_rgb(230, 60, 60)

def fmt_sun(dt_str: str):
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%I:%M %p")
    except Exception:
        try:
            return f"{dt_str[11:13]}:{dt_str[14:16]}"
        except Exception:
            return dt_str

# ---- Time helpers (Chicago) ----
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

def _chicago_tz_for(dt_naive: datetime):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(DEFAULT_TZ_NAME)
        except Exception:
            pass
    # Fallback manual DST calc
    y = dt_naive.year
    march8 = datetime(y, 3, 8)
    second_sun_march = march8 + timedelta(days=(6 - march8.weekday()) % 7)
    nov1 = datetime(y, 11, 1)
    first_sun_nov = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    is_dst = second_sun_march <= dt_naive < first_sun_nov
    return timezone(timedelta(hours=-5 if is_dst else -6))

def _parse_time(time_str: str):
    t = time_str.strip().lower().replace(" ", "")
    m = re.match(r"^(\d{1,2}):(\d{2})(am|pm)?$", t) or re.match(r"^(\d{2})(\d{2})(am|pm)?$", t)
    if not m:
        raise ValueError("Time must be HH:MM (24h), HHMM, or h:mma/pm.")
    hh, mi, ampm = m.groups()
    hh, mi = int(hh), int(mi)
    if ampm:
        hh = (hh % 12) + (12 if ampm == "pm" else 0)
    if not (0 <= hh <= 23 and 0 <= mi <= 59):
        raise ValueError("Invalid time.")
    return hh, mi

def _next_local_run(now_local: datetime, hh: int, mi: int, cadence: str) -> datetime:
    target = now_local.replace(hour=hh, minute=mi, second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1 if cadence == "daily" else 7)
    return target

def _fmt_local(dt_utc: datetime):
    return dt_utc.astimezone(_chicago_tz_for(datetime.now())).strftime("%m-%d-%Y %H:%M %Z")

async def _zip_to_place_and_coords(session: aiohttp.ClientSession, zip_code: str):
    async with session.get(f"https://api.zippopotam.us/us/{zip_code}", timeout=aiohttp.ClientTimeout(total=12)) as r:
        if r.status != 200:
            raise RuntimeError("Invalid ZIP or lookup failed.")
        zp = await r.json()
    place = zp["places"][0]
    city = place["place name"]; state = place["state abbreviation"]
    lat = float(place["latitude"]); lon = float(place["longitude"])
    return city, state, lat, lon

async def _fetch_outlook(session: aiohttp.ClientSession, lat: float, lon: float, days: int, tz_name: str = "auto"):
    params = {
        "latitude": lat, "longitude": lon,
        "timezone": tz_name,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,sunrise,sunset,uv_index_max",
    }
    async with session.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            raise RuntimeError("Weather API unavailable.")
        data = await r.json()
    daily = data.get("daily") or {}
    out = []
    dates = (daily.get("time") or [])[:days]
    tmax = (daily.get("temperature_2m_max") or [])[:days]
    tmin = (daily.get("temperature_2m_min") or [])[:days]
    prec = (daily.get("precipitation_sum") or [])[:days]
    pop  = (daily.get("precipitation_probability_max") or [])[:days]
    wmax = (daily.get("wind_speed_10m_max") or [])[:days]
    codes = (daily.get("weather_code") or [])[:days]
    rises = (daily.get("sunrise") or [])[:days]
    sets  = (daily.get("sunset") or [])[:days]
    uvs   = (daily.get("uv_index_max") or [])[:days]

    for i, d in enumerate(dates):
        hi = tmax[i] if i < len(tmax) else None
        lo = tmin[i] if i < len(tmin) else None
        pr = prec[i] if i < len(prec) else 0.0
        pp = pop[i] if i < len(pop) else None
        wm = wmax[i] if i < len(wmax) else None
        code = codes[i] if i < len(codes) else 0
        sunrise = rises[i] if i < len(rises) else None
        sunset = sets[i] if i < len(sets) else None
        uv = uvs[i] if i < len(uvs) else None
        icon, desc = wx_icon_desc(code)
        parts = []
        if hi is not None and lo is not None:
            parts.append(f"**{round(hi)}° / {round(lo)}°**")
        if wm is not None:
            parts.append(f"\U0001F4A8 {round(wm)} mph")
        if pp is not None:
            parts.append(f"\u2614 {int(pp)}%")
        parts.append(f"\U0001F4CF {pr:.2f} in")
        line = f"{icon} {desc} — " + " - ".join(parts)
        out.append((d, line, sunrise, sunset, uv, hi))
    return out

# ---- NWS alerts helpers ----
SEVERITY_ORDER = {"advisory": 0, "watch": 1, "warning": 2}
NWS_SEV_MAP = {"minor": 0, "moderate": 1, "severe": 2, "extreme": 2}

def _seen_key(uid: int, alert_id: str) -> str:
    return f"wx_seen:{int(uid)}:{alert_id}"

CADENCE_CHOICES = [
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="weekly (send on this weekday)", value="weekly"),
]

class Weather(commands.Cog):
    """All weather features: current, subscriptions, and alerts."""
    def __init__(self, bot: commands.Bot, store=None):
        self.bot = bot
        # Try to discover the Store from bot or import-time fallback
        self.store = store or getattr(bot, "store", None)
        if self.store is None:
            try:
                import bot as _bot_main
                self.store = getattr(_bot_main, "store", None)
            except Exception:
                self.store = None
        # Feedback anti-spam (user_id -> last_ts)
        self._feedback_last = {}

        # Background loops
        self.weather_scheduler.start()
        self.wx_alerts_scheduler.start()

    def cog_unload(self):
        self.weather_scheduler.cancel()
        self.wx_alerts_scheduler.cancel()

    # -------- Slash Commands --------


    async def _send_feedback(self, inter: discord.Interaction, kind: str, message: str) -> None:
        # simple cooldown: 1 per 60s per user
        now = asyncio.get_event_loop().time()
        last = self._feedback_last.get(inter.user.id, 0.0)
        if now - last < 60:
            raise RuntimeError("cooldown")
        self._feedback_last[inter.user.id] = now

        message = (message or "").strip()
        if not message:
            raise ValueError("empty")
        if len(message) > 1800:
            message = message[:1800] + "…"

        where = "DM" if inter.guild is None else f"{inter.guild.name} ({inter.guild.id})"

        embed = discord.Embed(
            title=f"Weather Bot {kind}",
            description=message,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="From", value=f"{inter.user} ({inter.user.id})", inline=False)
        embed.add_field(name="Context", value=where, inline=False)

        # Prefer a feedback channel; fallback to DMing the owner
        if FEEDBACK_CHANNEL_ID:
            ch = self.bot.get_channel(FEEDBACK_CHANNEL_ID)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(FEEDBACK_CHANNEL_ID)
                except Exception:
                    ch = None
            if ch is None:
                raise RuntimeError("bad_channel")
            await ch.send(embed=embed)
            return

        if not BOT_OWNER_ID:
            raise RuntimeError("no_owner")

        owner = self.bot.get_user(BOT_OWNER_ID)
        if owner is None:
            owner = await self.bot.fetch_user(BOT_OWNER_ID)
        await owner.send(embed=embed)

    @app_commands.command(name="feedback", description="Send feedback to the bot owner.")
    @app_commands.describe(message="What should I improve? Bug report or feature request.")
    async def feedback_cmd(self, inter: discord.Interaction, message: str):
        await inter.response.defer(ephemeral=True, thinking=False)
        try:
            await self._send_feedback(inter, "Feedback", message)
        except RuntimeError as e:
            if str(e) == "cooldown":
                return await inter.followup.send("You're sending feedback a bit fast—try again in a minute.", ephemeral=True)
            if str(e) == "no_owner":
                return await inter.followup.send("Feedback isn't configured (missing BOT_OWNER_ID).", ephemeral=True)
            if str(e) == "bad_channel":
                return await inter.followup.send("Feedback channel is misconfigured (FEEDBACK_CHANNEL_ID).", ephemeral=True)
            return await inter.followup.send("Couldn't deliver your feedback (DMs may be blocked).", ephemeral=True)
        except ValueError:
            return await inter.followup.send("Please include a message.", ephemeral=True)

        await inter.followup.send("✅ Thanks! Your feedback was sent.", ephemeral=True)

    @app_commands.command(name="bug", description="Report a bug to the bot owner.")
    @app_commands.describe(message="What happened? Include steps if possible.")
    async def bug_cmd(self, inter: discord.Interaction, message: str):
        await inter.response.defer(ephemeral=True, thinking=False)
        try:
            await self._send_feedback(inter, "Bug Report", message)
        except RuntimeError as e:
            if str(e) == "cooldown":
                return await inter.followup.send("You're sending reports a bit fast—try again in a minute.", ephemeral=True)
            if str(e) == "no_owner":
                return await inter.followup.send("Bug reporting isn't configured (missing BOT_OWNER_ID).", ephemeral=True)
            if str(e) == "bad_channel":
                return await inter.followup.send("Feedback channel is misconfigured (FEEDBACK_CHANNEL_ID).", ephemeral=True)
            return await inter.followup.send("Couldn't deliver your report (DMs may be blocked).", ephemeral=True)
        except ValueError:
            return await inter.followup.send("Please include a message.", ephemeral=True)

        await inter.followup.send("✅ Thanks! Your bug report was sent.", ephemeral=True)

    @app_commands.command(name="feature", description="Request a feature to the bot owner.")
    @app_commands.describe(message="What would you like added/changed?")
    async def feature_cmd(self, inter: discord.Interaction, message: str):
        await inter.response.defer(ephemeral=True, thinking=False)
        try:
            await self._send_feedback(inter, "Feature Request", message)
        except RuntimeError as e:
            if str(e) == "cooldown":
                return await inter.followup.send("You're sending requests a bit fast—try again in a minute.", ephemeral=True)
            if str(e) == "no_owner":
                return await inter.followup.send("Feature requests aren't configured (missing BOT_OWNER_ID).", ephemeral=True)
            if str(e) == "bad_channel":
                return await inter.followup.send("Feedback channel is misconfigured (FEEDBACK_CHANNEL_ID).", ephemeral=True)
            return await inter.followup.send("Couldn't deliver your request (DMs may be blocked).", ephemeral=True)
        except ValueError:
            return await inter.followup.send("Please include a message.", ephemeral=True)

        await inter.followup.send("✅ Thanks! Your feature request was sent.", ephemeral=True)

    
    @app_commands.command(name="moon", description="Show today's moon phase (uses your saved ZIP if you omit it).")
    @app_commands.describe(zip="Optional ZIP; uses your saved default if omitted")
    async def moon_cmd(self, inter: discord.Interaction, zip: Optional[str] = None):
        """Moon phase by date (and optionally by ZIP to show the location)."""
        if self.store is None:
            return await inter.response.send_message("Storage backend not available.", ephemeral=True)

        # Resolve ZIP (optional, just to show city/state like /weather does)
        z = None
        if zip and str(zip).strip():
            z = re.sub(r"[^0-9]", "", str(zip))
            if len(z) != 5:
                return await inter.response.send_message("Please give a valid 5‑digit US ZIP.", ephemeral=True)
        else:
            saved = self.store.get_user_zip(inter.user.id)
            if saved and len(str(saved)) == 5:
                z = str(saved)

        title_loc = ""
        if z:
            try:
                async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                    async with session.get(f"https://api.zippopotam.us/us/{z}", timeout=aiohttp.ClientTimeout(total=12)) as r:
                        if r.status == 200:
                            zp = await r.json()
                            place = zp["places"][0]
                            city = place["place name"]; state = place["state abbreviation"]
                            title_loc = f" — {city}, {state} {z}"
            except Exception:
                # If ZIP lookup fails, still show phase
                pass

        tz = _chicago_tz_for(datetime.now())
        now_local = datetime.now(tz)
        name, emoji, age = moon_phase_info_for_date(now_local)

        emb = discord.Embed(
            title=f"{emoji} Moon Phase{title_loc}",
            description=f"**{name}**",
            colour=discord.Colour.blurple()
        )
        emb.add_field(name="Moon age", value=f"{age} days", inline=True)
        emb.set_footer(text=f"Date: {now_local.strftime('%Y-%m-%d')} ({tz.key if hasattr(tz,'key') else 'local'})")
        await inter.response.send_message(embed=emb)

@app_commands.command(name="weather"
, description="Current weather by ZIP. Uses your saved ZIP if omitted.")
    @app_commands.describe(zip="Optional ZIP; uses your saved default if omitted")
    async def weather_cmd(self, inter: discord.Interaction, zip: Optional[str] = None):
        if self.store is None:
            return await inter.response.send_message("Storage backend not available.", ephemeral=True)
        await inter.response.defer()
        # Resolve ZIP
        if not zip or not str(zip).strip():
            saved = self.store.get_user_zip(inter.user.id)
            if not saved or len(str(saved)) != 5:
                return await inter.followup.send(
                    "You didn’t provide a ZIP and no default is saved. Set one with `/weather_set_zip 60601` or pass a ZIP.",
                    ephemeral=True
                )
            z = str(saved)
        else:
            z = re.sub(r"[^0-9]", "", str(zip))
            if len(z) != 5:
                return await inter.followup.send("Please give a valid 5‑digit US ZIP.", ephemeral=True)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                # ZIP -> coords
                async with session.get(f"https://api.zippopotam.us/us/{z}", timeout=aiohttp.ClientTimeout(total=12)) as r:
                    if r.status != 200:
                        return await inter.followup.send("Couldn't look up that ZIP.", ephemeral=True)
                    zp = await r.json()
                place = zp["places"][0]
                lat = float(place["latitude"]); lon = float(place["longitude"])
                city = place["place name"]; state = place["state abbreviation"]
                # Weather data
                params = {
                    "latitude": lat, "longitude": lon,
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "precipitation_unit": "inch",
                    "timezone": "auto",
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,precipitation,weather_code",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,uv_index_max,sunrise,sunset,wind_speed_10m_max",
                }
                async with session.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=aiohttp.ClientTimeout(total=15)) as r2:
                    if r2.status != 200:
                        return await inter.followup.send("Weather service is unavailable right now.", ephemeral=True)
                    wx = await r2.json()

            cur = wx.get("current") or wx.get("current_weather") or {}
            t = cur.get("temperature_2m") or cur.get("temperature")
            feels = cur.get("apparent_temperature", t)
            rh = cur.get("relative_humidity_2m")
            wind = cur.get("wind_speed_10m") or cur.get("windspeed")
            gust = cur.get("wind_gusts_10m")
            pcp = cur.get("precipitation", 0.0)
            code_now = cur.get("weather_code")
            daily = wx.get("daily") or {}
            icon, desc = wx_icon_desc((daily.get("weather_code") or [code_now or 0])[0])
            hi = (daily.get("temperature_2m_max") or [None])[0]
            lo = (daily.get("temperature_2m_min") or [None])[0]
            prcp_sum = (daily.get("precipitation_sum") or [0.0])[0]
            prcp_prob = (daily.get("precipitation_probability_max") or [None])[0]
            uv = (daily.get("uv_index_max") or [None])[0]
            sunrise = (daily.get("sunrise") or [None])[0]
            sunset = (daily.get("sunset") or [None])[0]
            wind_max = (daily.get("wind_speed_10m_max") or [None])[0]

            emb = discord.Embed(
                title=f"{icon} Weather — {city}, {state} {z}",
                description=f"**{desc}**",
                colour=wx_color_from_temp_f(t if t is not None else (hi if hi is not None else 70))
            )
            if t is not None:
                emb.add_field(name="Now", value=f"**{round(t)}°F** (feels {round(feels)}°)", inline=True)
            if hi is not None and lo is not None:
                emb.add_field(name="Today", value=f"High **{round(hi)}°** / Low **{round(lo)}°**", inline=True)
            if rh is not None:
                emb.add_field(name="Humidity", value=f"{int(rh)}%", inline=True)
            if wind is not None:
                wind_txt = f"{round(wind)} mph"
                if gust is not None:
                    wind_txt += f" (gusts {round(gust)} mph)"
                emb.add_field(name="Wind", value=wind_txt, inline=True)
            emb.add_field(name="Precip (now)", value=f"{pcp:.2f} in", inline=True)
            if prcp_prob is not None:
                emb.add_field(name="Precip Chance", value=f"{int(prcp_prob)}%", inline=True)
            if wind_max is not None:
                emb.add_field(name="Max Wind Today", value=f"{round(wind_max)} mph", inline=True)
            if uv is not None:
                emb.add_field(name="UV Index (max)", value=str(round(uv, 1)), inline=True)
            if sunrise:
                emb.add_field(name="Sunrise", value=fmt_sun(sunrise), inline=True)
            if sunset:
                emb.add_field(name="Sunset", value=fmt_sun(sunset), inline=True)
            await inter.followup.send(embed=emb)
        except Exception as e:
            await inter.followup.send(f"\u26A0\ufe0f Weather error: {e}", ephemeral=True)

    @app_commands.command(name="weather_set_zip", description="Set your default ZIP code for weather features.")
    async def weather_set_zip(self, inter: discord.Interaction, zip: app_commands.Range[str, 5, 10]):
        if self.store is None:
            return await inter.response.send_message("Storage backend not available.", ephemeral=True)
        z = re.sub(r"[^0-9]", "", zip)
        if len(z) != 5:
            return await inter.response.send_message("Please provide a valid 5‑digit US ZIP.", ephemeral=True)
        self.store.set_user_zip(inter.user.id, z)
        await inter.response.send_message(f"\u2705 Saved default ZIP: **{z}**", ephemeral=True)

    @app_commands.command(name="weather_subscribe", description="Subscribe to a daily or weekly weather DM at a Chicago-time hour.")
    @app_commands.describe(
        time="HH:MM (24h), HHMM, or h:mma/pm in Chicago time",
        cadence="daily or weekly",
        zip="Optional ZIP; uses your saved ZIP if omitted",
        weekly_days="For weekly: number of days to include (3, 7, or 10)"
    )
    @app_commands.choices(cadence=CADENCE_CHOICES)
    async def weather_subscribe(
        self,
        inter: discord.Interaction,
        time: str,
        cadence: app_commands.Choice[str],
        zip: Optional[app_commands.Range[str, 5, 10]] = None,
        weekly_days: Optional[app_commands.Range[int, 3, 10]] = 7
    ):
        if self.store is None:
            return await inter.response.send_message("Storage backend not available.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            hh, mi = _parse_time(time)
            z = re.sub(r"[^0-9]", "", zip) if zip else (self.store.get_user_zip(inter.user.id) or "")
            if len(z) != 5:
                return await inter.followup.send("Set a ZIP with `/weather_set_zip` or provide it here.", ephemeral=True)
            now_local = datetime.now(_chicago_tz_for(datetime.now()))
            first_local = _next_local_run(now_local, hh, mi, cadence.value)
            next_run_utc = first_local.astimezone(timezone.utc)
            sub = {
                "user_id": inter.user.id,
                "zip": z,
                "cadence": cadence.value,
                "hh": int(hh),
                "mi": int(mi),
                "weekly_days": int(weekly_days or 7),
                "next_run_utc": next_run_utc.isoformat(),
            }
            sid = self.store.add_weather_sub(sub)
            await inter.followup.send(
                f"\U0001F324\ufe0f Subscribed **#{sid}** — {cadence.value} at **{first_local.strftime('%I:%M %p %Z')}** for ZIP **{z}**.\n"
                + ("Weekly outlook length: **{} days**.".format(sub['weekly_days']) if cadence.value == "weekly" else "Daily: Today & Tomorrow."),
                ephemeral=True
            )
        except Exception as e:
            await inter.followup.send(f"\u26A0\ufe0f {type(e).__name__}: {e}", ephemeral=True)

    @app_commands.command(name="weather_subscriptions", description="List your weather subscriptions and next send time.")
    async def weather_subscriptions(self, inter: discord.Interaction):
        if self.store is None:
            return await inter.response.send_message("Storage backend not available.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        items = self.store.list_weather_subs(inter.user.id)
        if not items:
            return await inter.followup.send("You have no weather subscriptions.", ephemeral=True)

        out_lines = []
        tz = _chicago_tz_for(datetime.now())
        now_local = datetime.now(tz)

        for s in items:
            hh = int(s.get("hh", 8))
            mi = int(s.get("mi", 0))
            cadence = s.get("cadence", "daily") if s.get("cadence") in {"daily", "weekly"} else "daily"

            raw = s.get("next_run_utc")
            nxt = None
            needs = False
            if not raw or str(raw).strip().lower() == "none":
                needs = True
            else:
                try:
                    nxt = datetime.fromisoformat(str(raw)).replace(tzinfo=timezone.utc)
                except Exception:
                    needs = True

            if not needs and nxt is not None and nxt <= datetime.now(timezone.utc):
                needs = True

            if needs:
                first_local = _next_local_run(now_local, hh, mi, cadence)
                nxt = first_local.astimezone(timezone.utc)
                self.store.update_weather_sub(s["id"], user_id=int(s["user_id"]), next_run_utc=nxt.isoformat())

            out_lines.append(
                f"**#{s['id']}** — {cadence} at {hh:02d}:{mi:02d} CT - ZIP {s.get('zip','?????')} - next: {_fmt_local(nxt)}"
            )

        await inter.followup.send("\n".join(out_lines), ephemeral=True)

    @app_commands.command(name="weather_unsubscribe", description="Unsubscribe from weather DMs by ID.")
    async def weather_unsubscribe(self, inter: discord.Interaction, sub_id: int):
        if self.store is None:
            return await inter.response.send_message("Storage backend not available.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        ok = self.store.remove_weather_sub(sub_id, requester_id=inter.user.id)
        await inter.followup.send("Removed." if ok else "Couldn't remove that ID.", ephemeral=True)

    @app_commands.command(name="wx_alerts", description="Enable/disable severe weather alerts via DM (NWS).")
    @app_commands.describe(
        mode="on or off",
        zip="Optional ZIP (defaults to your saved ZIP)",
        min_severity="advisory | watch | warning (default: watch)"
    )
    async def wx_alerts(self, inter: discord.Interaction,
                        mode: str,
                        zip: Optional[str] = None,
                        min_severity: Optional[str] = "watch"):
        if self.store is None:
            return await inter.response.send_message("Storage backend not available.", ephemeral=True)
        mode = (mode or "").strip().lower()
        if mode not in ("on", "off"):
            return await inter.response.send_message("Use **on** or **off**.", ephemeral=True)
        if mode == "off":
            self.store.set_note(inter.user.id, "wx_alerts_enabled", "0")
            return await inter.response.send_message("\U0001F515 Severe weather alerts disabled.", ephemeral=True)

        z = re.sub(r"[^0-9]", "", zip) if zip else (self.store.get_user_zip(inter.user.id) or "")
        if len(z) != 5:
            return await inter.response.send_message("Set a ZIP with `/weather_set_zip` or provide it here.", ephemeral=True)

        sev = (min_severity or "watch").strip().lower()
        if sev not in ("advisory", "watch", "warning"):
            sev = "watch"

        self.store.set_note(inter.user.id, "wx_alerts_enabled", "1")
        self.store.set_note(inter.user.id, "wx_alerts_zip", z)
        self.store.set_note(inter.user.id, "wx_alerts_min_sev", sev)
        await inter.response.send_message(f"\U0001F514 Alerts **ON** for **{z}** (min severity: **{sev}**).", ephemeral=True)

    # -------- Schedulers --------
    @tasks.loop(seconds=60)
    async def weather_scheduler(self):
        if self.store is None:
            return
        try:
            now_utc = datetime.now(timezone.utc)
            subs = self.store.list_weather_subs(None)
            if not subs:
                return
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                for s in subs:
                    due = datetime.fromisoformat(s["next_run_utc"]).replace(tzinfo=timezone.utc)
                    if due <= now_utc:
                        try:
                            user = await self.bot.fetch_user(int(s["user_id"]))
                            city, state, lat, lon = await _zip_to_place_and_coords(session, s["zip"])
                            if s["cadence"] == "daily":
                                outlook = await _fetch_outlook(session, lat, lon, days=2)
                                first_hi = outlook[0][5] if outlook and outlook[0][5] is not None else None
                                emb = discord.Embed(
                                    title=f"\U0001F324\ufe0f Daily Outlook — {city}, {state} {s['zip']}",
                                    colour=wx_color_from_temp_f(first_hi if first_hi is not None else 70)
                                )
                                for (d, line, sunrise, sunset, uv, _hi) in outlook:
                                    extras = []
                                    if sunrise: extras.append(f"\U0001F305 {fmt_sun(sunrise)}")
                                    if sunset: extras.append(f"\U0001F307 {fmt_sun(sunset)}")
                                    if uv is not None: extras.append(f"\U0001F506 UV {round(uv,1)}")
                                    value = "\n".join([line, " - ".join(extras)]) if extras else line
                                    emb.add_field(name=d, value=value, inline=False)
                                emb.set_footer(text="Chicago time schedule")
                                await user.send(embed=emb)
                                next_local = datetime.now(_chicago_tz_for(datetime.now()))
                                next_local = next_local.replace(hour=s["hh"], minute=s["mi"], second=0, microsecond=0)
                                if next_local <= datetime.now(_chicago_tz_for(datetime.now())):
                                    next_local += timedelta(days=1)
                                self.store.update_weather_sub(s["id"], user_id=int(s["user_id"]), next_run_utc=next_local.astimezone(timezone.utc).isoformat())
                            else:
                                days = int(s.get("weekly_days", 7))
                                days = 10 if days > 10 else (3 if days < 3 else days)
                                outlook = await _fetch_outlook(session, lat, lon, days=days)
                                first_hi = outlook[0][5] if outlook and outlook[0][5] is not None else None
                                emb = discord.Embed(
                                    title=f"\U0001F5D3\ufe0f Weekly Outlook ({days} days) — {city}, {state} {s['zip']}",
                                    colour=wx_color_from_temp_f(first_hi if first_hi is not None else 70)
                                )
                                for (d, line, _sunrise, _sunset, _uv, _hi) in outlook:
                                    emb.add_field(name=d, value=line, inline=False)
                                emb.set_footer(text="Chicago time schedule")
                                await user.send(embed=emb)
                                next_local = datetime.now(_chicago_tz_for(datetime.now()))
                                next_local = next_local.replace(hour=s["hh"], minute=s["mi"], second=0, microsecond=0)
                                if next_local <= datetime.now(_chicago_tz_for(datetime.now())):
                                    next_local += timedelta(days=7)
                                else:
                                    next_local += timedelta(days=7)
                                self.store.update_weather_sub(s["id"], user_id=int(s["user_id"]), next_run_utc=next_local.astimezone(timezone.utc).isoformat())
                        except Exception:
                            fallback = now_utc + timedelta(minutes=5)
                            self.store.update_weather_sub(s["id"], next_run_utc=fallback.isoformat())
        except Exception:
            pass

    @weather_scheduler.before_loop
    async def before_weather(self):
        await self.bot.wait_until_ready()

    async def _fetch_nws_alerts(self, session: aiohttp.ClientSession, lat: float, lon: float):
        url = "https://api.weather.gov/alerts/active"
        params = {"point": f"{lat},{lon}", "status": "actual", "message_type": "alert"}
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=12), headers=HTTP_HEADERS) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        except Exception:
            return []
        feats = data.get("features", []) or []
        out = []
        for f in feats:
            p = f.get("properties", {}) or {}
            out.append({
                "id": p.get("id") or f.get("id"),
                "event": p.get("event"),
                "headline": p.get("headline"),
                "severity": (p.get("severity") or "").lower(),
                "certainty": (p.get("certainty") or "").lower(),
                "urgency": (p.get("urgency") or "").lower(),
                "areas": p.get("areaDesc"),
                "starts": p.get("onset") or p.get("effective"),
                "ends": p.get("ends") or p.get("expires"),
                "instr": p.get("instruction"),
                "desc": p.get("description"),
                "sender": p.get("senderName"),
                "link":  p.get("uri"),
            })
        return out

    @tasks.loop(seconds=300)
    async def wx_alerts_scheduler(self):
        if self.store is None:
            return
        try:
            user_ids = set()
            try:
                for s in self.store.list_weather_subs(None):
                    user_ids.add(int(s.get("user_id")))
            except Exception:
                pass
            try:
                rows = self.store.db.execute("SELECT user_id FROM weather_zips").fetchall()
                user_ids |= {int(r[0]) for r in rows}
            except Exception:
                pass
            if not user_ids:
                return

            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                for uid in user_ids:
                    if self.store.get_note(uid, "wx_alerts_enabled") != "1":
                        continue
                    z = self.store.get_note(uid, "wx_alerts_zip") or (self.store.get_user_zip(uid) or "")
                    if len(z) != 5:
                        continue
                    try:
                        city, state, lat, lon = await _zip_to_place_and_coords(session, z)
                        alerts = await self._fetch_nws_alerts(session, lat, lon)
                        min_sev = self.store.get_note(uid, "wx_alerts_min_sev") or "watch"
                        min_rank = SEVERITY_ORDER.get(min_sev, 1)

                        fresh = []
                        for a in alerts:
                            rank = NWS_SEV_MAP.get(a.get("severity",""), 0)
                            if rank < min_rank:
                                continue
                            aid = a.get("id") or ""
                            if not aid:
                                continue
                            if self.store.get_note(uid, _seen_key(uid, aid)):
                                continue
                            fresh.append(a)

                        if not fresh:
                            continue

                        emb = discord.Embed(
                            title=f"\u26A0\ufe0f Weather Alerts — {city}, {state} {z}",
                            colour=discord.Colour.orange()
                        )
                        for a in fresh[:10]:
                            name = f"{a.get('event') or 'Alert'} ({(a.get('severity') or '').title()})"
                            when = ""
                            if a.get("starts"): when += f"Starts: {a['starts']}\n"
                            if a.get("ends"):   when += f"Ends: {a['ends']}\n"
                            body = (a.get("headline") or a.get("desc") or "Details unavailable").strip()
                            if len(body) > 400: body = body[:397] + "…"
                            tail = f"\n{when}Source: {a.get('sender') or 'NWS'}"
                            if a.get("link"): tail += f"\nMore: {a['link']}"
                            emb.add_field(name=name, value=f"{body}{tail}", inline=False)

                        user = await self.bot.fetch_user(uid)
                        await user.send(embed=emb)
                        # mark seen
                        for a in fresh:
                            aid = a.get("id")
                            if aid:
                                self.store.set_note(uid, _seen_key(uid, aid), "1")

                    except Exception:
                        continue
        except Exception:
            pass

    @wx_alerts_scheduler.before_loop
    async def before_alerts(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    # Try to pass a store if the bot has one attached
    store = getattr(bot, "store", None)
    await bot.add_cog(Weather(bot, store=store))


