
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
from location_service import search_locations
from weather_ui import HelpView, SubscriptionDraft, DestinationView, SubscriptionManageView, wizard_embed

# ---- Constants & styling helpers ----
DEFAULT_TZ_NAME = "America/Chicago"
HTTP_HEADERS = {
    "User-Agent": "UtilaBot/1.0 (+https://github.com/ethanocurtis/Utilabot)",
    "Accept": "application/json",
}
# ---- Feedback routing (set via env) ----
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or 0)  # your Discord user id
FEEDBACK_CHANNEL_ID = int(os.getenv("FEEDBACK_CHANNEL_ID", "0") or 0)  # optional: send feedback to this channel id
BOT_VERSION = "2.0.1"



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

# ---- Time & user preference helpers ----
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

def _tzinfo_from_name(tz_name: str):
    """Best-effort tzinfo for an IANA tz name. Falls back to DEFAULT_TZ_NAME."""
    tz_name = (tz_name or "").strip() or DEFAULT_TZ_NAME
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            try:
                return ZoneInfo(DEFAULT_TZ_NAME)
            except Exception:
                pass
    # Fallback manual DST calc for America/Chicago only
    dt_naive = datetime.now()
    y = dt_naive.year
    march8 = datetime(y, 3, 8)
    second_sun_march = march8 + timedelta(days=(6 - march8.weekday()) % 7)
    nov1 = datetime(y, 11, 1)
    first_sun_nov = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    is_dst = second_sun_march <= dt_naive < first_sun_nov
    return timezone(timedelta(hours=-5 if is_dst else -6))

def _get_user_tz_name(store, user_id: int) -> str:
    if store is None:
        return DEFAULT_TZ_NAME
    tz = store.get_note(int(user_id), "wx_tz")
    return (tz or DEFAULT_TZ_NAME).strip() or DEFAULT_TZ_NAME

def _get_user_units(store, user_id: int) -> str:
    """Return 'standard' or 'metric'."""
    if store is None:
        return "standard"
    u = (store.get_note(int(user_id), "wx_units") or "standard").strip().lower()
    return u if u in {"standard", "metric"} else "standard"

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

def _fmt_local(dt_utc: datetime, tz_name: str):
    return dt_utc.astimezone(_tzinfo_from_name(tz_name)).strftime("%m-%d-%Y %H:%M %Z")

async def _zip_to_place_and_coords(session: aiohttp.ClientSession, zip_code: str):
    async with session.get(f"https://api.zippopotam.us/us/{zip_code}", timeout=aiohttp.ClientTimeout(total=12)) as r:
        if r.status != 200:
            raise RuntimeError("Invalid ZIP or lookup failed.")
        zp = await r.json()
    place = zp["places"][0]
    city = place["place name"]; state = place["state abbreviation"]
    lat = float(place["latitude"]); lon = float(place["longitude"])
    return city, state, lat, lon

async def _fetch_outlook(session: aiohttp.ClientSession, lat: float, lon: float, days: int, tz_name: str, units: str):
    units = (units or "standard").lower()
    temp_unit = "fahrenheit" if units == "standard" else "celsius"
    wind_unit = "mph" if units == "standard" else "kmh"
    precip_unit = "inch" if units == "standard" else "mm"
    params = {
        "latitude": lat, "longitude": lon,
        "timezone": tz_name,
        "temperature_unit": temp_unit,
        "wind_speed_unit": wind_unit,
        "precipitation_unit": precip_unit,
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
            parts.append(f"\U0001F4A8 {round(wm)} {wind_unit}")
        if pp is not None:
            parts.append(f"\u2614 {int(pp)}%")
        parts.append(f"\U0001F4CF {pr:.2f} {precip_unit}")
        line = f"{icon} {desc} — " + " - ".join(parts)
        out.append((d, line, sunrise, sunset, uv, hi, {"max_wind": wm, "max_temp": hi, "min_temp": lo, "rain_chance": pp, "precipitation": pr, "uv": uv}))
    return out


async def _fetch_hourly(session: aiohttp.ClientSession, lat: float, lon: float, tz_name: str, units: str, hours: int = 12):
    """Return a list of hourly forecast rows for the next N hours.

    Each item: (time_str, weather_code, temp, precip_prob, precip_amt, wind)
    time_str is in the requested timezone.
    """
    units = (units or "standard").lower()
    temp_unit = "fahrenheit" if units == "standard" else "celsius"
    wind_unit = "mph" if units == "standard" else "kmh"
    precip_unit = "inch" if units == "standard" else "mm"

    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": tz_name,
        "temperature_unit": temp_unit,
        "wind_speed_unit": wind_unit,
        "precipitation_unit": precip_unit,
        "hourly": "temperature_2m,weather_code,precipitation_probability,precipitation,wind_speed_10m",
    }
    async with session.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            raise RuntimeError("Weather API unavailable.")
        data = await r.json()

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    codes = hourly.get("weather_code") or []
    pops  = hourly.get("precipitation_probability") or []
    precs = hourly.get("precipitation") or []
    winds = hourly.get("wind_speed_10m") or []

    # Find the index closest to "now" in the requested timezone.
    tz = _tzinfo_from_name(tz_name)
    now_local = datetime.now(tz)

    start_idx = 0
    for i, ts in enumerate(times):
        try:
            # Open-Meteo returns local time strings when timezone is set.
            t_local = datetime.fromisoformat(ts)
            if t_local >= now_local.replace(tzinfo=None):
                start_idx = i
                break
        except Exception:
            continue

    end_idx = min(len(times), start_idx + max(1, int(hours)))
    out = []
    for i in range(start_idx, end_idx):
        out.append((
            times[i],
            int(codes[i]) if i < len(codes) else 0,
            temps[i] if i < len(temps) else None,
            pops[i] if i < len(pops) else None,
            precs[i] if i < len(precs) else None,
            winds[i] if i < len(winds) else None,
            wind_unit,
            precip_unit,
            "°F" if units == "standard" else "°C",
        ))
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

class RequestStatusView(discord.ui.View):
    def __init__(self, cog, request_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id

    async def _set(self, interaction: discord.Interaction, status: str):
        if not self.cog._is_staff(interaction.user):
            return await interaction.response.send_message("Only the configured bot owner can update requests.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.cog._update_request_status(self.request_id, status)
        try:
            req = self.cog.store.get_feedback_request(self.request_id)
            embed = self.cog._request_embed(req)
            await interaction.message.edit(embed=embed, view=RequestStatusView(self.cog, self.request_id))
        except Exception:
            pass
        await interaction.followup.send(f"Request #{self.request_id} marked **{status}**.", ephemeral=True)

    @discord.ui.button(label="Planned", style=discord.ButtonStyle.primary)
    async def planned(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._set(interaction, "planned")

    @discord.ui.button(label="In Progress", style=discord.ButtonStyle.secondary)
    async def progress(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._set(interaction, "in_progress")

    @discord.ui.button(label="Complete", style=discord.ButtonStyle.success)
    async def complete(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._set(interaction, "completed")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._set(interaction, "declined")


class StickyWeatherView(discord.ui.View):
    """Persistent controls attached to every sticky weather dashboard."""
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def _dashboard(self, interaction: discord.Interaction):
        row = self.cog.store.get_sticky_dashboard(message_id=interaction.message.id)
        if not row or not row.get("enabled", 1):
            await interaction.response.send_message("This weather dashboard is no longer active.", ephemeral=True)
            return None
        return row

    @discord.ui.button(label="Hourly", emoji="🕐", style=discord.ButtonStyle.primary, custom_id="wx:sticky:hourly")
    async def hourly(self, interaction: discord.Interaction, _button: discord.ui.Button):
        row = await self._dashboard(interaction)
        if not row: return
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                rows = await _fetch_hourly(session, row["latitude"], row["longitude"], row["timezone"], row["units"], 12)
            lines=[]
            for ts,code,temp,pop,_prec,wind,wu,_pu,deg in rows:
                icon,_=wx_icon_desc(code)
                lines.append(f"**{datetime.fromisoformat(ts).strftime('%I %p')}** {icon} {round(temp)}{deg} · rain {pop or 0}% · wind {round(wind or 0)} {wu}")
            await interaction.followup.send(embed=discord.Embed(title=f"Hourly — {row['location_name']}", description="\n".join(lines), colour=discord.Colour.blurple()), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Could not load the hourly forecast: {exc}", ephemeral=True)

    @discord.ui.button(label="7-Day", emoji="📅", style=discord.ButtonStyle.secondary, custom_id="wx:sticky:daily")
    async def daily(self, interaction: discord.Interaction, _button: discord.ui.Button):
        row = await self._dashboard(interaction)
        if not row: return
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                outlook = await _fetch_outlook(session, row["latitude"], row["longitude"], 7, row["timezone"], row["units"])
            sub={"cadence":"weekly","location_name":row["location_name"],"tz_name":row["timezone"],"units":row["units"]}
            await interaction.followup.send(embed=self.cog._outlook_embed(sub, outlook), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Could not load the 7-day forecast: {exc}", ephemeral=True)

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.success, custom_id="wx:sticky:refresh")
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button):
        row = await self._dashboard(interaction)
        if not row: return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cog.refresh_sticky_dashboard(row, interaction.message)
            await interaction.followup.send("✅ Dashboard refreshed.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Refresh failed: {exc}", ephemeral=True)


class OwnerAnalyticsView(discord.ui.View):
    def __init__(self, cog, owner_id: int):
        super().__init__(timeout=300)
        self.cog, self.owner_id = cog, int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not self.cog._is_staff(interaction.user):
            await interaction.response.send_message("This owner dashboard is private.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.cog.owner_analytics_embed(), view=self)

    @discord.ui.button(label="Pending Requests", emoji="💬", style=discord.ButtonStyle.secondary)
    async def requests(self, interaction: discord.Interaction, _button: discord.ui.Button):
        rows = self.cog.store.db.execute("SELECT id,request_type,status,message FROM feedback_requests WHERE status NOT IN ('completed','declined','duplicate','fixed') ORDER BY created_at DESC LIMIT 15").fetchall()
        text = "\n\n".join(f"**#{r[0]} · {str(r[1]).title()} · {str(r[2]).replace('_',' ').title()}**\n{str(r[3])[:160]}" for r in rows) or "No pending requests."
        await interaction.response.send_message(embed=discord.Embed(title="Pending Requests", description=text, colour=discord.Colour.blurple()), ephemeral=True)


class Weather(commands.Cog):
    """Weather, locations, subscriptions, alerts, and feedback tracking."""
    def __init__(self, bot: commands.Bot, store=None):
        self.bot = bot
        self.store = store or getattr(bot, "store", None)
        self._feedback_last = {}
        self.bot.add_view(StickyWeatherView(self))
        self.weather_scheduler.start()
        self.wx_alerts_scheduler.start()
        self.sticky_dashboard_scheduler.start()

    def cog_unload(self):
        self.weather_scheduler.cancel(); self.wx_alerts_scheduler.cancel(); self.sticky_dashboard_scheduler.cancel()

    def _is_staff(self, user) -> bool:
        return bool(BOT_OWNER_ID and int(user.id) == BOT_OWNER_ID)

    async def resolve_location_query(self, query: str) -> Dict[str, Any]:
        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
            results = await search_locations(session, query.strip(), 1)
        if not results:
            raise ValueError("No matching location was found.")
        return results[0]

    @staticmethod
    def parse_subscription_time(value: str) -> Tuple[int, int]:
        return _parse_time(value)

    @staticmethod
    def normalize_condition_metric(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        aliases = {
            "wind": "max_wind", "max_wind": "max_wind", "maximum_wind": "max_wind",
            "max_temp": "max_temp", "maximum_temperature": "max_temp", "high_temp": "max_temp",
            "min_temp": "min_temp", "minimum_temperature": "min_temp", "low_temp": "min_temp",
            "rain": "rain_chance", "rain_chance": "rain_chance", "chance_of_rain": "rain_chance",
            "precip": "precipitation", "precipitation": "precipitation",
            "uv": "uv", "uv_index": "uv",
        }
        metric = aliases.get(cleaned)
        if not metric:
            raise ValueError("Metric must be max wind, max temp, min temp, rain chance, precipitation, or UV.")
        return metric

    def create_subscription_from_draft(self, state: SubscriptionDraft):
        if not all([state.destination_type, state.location, state.cadence]) or state.hh is None or state.mi is None:
            raise ValueError("The subscription setup is incomplete.")
        loc = state.location
        tz_name = loc.get("timezone") or _get_user_tz_name(self.store, state.user_id)
        first = _next_local_run(datetime.now(_tzinfo_from_name(tz_name)), state.hh, state.mi, state.cadence)
        sub = {
            "user_id": state.user_id, "zip": "", "cadence": state.cadence, "hh": state.hh, "mi": state.mi,
            "weekly_days": state.weekly_days, "tz_name": tz_name, "units": _get_user_units(self.store, state.user_id),
            "next_run_utc": first.astimezone(timezone.utc).isoformat(), "location_id": loc.get("id"),
            "location_name": loc["display_name"], "latitude": loc["latitude"], "longitude": loc["longitude"],
            "country_code": loc.get("country_code"), "destination_type": state.destination_type,
            "guild_id": state.guild_id, "channel_id": state.channel_id, "created_by": state.user_id,
            "condition_metric": state.condition_metric, "condition_operator": state.condition_operator,
            "condition_value": state.condition_value, "condition_unit": _get_user_units(self.store, state.user_id), "enabled": 1,
        }
        return self.store.add_weather_sub(sub), first

    def get_manageable_subscription(self, sub_id: int, user, guild=None) -> Dict[str, Any]:
        row = self.store.get_weather_sub(sub_id)
        if not row:
            raise ValueError("Subscription not found.")
        is_owner = int(row["user_id"]) == int(user.id)
        is_guild_admin = bool(guild and user.guild_permissions.manage_guild and row.get("guild_id") == guild.id)
        if not (is_owner or is_guild_admin):
            raise ValueError("You cannot manage that subscription.")
        return row

    async def test_subscription(self, sub_id: int, user, guild=None) -> str:
        sub = self.get_manageable_subscription(sub_id, user, guild)
        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
            days = 2 if sub["cadence"] == "daily" else max(3, min(10, int(sub.get("weekly_days") or 7)))
            outlook = await _fetch_outlook(session, float(sub["latitude"]), float(sub["longitude"]), days, sub.get("tz_name") or DEFAULT_TZ_NAME, sub.get("units") or "standard")
        embed = self._outlook_embed(sub, outlook)
        embed.description = "**Test delivery** — this does not affect the next scheduled run."
        if sub.get("destination_type") == "channel":
            channel = self.bot.get_channel(int(sub["channel_id"])) or await self.bot.fetch_channel(int(sub["channel_id"]))
            await channel.send(embed=embed)
            return channel.mention
        target = self.bot.get_user(int(sub["user_id"])) or await self.bot.fetch_user(int(sub["user_id"]))
        await target.send(embed=embed)
        return "your DMs"

    async def _resolve_location(self, session, user_id: int, query: Optional[str] = None) -> Dict[str, Any]:
        if query and query.strip():
            return (await search_locations(session, query.strip(), 1))[0]
        saved = self.store.get_default_location(user_id)
        if saved:
            return saved
        legacy = self.store.get_user_zip(user_id)
        if legacy:
            loc = (await search_locations(session, f"{legacy}, United States", 1))[0]
            self.store.save_location(user_id, loc, "Default", True)
            return self.store.get_default_location(user_id)
        raise ValueError("No default location is saved. Use `/location_set` first or provide a location.")

    def _request_embed(self, req: Dict[str, Any]) -> discord.Embed:
        status = (req.get("status") or "submitted").replace("_", " ").title()
        emb = discord.Embed(title=f"Weather Bot {req.get('request_type','feedback').title()} #{req['id']}", description=req.get("message") or "", timestamp=datetime.fromisoformat(req["created_at"]))
        emb.add_field(name="Status", value=status, inline=True)
        emb.add_field(name="From", value=f"<@{req['user_id']}> (`{req['user_id']}`)", inline=True)
        emb.add_field(name="Context", value=req.get("guild_name") or "DM", inline=False)
        if req.get("staff_note"): emb.add_field(name="Staff note", value=req["staff_note"][:1024], inline=False)
        return emb

    async def _send_feedback(self, inter: discord.Interaction, kind: str, message: str) -> int:
        now = asyncio.get_running_loop().time(); last = self._feedback_last.get(inter.user.id, 0)
        if now-last < 60: raise RuntimeError("cooldown")
        self._feedback_last[inter.user.id] = now
        message=(message or "").strip()
        if not message: raise ValueError("empty")
        message=message[:1800]
        rid=self.store.create_feedback_request(inter.user.id, inter.guild.id if inter.guild else None, inter.guild.name if inter.guild else None, kind.lower().replace(" report", ""), message)
        req=self.store.get_feedback_request(rid); emb=self._request_embed(req)
        sent=None
        if FEEDBACK_CHANNEL_ID:
            ch=self.bot.get_channel(FEEDBACK_CHANNEL_ID) or await self.bot.fetch_channel(FEEDBACK_CHANNEL_ID)
            sent=await ch.send(embed=emb, view=RequestStatusView(self, rid))
        elif BOT_OWNER_ID:
            owner=self.bot.get_user(BOT_OWNER_ID) or await self.bot.fetch_user(BOT_OWNER_ID)
            sent=await owner.send(embed=emb, view=RequestStatusView(self, rid))
        else: raise RuntimeError("no_owner")
        if sent: self.store.set_feedback_message(rid, sent.channel.id, sent.id)
        return rid

    async def _update_request_status(self, request_id: int, status: str, note: Optional[str] = None):
        req=self.store.update_feedback_status(request_id, status, note)
        if not req: raise ValueError("Request not found")
        try:
            user=self.bot.get_user(int(req["user_id"])) or await self.bot.fetch_user(int(req["user_id"]))
            text=f"Your {req['request_type']} request **#{request_id}** is now **{status.replace('_',' ')}**."
            if note: text += f"\n\n**Note:** {note}"
            await user.send(text)
            self.store.mark_feedback_notified(request_id)
        except Exception:
            pass
        return req

    async def _feedback_command(self, inter, kind, message):
        await inter.response.defer(ephemeral=True)
        try: rid=await self._send_feedback(inter, kind, message)
        except RuntimeError as e:
            return await inter.followup.send("Please wait a minute before submitting another request." if str(e)=="cooldown" else "Feedback routing is not configured correctly.", ephemeral=True)
        except ValueError: return await inter.followup.send("Please include a message.", ephemeral=True)
        await inter.followup.send(f"✅ Submitted as **#{rid}**. You’ll be notified when its status changes.", ephemeral=True)

    @app_commands.command(name="help", description="Open the interactive Weather Bot help menu.")
    async def help_cmd(self, inter: discord.Interaction):
        embed = discord.Embed(
            title="🌦️ Weather Bot Help",
            description="Choose a topic below. You can also create a subscription directly from this menu.",
            colour=discord.Colour.blurple(),
        )
        await inter.response.send_message(embed=embed, view=HelpView(self, inter.user.id), ephemeral=True)

    @app_commands.command(name="feedback", description="Send feedback to the bot owner.")
    async def feedback_cmd(self, inter: discord.Interaction, message: str): await self._feedback_command(inter,"feedback",message)
    @app_commands.command(name="bug", description="Report a bug to the bot owner.")
    async def bug_cmd(self, inter: discord.Interaction, message: str): await self._feedback_command(inter,"bug",message)
    @app_commands.command(name="feature", description="Request a feature.")
    async def feature_cmd(self, inter: discord.Interaction, message: str): await self._feedback_command(inter,"feature",message)

    @app_commands.command(name="my_requests", description="Show your recent feedback, bugs, and feature requests.")
    async def my_requests(self, inter: discord.Interaction):
        rows=self.store.list_feedback_requests(inter.user.id)
        if not rows: return await inter.response.send_message("You have no saved requests.", ephemeral=True)
        lines=[f"**#{r['id']}** · {r['request_type']} · **{r['status'].replace('_',' ')}**\n{r['message'][:120]}" for r in rows]
        await inter.response.send_message("\n\n".join(lines), ephemeral=True)

    @app_commands.command(name="request_update", description="Owner: update a submitted request.")
    @app_commands.choices(status=[app_commands.Choice(name=x.replace('_',' ').title(), value=x) for x in ["submitted","planned","in_progress","completed","declined","duplicate","fixed"]])
    async def request_update(self, inter: discord.Interaction, request_id: int, status: app_commands.Choice[str], note: Optional[str]=None):
        if not self._is_staff(inter.user): return await inter.response.send_message("Owner only.", ephemeral=True)
        await self._update_request_status(request_id,status.value,note)
        await inter.response.send_message(f"Updated request #{request_id} to **{status.name}**.", ephemeral=True)

    @app_commands.command(name="location_set", description="Save a city, postal code, or place as your default location.")
    async def location_set(self, inter: discord.Interaction, location: str, name: Optional[str]="Default"):
        await inter.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session: loc=(await search_locations(session, location, 1))[0]
            self.store.save_location(inter.user.id,loc,name or "Default",True)
            await inter.followup.send(f"✅ Saved **{loc['display_name']}** as **{name or 'Default'}**. Timezone: `{loc['timezone']}`",ephemeral=True)
        except Exception as e: await inter.followup.send(f"Could not save that location: {e}",ephemeral=True)

    @app_commands.command(name="locations", description="List your saved weather locations.")
    async def locations(self, inter: discord.Interaction):
        rows=self.store.list_locations(inter.user.id)
        if not rows: return await inter.response.send_message("No locations saved. Use `/location_set`.",ephemeral=True)
        await inter.response.send_message("\n".join(f"{'⭐' if r['is_default'] else '•'} **{r['name']}** — {r['display_name']}" for r in rows),ephemeral=True)

    @app_commands.command(name="weather_set_zip", description="Legacy: save a US ZIP as your default location.")
    async def weather_set_zip(self, inter: discord.Interaction, zip: str):
        z = re.sub(r"[^0-9]", "", zip)
        if len(z) != 5:
            return await inter.response.send_message("Please provide a valid 5-digit US ZIP.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                loc = (await search_locations(session, f"{z}, United States", 1))[0]
            self.store.set_user_zip(inter.user.id, z)
            self.store.save_location(inter.user.id, loc, "Default", True)
            await inter.followup.send(f"✅ Saved **{loc['display_name']}** as your default location.", ephemeral=True)
        except Exception as e:
            await inter.followup.send(f"Could not resolve that ZIP: {e}", ephemeral=True)

    @app_commands.command(name="units", description="Set standard or metric weather units.")
    @app_commands.choices(mode=[app_commands.Choice(name="standard (°F, mph, in)",value="standard"),app_commands.Choice(name="metric (°C, km/h, mm)",value="metric")])
    async def units_cmd(self, inter: discord.Interaction, mode: app_commands.Choice[str]):
        self.store.set_note(inter.user.id,"wx_units",mode.value); await inter.response.send_message(f"✅ Units set to **{mode.value}**.",ephemeral=True)

    @app_commands.command(name="timezone", description="Set your scheduling timezone.")
    async def timezone_cmd(self, inter: discord.Interaction, tz_name: str):
        try: _tzinfo_from_name(tz_name)
        except Exception: return await inter.response.send_message("Invalid IANA timezone.",ephemeral=True)
        self.store.set_note(inter.user.id,"wx_tz",tz_name); await inter.response.send_message(f"✅ Timezone set to `{tz_name}`.",ephemeral=True)

    @app_commands.command(name="settings", description="Show your saved weather settings.")
    async def settings_cmd(self, inter: discord.Interaction):
        loc=self.store.get_default_location(inter.user.id)
        await inter.response.send_message(f"**Location:** {loc['display_name'] if loc else 'Not set'}\n**Units:** {_get_user_units(self.store,inter.user.id)}\n**Timezone:** {_get_user_tz_name(self.store,inter.user.id)}",ephemeral=True)

    async def _current_embed(self, session, loc, units, tz_name):
        temp_unit="fahrenheit" if units=="standard" else "celsius"; wind_unit="mph" if units=="standard" else "kmh"; precip_unit="inch" if units=="standard" else "mm"; deg="°F" if units=="standard" else "°C"
        params={"latitude":loc["latitude"],"longitude":loc["longitude"],"temperature_unit":temp_unit,"wind_speed_unit":wind_unit,"precipitation_unit":precip_unit,"timezone":tz_name,"current":"temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,precipitation,weather_code","daily":"weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,uv_index_max,sunrise,sunset,wind_speed_10m_max"}
        async with session.get("https://api.open-meteo.com/v1/forecast",params=params,timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status!=200: raise RuntimeError("Weather service unavailable")
            wx=await r.json()
        cur=wx.get("current") or {}; daily=wx.get("daily") or {}; code=(daily.get("weather_code") or [cur.get("weather_code",0)])[0]; icon,desc=wx_icon_desc(code)
        t=cur.get("temperature_2m"); hi=(daily.get("temperature_2m_max") or [None])[0]; lo=(daily.get("temperature_2m_min") or [None])[0]
        emb=discord.Embed(title=f"{icon} Weather — {loc['display_name']}",description=f"**{desc}**",colour=wx_color_from_temp_f(float(t) if t is not None and units=="standard" else 70))
        if t is not None: emb.add_field(name="Now",value=f"**{round(t)}{deg}** (feels {round(cur.get('apparent_temperature',t))}{deg})")
        if hi is not None: emb.add_field(name="Today",value=f"High **{round(hi)}{deg}** / Low **{round(lo)}{deg}**")
        emb.add_field(name="Wind",value=f"{round(cur.get('wind_speed_10m',0))} {wind_unit} (gusts {round(cur.get('wind_gusts_10m',0))} {wind_unit})")
        emb.add_field(name="Humidity",value=f"{cur.get('relative_humidity_2m','?')}%")
        emb.add_field(name="Precip Chance",value=f"{(daily.get('precipitation_probability_max') or ['?'])[0]}%")
        emb.add_field(name="UV",value=str((daily.get('uv_index_max') or ['?'])[0]))
        emb.set_footer(text=f"Units: {units} • Timezone: {tz_name}")
        return emb

    @app_commands.command(name="weather", description="Current weather for any city, postal code, or saved location.")
    async def weather_cmd(self, inter: discord.Interaction, location: Optional[str]=None):
        await inter.response.defer()
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                loc=await self._resolve_location(session,inter.user.id,location)
                tz=loc.get("timezone") or _get_user_tz_name(self.store,inter.user.id)
                emb=await self._current_embed(session,loc,_get_user_units(self.store,inter.user.id),tz)
            await inter.followup.send(embed=emb)
            self.store.record_event("weather_lookup", inter.user.id, inter.guild.id if inter.guild else None)
        except Exception as e: await inter.followup.send(f"⚠️ {e}",ephemeral=True)

    @app_commands.command(name="hourly", description="Hourly forecast for any location.")
    async def hourly_cmd(self, inter: discord.Interaction, location: Optional[str]=None, hours: app_commands.Range[int,6,24]=12):
        await inter.response.defer()
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                loc=await self._resolve_location(session,inter.user.id,location); units=_get_user_units(self.store,inter.user.id); tz=loc.get("timezone") or _get_user_tz_name(self.store,inter.user.id)
                rows=await _fetch_hourly(session,loc["latitude"],loc["longitude"],tz,units,hours)
            lines=[]
            for ts,code,temp,pop,prec,wind,wu,pu,deg in rows:
                icon,desc=wx_icon_desc(code); lines.append(f"**{datetime.fromisoformat(ts).strftime('%I %p')}** {icon} {round(temp)}{deg} · rain {pop or 0}% · wind {round(wind or 0)} {wu}")
            emb=discord.Embed(title=f"Hourly — {loc['display_name']}",description="\n".join(lines)); await inter.followup.send(embed=emb)
        except Exception as e: await inter.followup.send(f"⚠️ {e}",ephemeral=True)

    @app_commands.command(name="moon", description="Show today's moon phase.")
    async def moon_cmd(self, inter: discord.Interaction, location: Optional[str]=None):
        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
            try: loc=await self._resolve_location(session,inter.user.id,location); tz=loc.get("timezone") or "UTC"
            except Exception: loc={"display_name":"your location"}; tz=_get_user_tz_name(self.store,inter.user.id)
        now=datetime.now(_tzinfo_from_name(tz)); name,emoji,age=moon_phase_info_for_date(now)
        await inter.response.send_message(embed=discord.Embed(title=f"{emoji} Moon Phase — {loc['display_name']}",description=f"**{name}**\nMoon age: {age} days"))

    @app_commands.command(name="weather_subscribe", description="Open the guided forecast subscription setup wizard.")
    async def weather_subscribe(self, inter: discord.Interaction):
        state = SubscriptionDraft(user_id=inter.user.id)
        embed = wizard_embed("Where should this report go?", "Choose a personal DM or a server channel.", state)
        await inter.response.send_message(embed=embed, view=DestinationView(self, state), ephemeral=True)

    @app_commands.command(name="weather_subscribe_advanced", description="Create a subscription using advanced command options.")
    @app_commands.describe(time="Delivery time, such as 7:00 AM", cadence="daily or weekly", location="City, place, postal code, or saved default", destination="dm or channel", channel="Required for channel delivery", weekly_days="Number of forecast days for weekly reports", metric="max_wind, max_temp, min_temp, rain_chance, precipitation, or uv", operator=">, >=, <, or <=", threshold="Only send when this condition matches")
    async def weather_subscribe_advanced(self, inter: discord.Interaction, time: str, cadence: str="daily", location: Optional[str]=None, destination: str="dm", channel: Optional[discord.TextChannel]=None, weekly_days: app_commands.Range[int,3,10]=7, metric: Optional[str]=None, operator: Optional[str]=None, threshold: Optional[float]=None):
        await inter.response.defer(ephemeral=True)
        try:
            cadence=cadence.lower(); destination=destination.lower()
            if cadence not in {"daily","weekly"}: raise ValueError("Cadence must be daily or weekly.")
            if destination not in {"dm","channel"}: raise ValueError("Destination must be dm or channel.")
            if destination=="channel":
                if not inter.guild or not channel: raise ValueError("Choose a server channel.")
                if not inter.user.guild_permissions.manage_guild: raise ValueError("Manage Server permission is required.")
                perms=channel.permissions_for(inter.guild.me)
                if not (perms.view_channel and perms.send_messages and perms.embed_links): raise ValueError("I need View Channel, Send Messages, and Embed Links there.")
            if metric or operator or threshold is not None:
                if metric not in {"max_wind","max_temp","min_temp","rain_chance","precipitation","uv"} or operator not in {">",">=","<","<="} or threshold is None: raise ValueError("Provide a valid metric, operator, and threshold together.")
            hh,mi=_parse_time(time)
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session: loc=await self._resolve_location(session,inter.user.id,location)
            tz_name=loc.get("timezone") or _get_user_tz_name(self.store,inter.user.id); first=_next_local_run(datetime.now(_tzinfo_from_name(tz_name)),hh,mi,cadence)
            sub={"user_id":inter.user.id,"zip":"","cadence":cadence,"hh":hh,"mi":mi,"weekly_days":weekly_days,"tz_name":tz_name,"units":_get_user_units(self.store,inter.user.id),"next_run_utc":first.astimezone(timezone.utc).isoformat(),"location_name":loc["display_name"],"latitude":loc["latitude"],"longitude":loc["longitude"],"country_code":loc.get("country_code"),"destination_type":destination,"guild_id":inter.guild.id if destination=="channel" else None,"channel_id":channel.id if channel else None,"created_by":inter.user.id,"condition_metric":metric,"condition_operator":operator,"condition_value":threshold,"condition_unit":_get_user_units(self.store,inter.user.id),"enabled":1}
            sid=self.store.add_weather_sub(sub); condition=f" only when `{metric} {operator} {threshold}`" if metric else ""
            await inter.followup.send(f"✅ Subscription **#{sid}** created for **{loc['display_name']}**, delivered to **{channel.mention if channel else 'DM'}**{condition}. Next evaluation: {first.strftime('%Y-%m-%d %I:%M %p %Z')}",ephemeral=True)
        except Exception as e: await inter.followup.send(f"⚠️ {e}",ephemeral=True)

    @app_commands.command(name="weather_subscriptions", description="Open the subscription management dashboard.")
    async def weather_subscriptions(self, inter: discord.Interaction):
        rows = self.store.list_weather_subs(inter.user.id)
        if inter.guild and inter.user.guild_permissions.manage_guild:
            known = {r["id"] for r in rows}
            rows += [r for r in self.store.list_weather_subs(guild_id=inter.guild.id) if r["id"] not in known]
        if not rows:
            return await inter.response.send_message("No subscriptions found. Use `/weather_subscribe` to create one.", ephemeral=True)
        lines = []
        for row in rows[:25]:
            dest = f"<#{row['channel_id']}>" if row.get("destination_type") == "channel" else "DM"
            cond = f" · if {row['condition_metric']} {row['condition_operator']} {row['condition_value']}" if row.get("condition_metric") else " · always"
            status = "active" if row.get("enabled", 1) else "paused"
            result = f"\nLast evaluation: {row['last_result']}" if row.get("last_result") else ""
            lines.append(
                f"**#{row['id']}** · {status} · {row['cadence']} at {row['hh']:02d}:{row['mi']:02d}\n"
                f"{row.get('location_name') or row.get('zip')} → {dest}{cond}{result}"
            )
        embed = discord.Embed(
            title="🔔 Subscription Dashboard",
            description="\n\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="Choose a subscription below to test, pause/resume, or delete it.")
        await inter.response.send_message(
            embed=embed,
            view=SubscriptionManageView(self, inter.user.id, rows, inter.guild.id if inter.guild else None),
            ephemeral=True,
        )

    @app_commands.command(name="weather_unsubscribe", description="Remove a weather subscription.")
    async def weather_unsubscribe(self, inter: discord.Interaction, sub_id: int):
        admin=bool(inter.guild and inter.user.guild_permissions.manage_guild)
        ok=self.store.remove_weather_sub(sub_id,inter.user.id,inter.guild.id if inter.guild else None,admin)
        await inter.response.send_message("Removed." if ok else "Could not remove that subscription.",ephemeral=True)

    def _condition_matches(self, sub, outlook):
        metric=sub.get("condition_metric")
        if not metric: return True,"always send"
        rows=outlook[:1] if sub.get("cadence")=="daily" else outlook
        values=[]
        for row in rows:
            # row: date,line,sunrise,sunset,uv,hi, plus appended metrics in new fetch implementation if present
            data=row[6] if len(row)>6 else {}
            mapping={"max_wind":data.get("max_wind"),"max_temp":data.get("max_temp"),"min_temp":data.get("min_temp"),"rain_chance":data.get("rain_chance"),"precipitation":data.get("precipitation"),"uv":data.get("uv")}
            if mapping.get(metric) is not None: values.append(float(mapping[metric]))
        if not values: return False,"metric unavailable"
        actual=min(values) if metric=="min_temp" else max(values); target=float(sub["condition_value"]); op=sub["condition_operator"]
        matched={">":actual>target,">=":actual>=target,"<":actual<target,"<=":actual<=target}[op]
        return matched,f"{metric} was {actual:g}; required {op} {target:g}"

    def _outlook_embed(self, sub, outlook):
        title=("Daily" if sub["cadence"]=="daily" else "Weekly")+f" Outlook — {sub.get('location_name') or sub.get('zip')}"
        emb=discord.Embed(title=title,colour=discord.Colour.blurple())
        for d,line,sunrise,sunset,uv,*_ in outlook:
            extras=[]
            if sunrise: extras.append(f"🌅 {fmt_sun(sunrise)}")
            if sunset: extras.append(f"🌇 {fmt_sun(sunset)}")
            if uv is not None: extras.append(f"🔆 UV {round(uv,1)}")
            emb.add_field(name=d,value=line+("\n"+" · ".join(extras) if extras else ""),inline=False)
        emb.set_footer(text=f"Scheduled in {sub['tz_name']} • Units: {sub['units']}")
        return emb

    @tasks.loop(seconds=60)
    async def weather_scheduler(self):
        if not self.store: return
        now=datetime.now(timezone.utc)
        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
            for s in self.store.list_weather_subs(enabled_only=True):
                try:
                    due=datetime.fromisoformat(s["next_run_utc"]); due=due if due.tzinfo else due.replace(tzinfo=timezone.utc)
                    if due>now: continue
                    lat=s.get("latitude"); lon=s.get("longitude")
                    if lat is None and s.get("zip"):
                        city,state,lat,lon=await _zip_to_place_and_coords(session,s["zip"]); s["location_name"]=f"{city}, {state} {s['zip']}"
                    days=2 if s["cadence"]=="daily" else max(3,min(10,int(s.get("weekly_days") or 7)))
                    outlook=await _fetch_outlook(session,float(lat),float(lon),days,s.get("tz_name") or DEFAULT_TZ_NAME,s.get("units") or "standard")
                    matched,result=self._condition_matches(s,outlook)
                    if matched:
                        emb=self._outlook_embed(s,outlook)
                        if s.get("destination_type")=="channel":
                            ch=self.bot.get_channel(int(s["channel_id"])) or await self.bot.fetch_channel(int(s["channel_id"])); await ch.send(embed=emb)
                        else:
                            user=self.bot.get_user(int(s["user_id"])) or await self.bot.fetch_user(int(s["user_id"])); await user.send(embed=emb)
                    tz=_tzinfo_from_name(s.get("tz_name") or DEFAULT_TZ_NAME); nxt=datetime.now(tz).replace(hour=int(s["hh"]),minute=int(s["mi"]),second=0,microsecond=0)+timedelta(days=1 if s["cadence"]=="daily" else 7)
                    self.store.update_weather_sub(s["id"],nxt.astimezone(timezone.utc).isoformat(),failure_count=0,last_error=None,last_result=("sent: " if matched else "not sent: ")+result,last_sent_at=now.isoformat() if matched else s.get("last_sent_at"))
                    self.store.record_event("scheduled_sent" if matched else "scheduled_skipped", s.get("user_id"), s.get("guild_id"))
                except Exception as e:
                    failures=int(s.get("failure_count") or 0)+1; disable=failures>=5
                    self.store.update_weather_sub(s["id"],(now+timedelta(minutes=5)).isoformat(),failure_count=failures,last_error=str(e)[:300],last_result="delivery failed",enabled=0 if disable else 1)
                    self.store.record_event("scheduler_error", s.get("user_id"), s.get("guild_id"))

    @weather_scheduler.before_loop
    async def before_weather(self): await self.bot.wait_until_ready()

    def owner_analytics_embed(self) -> discord.Embed:
        stats = self.store.analytics_summary(); today = stats["events_today"]
        emb = discord.Embed(title="🌦️ Weather Bot Owner Analytics", description=f"Version **{BOT_VERSION}**", colour=discord.Colour.blurple(), timestamp=datetime.now(timezone.utc))
        emb.add_field(name="Reach", value=f"Servers **{len(self.bot.guilds):,}**\nKnown users **{stats['known_users']:,}**\nSaved locations **{stats['saved_locations']:,}**", inline=True)
        emb.add_field(name="Automation", value=f"Active subscriptions **{stats['active_subscriptions']:,}**\nServer subscriptions **{stats['server_subscriptions']:,}**\nSticky dashboards **{stats['sticky_dashboards']:,}**", inline=True)
        emb.add_field(name="Today", value=f"Weather lookups **{today.get('weather_lookup',0):,}**\nReports sent **{today.get('scheduled_sent',0):,}**\nThreshold skips **{today.get('scheduled_skipped',0):,}**\nScheduler errors **{today.get('scheduler_error',0):,}", inline=True)
        emb.add_field(name="Pending", value=f"Feature requests **{stats['pending_features']:,}**\nBug reports **{stats['pending_bugs']:,}**", inline=False)
        emb.set_footer(text="Analytics begin accumulating after upgrading to 2.0.1; aggregate database totals are immediate.")
        return emb

    @app_commands.command(name="owner_analytics", description="Owner: open Weather Bot analytics.")
    async def owner_analytics(self, inter: discord.Interaction):
        if not self._is_staff(inter.user):
            return await inter.response.send_message("Only the configured bot owner can use this command.", ephemeral=True)
        await inter.response.send_message(embed=self.owner_analytics_embed(), view=OwnerAnalyticsView(self, inter.user.id), ephemeral=True)

    async def refresh_sticky_dashboard(self, row: Dict[str, Any], message=None):
        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
            loc={"display_name":row["location_name"],"latitude":row["latitude"],"longitude":row["longitude"]}
            emb=await self._current_embed(session, loc, row["units"], row["timezone"])
        emb.title = emb.title.replace("Weather —", "Live Weather —")
        emb.description = (emb.description or "") + "\n\n*This message refreshes automatically.*"
        emb.set_footer(text=f"Weather Bot {BOT_VERSION} • Updated {datetime.now(_tzinfo_from_name(row['timezone'])).strftime('%b %d, %I:%M %p %Z')} • Every {row['refresh_minutes']} min")
        if message is None:
            channel=self.bot.get_channel(int(row["channel_id"])) or await self.bot.fetch_channel(int(row["channel_id"]))
            message=await channel.fetch_message(int(row["message_id"]))
        await message.edit(embed=emb, view=StickyWeatherView(self))
        self.store.update_sticky_dashboard(row["id"], last_refresh_at=datetime.now(timezone.utc).isoformat(), last_error=None)

    @app_commands.command(name="sticky_weather_create", description="Create an automatically refreshing weather dashboard in a channel.")
    @app_commands.describe(channel="Channel that will contain the dashboard", location="City, place, postal code, or your saved default", refresh_minutes="Refresh interval; 15 minutes recommended")
    async def sticky_weather_create(self, inter: discord.Interaction, channel: discord.TextChannel, location: Optional[str]=None, refresh_minutes: app_commands.Range[int,5,60]=15):
        if not inter.guild or not inter.user.guild_permissions.manage_guild:
            return await inter.response.send_message("You need **Manage Server** to create a sticky dashboard.", ephemeral=True)
        perms=channel.permissions_for(inter.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.read_message_history):
            return await inter.response.send_message("I need View Channel, Send Messages, Embed Links, and Read Message History there.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                loc=await self._resolve_location(session, inter.user.id, location)
            units=_get_user_units(self.store, inter.user.id); tz=loc.get("timezone") or _get_user_tz_name(self.store, inter.user.id)
            placeholder=await channel.send(embed=discord.Embed(title="🌦️ Creating weather dashboard…", colour=discord.Colour.blurple()))
            did=self.store.add_sticky_dashboard({"guild_id":inter.guild.id,"channel_id":channel.id,"message_id":placeholder.id,"created_by":inter.user.id,"location_name":loc["display_name"],"latitude":loc["latitude"],"longitude":loc["longitude"],"country_code":loc.get("country_code"),"timezone":tz,"units":units,"refresh_minutes":refresh_minutes,"enabled":1})
            row=self.store.get_sticky_dashboard(dashboard_id=did); await self.refresh_sticky_dashboard(row, placeholder)
            await inter.followup.send(f"✅ Sticky dashboard **#{did}** created in {channel.mention}.", ephemeral=True)
        except Exception as exc:
            await inter.followup.send(f"⚠️ Could not create the dashboard: {exc}", ephemeral=True)

    @app_commands.command(name="sticky_weather_list", description="List this server's sticky weather dashboards.")
    async def sticky_weather_list(self, inter: discord.Interaction):
        if not inter.guild or not inter.user.guild_permissions.manage_guild:
            return await inter.response.send_message("You need **Manage Server** to manage sticky dashboards.", ephemeral=True)
        rows=self.store.list_sticky_dashboards(guild_id=inter.guild.id)
        text="\n".join(f"**#{r['id']}** · <#{r['channel_id']}> · {r['location_name']} · every {r['refresh_minutes']} min · {'active' if r['enabled'] else 'paused'}" for r in rows) or "No sticky dashboards configured."
        await inter.response.send_message(embed=discord.Embed(title="Sticky Weather Dashboards", description=text, colour=discord.Colour.blurple()), ephemeral=True)

    @app_commands.command(name="sticky_weather_delete", description="Delete a sticky weather dashboard.")
    async def sticky_weather_delete(self, inter: discord.Interaction, dashboard_id: int):
        if not inter.guild or not inter.user.guild_permissions.manage_guild:
            return await inter.response.send_message("You need **Manage Server** to manage sticky dashboards.", ephemeral=True)
        row=self.store.get_sticky_dashboard(dashboard_id=dashboard_id)
        if not row or row["guild_id"] != inter.guild.id:
            return await inter.response.send_message("Dashboard not found in this server.", ephemeral=True)
        try:
            channel=self.bot.get_channel(int(row["channel_id"])) or await self.bot.fetch_channel(int(row["channel_id"]))
            msg=await channel.fetch_message(int(row["message_id"])); await msg.delete()
        except Exception: pass
        self.store.remove_sticky_dashboard(dashboard_id, inter.guild.id)
        await inter.response.send_message("✅ Sticky dashboard deleted.", ephemeral=True)

    @tasks.loop(minutes=1)
    async def sticky_dashboard_scheduler(self):
        now=datetime.now(timezone.utc)
        for row in self.store.list_sticky_dashboards(enabled_only=True):
            try:
                last=datetime.fromisoformat(row["last_refresh_at"]) if row.get("last_refresh_at") else datetime.fromtimestamp(0, timezone.utc)
                if last.tzinfo is None: last=last.replace(tzinfo=timezone.utc)
                if now-last < timedelta(minutes=max(5,int(row.get("refresh_minutes") or 15))): continue
                await self.refresh_sticky_dashboard(row)
            except Exception as exc:
                self.store.update_sticky_dashboard(row["id"], last_error=str(exc)[:300])

    @sticky_dashboard_scheduler.before_loop
    async def before_sticky_dashboards(self): await self.bot.wait_until_ready()

    @app_commands.command(name="wx_alerts", description="Enable or disable US NWS alerts by DM.")
    async def wx_alerts(self, inter: discord.Interaction, mode: str, min_severity: Optional[str]="moderate"):
        mode=mode.lower()
        if mode not in {"on","off"}: return await inter.response.send_message("Use on or off.",ephemeral=True)
        self.store.set_note(inter.user.id,"wx_alerts_enabled","1" if mode=="on" else "0")
        self.store.set_note(inter.user.id,"wx_alerts_min_sev",min_severity or "moderate")
        await inter.response.send_message(f"Alerts **{mode.upper()}**. NWS alerts are available only for US locations.",ephemeral=True)

    async def _fetch_nws_alerts(self,session,lat,lon):
        async with session.get("https://api.weather.gov/alerts/active",params={"point":f"{lat},{lon}"},headers=HTTP_HEADERS,timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status!=200:return []
            data=await r.json()
        return [f.get("properties",{}) for f in data.get("features",[])]

    @tasks.loop(minutes=5)
    async def wx_alerts_scheduler(self):
        if not self.store:return
        rows=self.store.db.execute("SELECT DISTINCT user_id FROM notes WHERE key='wx_alerts_enabled' AND value='1'").fetchall()
        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
            for row in rows:
                uid=int(row[0]); loc=self.store.get_default_location(uid)
                if not loc or loc.get("country_code")!="US":continue
                try:
                    alerts=await self._fetch_nws_alerts(session,loc["latitude"],loc["longitude"]); fresh=[]
                    minsev=(self.store.get_note(uid,"wx_alerts_min_sev") or "moderate").lower(); order={"minor":0,"moderate":1,"severe":2,"extreme":3}
                    for a in alerts:
                        aid=a.get("id") or a.get("@id"); sev=(a.get("severity") or "minor").lower()
                        if not aid or order.get(sev,0)<order.get(minsev,1) or self.store.get_note(uid,f"seen_alert:{aid}"):continue
                        fresh.append(a)
                    if not fresh:continue
                    emb=discord.Embed(title=f"⚠️ Weather Alerts — {loc['display_name']}",colour=discord.Colour.orange())
                    for a in fresh[:10]: emb.add_field(name=f"{a.get('event','Alert')} ({a.get('severity','Unknown')})",value=(a.get('headline') or a.get('description') or 'Details unavailable')[:1000],inline=False)
                    user=self.bot.get_user(uid) or await self.bot.fetch_user(uid); await user.send(embed=emb)
                    for a in fresh:self.store.set_note(uid,f"seen_alert:{a.get('id') or a.get('@id')}",datetime.now(timezone.utc).isoformat())
                except Exception: continue

    @wx_alerts_scheduler.before_loop
    async def before_alerts(self): await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Weather(bot, store=getattr(bot,"store",None)))
