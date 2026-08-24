
import os
import re
import html
import json
import io
import math
import aiohttp
from PIL import Image, ImageDraw
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

from astral import moon

import discord
from discord.ext import tasks, commands
from discord import app_commands
from location_service import search_locations
from weather_ui import (HelpView, SubscriptionDraft, ReportTypeView, SubscriptionManageView, wizard_embed, WeatherDashboardView, dashboard_home_embed)

# ---- Constants & styling helpers ----
DEFAULT_TZ_NAME = "America/Chicago"
HTTP_HEADERS = {
    "User-Agent": "UtilaBot/1.0 (+https://github.com/ethanocurtis/Utilabot)",
    "Accept": "application/json",
}
# ---- Feedback routing (set via env) ----
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or 0)  # your Discord user id
FEEDBACK_CHANNEL_ID = int(os.getenv("FEEDBACK_CHANNEL_ID", "0") or 0)  # optional: send feedback to this channel id
BOT_VERSION = "2.5.2"
GITHUB_REPO_URL = "https://github.com/ethanocurtis/Weather-Bot"
GITHUB_ISSUES_URL = f"{GITHUB_REPO_URL}/issues"



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
        out.append((d, line, sunrise, sunset, uv, hi, {"max_wind": wm, "max_temp": hi, "min_temp": lo, "rain_chance": pp, "precipitation": pr, "uv": uv, "weather_code": code, "condition": desc}))
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



async def _fetch_air_quality(session: aiohttp.ClientSession, lat: float, lon: float, tz_name: str) -> Dict[str, Any]:
    params = {
        "latitude": lat, "longitude": lon, "timezone": tz_name,
        "current": "us_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,ozone",
        "hourly": "alder_pollen,birch_pollen,grass_pollen,mugwort_pollen,olive_pollen,ragweed_pollen",
        "forecast_days": 2,
    }
    async with session.get("https://air-quality-api.open-meteo.com/v1/air-quality", params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            raise RuntimeError("Air-quality API unavailable.")
        data = await r.json()
    current = data.get("current") or {}
    hourly = data.get("hourly") or {}
    pollen = {}
    for key in ("alder_pollen","birch_pollen","grass_pollen","mugwort_pollen","olive_pollen","ragweed_pollen"):
        vals = [v for v in (hourly.get(key) or [])[:24] if isinstance(v,(int,float))]
        pollen[key] = max(vals) if vals else None
    return {"current": current, "pollen": pollen}


def _aqi_label(value: Optional[float]) -> Tuple[str, discord.Colour]:
    if value is None: return "Unavailable", discord.Colour.light_grey()
    v=float(value)
    if v <= 50: return "Good", discord.Colour.green()
    if v <= 100: return "Moderate", discord.Colour.gold()
    if v <= 150: return "Unhealthy for sensitive groups", discord.Colour.orange()
    if v <= 200: return "Unhealthy", discord.Colour.red()
    if v <= 300: return "Very unhealthy", discord.Colour.dark_red()
    return "Hazardous", discord.Colour.dark_magenta()


def _pollen_level(value: Optional[float]) -> str:
    if value is None: return "Unavailable"
    v=float(value)
    if v < 1: return "None"
    if v < 10: return "Low"
    if v < 50: return "Moderate"
    if v < 200: return "High"
    return "Very high"


def _precipitation_forecast_text(condition: str, weather_code: Optional[int], rain_chance: Optional[float]) -> Optional[str]:
    """Build probability-aware precipitation wording without implying it is occurring now."""
    code = int(weather_code) if weather_code is not None else -1
    precip_codes = set(range(51, 58)) | set(range(61, 68)) | set(range(71, 78)) | set(range(80, 87)) | {95, 96, 99}
    if code not in precip_codes:
        return None

    chance = round(float(rain_chance)) if rain_chance is not None else None
    lower = (condition or "precipitation").lower()

    if "thunder" in lower:
        event = "thunderstorms"
    elif "snow" in lower or code == 77:
        event = "snow"
    elif "freezing" in lower:
        event = "freezing precipitation"
    elif "drizzle" in lower:
        event = "drizzle"
    else:
        event = "rain"

    if chance is None:
        lead = f"{condition} is possible today."
    elif chance >= 70:
        lead = f"{event.capitalize()} is likely today, with chances peaking near **{chance}%**."
    elif chance >= 40:
        lead = f"{event.capitalize()} is possible today, with chances peaking near **{chance}%**."
    elif chance >= 20:
        lead = f"A few periods of {event} are possible today, although the peak chance is only **{chance}%**."
    else:
        lead = f"There is only a slight chance of {event} today, peaking near **{chance}%**."

    if code in {55, 57, 65, 67, 75, 81, 82, 86, 96, 99}:
        if chance is not None and chance < 50:
            lead += f" Many locations may stay dry, but any {event} that develops could be heavy."
        else:
            lead += f" Any {event} that develops may be heavy."
    elif code in {95}:
        lead += " Brief thunderstorms may occur where storms develop."
    return lead


def _weather_briefing(location_name: str, outlook: List[Tuple], units: str, air: Optional[Dict[str,Any]]=None) -> str:
    if not outlook: return f"No forecast is currently available for {location_name}."
    d,line,_,_,uv,hi,metrics=outlook[0]
    condition = metrics.get("condition") or re.sub(r"^\S+\s+", "", line).split(" — ",1)[0]
    weather_code = metrics.get("weather_code")
    temp_unit = "°F" if units == "standard" else "°C"
    wind_unit = "mph" if units == "standard" else "km/h"
    rain=metrics.get("rain_chance")
    precip_text = _precipitation_forecast_text(condition, weather_code, rain)
    parts=[precip_text or f"Expect **{condition.lower()}** in **{location_name}** today."]
    if metrics.get("max_temp") is not None and metrics.get("min_temp") is not None:
        parts.append(f"Temperatures should range from **{round(metrics['min_temp'])}{temp_unit}** to **{round(metrics['max_temp'])}{temp_unit}**.")
    if rain is not None and precip_text is None:
        if rain >= 70: parts.append(f"Rain is likely, with a peak chance near **{round(rain)}%**; plan for wet conditions.")
        elif rain >= 35: parts.append(f"There is a **{round(rain)}%** chance of rain, so keeping rain gear nearby would be sensible.")
        elif rain >= 20: parts.append(f"A few showers are possible, with a peak chance near **{round(rain)}%**.")
        else: parts.append(f"Dry conditions are favored, with rain chances peaking near **{round(rain)}%**.")
    wind=metrics.get("max_wind")
    if wind is not None:
        if wind >= (25 if units=='standard' else 40): parts.append(f"Winds may be strong, reaching about **{round(wind)} {wind_unit}**; secure loose outdoor items.")
        else: parts.append(f"Winds should peak near **{round(wind)} {wind_unit}**.")
    if uv is not None and uv >= 6: parts.append(f"The UV index may reach **{round(uv,1)}**, so sun protection is recommended.")
    if air:
        aqi=air.get("current",{}).get("us_aqi")
        label,_=_aqi_label(aqi)
        if aqi is not None: parts.append(f"Air quality is **{label.lower()}** with a US AQI near **{round(aqi)}**.")
    return " ".join(parts)


def _server_next_run(tz_name: str, hh: int, mi: int, cadence: str, weekly_day: int=0) -> datetime:
    now=datetime.now(_tzinfo_from_name(tz_name))
    target=now.replace(hour=hh,minute=mi,second=0,microsecond=0)
    if cadence == "weekly":
        target += timedelta(days=(weekly_day-target.weekday())%7)
        if target <= now: target += timedelta(days=7)
    elif target <= now:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def _alert_category(event: str) -> str:
    e=(event or "").lower()
    if "tornado" in e: return "tornado"
    if any(x in e for x in ("winter","snow","ice","blizzard","freeze")): return "winter"
    if any(x in e for x in ("flood","flash flood")): return "flood"
    if any(x in e for x in ("heat","excessive heat")): return "heat"
    return "storm"

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
            await interaction.followup.send(embeds=self.cog._outlook_embeds(sub, outlook), ephemeral=True)
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


class WeatherRoleLocationModal(discord.ui.Modal, title="Weather alert location"):
    location = discord.ui.TextInput(label="US city, ZIP code, or place", placeholder="Chicago, IL or 60601", max_length=100)

    def __init__(self, cog, owner_id: int, channel_id: int):
        super().__init__()
        self.cog = cog
        self.owner_id = owner_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                loc = await self.cog._resolve_location(session, interaction.user.id, str(self.location.value))
            if loc.get("country_code") != "US":
                return await interaction.followup.send(
                    "Weather-role alerts currently require a US location because they use the National Weather Service.",
                    ephemeral=True,
                )
            description = (
                f"**Channel:** <#{self.channel_id}>\n"
                f"**Location:** {loc['display_name']}\n\n"
                "Choose the lowest severity that should be posted."
            )
            await interaction.edit_original_response(
                embed=discord.Embed(title="🚨 Minimum Alert Severity", description=description, colour=discord.Colour.blurple()),
                view=WeatherRoleSeverityView(self.cog, self.owner_id, self.channel_id, loc),
            )
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Could not resolve that location: {exc}", ephemeral=True)


class WeatherRoleChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Choose the alert channel…", channel_types=[discord.ChannelType.text, discord.ChannelType.news])

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                "I need View Channel, Send Messages, and Embed Links there.", ephemeral=True
            )
        await interaction.response.send_modal(WeatherRoleLocationModal(self.view.cog, self.view.owner_id, channel.id))


class WeatherRoleChannelView(discord.ui.View):
    def __init__(self, cog, owner_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.add_item(WeatherRoleChannelSelect())

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This setup belongs to someone else.", ephemeral=True)
            return False
        return True


class WeatherRoleSeverityView(discord.ui.View):
    def __init__(self, cog, owner_id: int, channel_id: int, loc: Dict[str, Any]):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.channel_id = channel_id
        self.loc = loc

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This setup belongs to someone else.", ephemeral=True)
            return False
        return True

    async def finish(self, interaction, severity):
        guild = interaction.guild
        if not guild.me.guild_permissions.manage_roles:
            return await interaction.response.send_message(
                "I need **Manage Roles** before I can create the opt-in alert roles.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        role_names = {
            "storm": "🌩 Storm Alerts",
            "winter": "🌨 Winter Weather",
            "tornado": "🌪 Tornado Alerts",
            "flood": "🌊 Flood Alerts",
            "heat": "🔥 Heat Alerts",
        }
        ids = {}
        try:
            for key, name in role_names.items():
                role = discord.utils.get(guild.roles, name=name)
                if role is None:
                    role = await guild.create_role(name=name, mentionable=True, reason="Weather Bot alert-role setup")
                ids[key] = role.id
            self.cog.store.set_weather_role_config({
                "guild_id": guild.id,
                "channel_id": self.channel_id,
                "location_name": self.loc["display_name"],
                "latitude": self.loc["latitude"],
                "longitude": self.loc["longitude"],
                "country_code": self.loc.get("country_code"),
                "timezone": self.loc.get("timezone") or DEFAULT_TZ_NAME,
                "min_severity": severity,
                "storm_role_id": ids["storm"],
                "winter_role_id": ids["winter"],
                "tornado_role_id": ids["tornado"],
                "flood_role_id": ids["flood"],
                "heat_role_id": ids["heat"],
                "enabled": 1,
                "created_by": interaction.user.id,
            })
            roles_text = "\n".join(f"<@&{role_id}>" for role_id in ids.values())
            description = (
                f"Alerts for **{self.loc['display_name']}** will post in <#{self.channel_id}>.\n\n"
                f"Created or reused:\n{roles_text}\n\n"
                "Members can use `/weather_role_join` and `/weather_role_leave`."
            )
            await interaction.edit_original_response(
                embed=discord.Embed(title="✅ Weather Roles Ready", description=description, colour=discord.Colour.green()),
                view=None,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I could not create or manage the roles. Put my highest role above the alert roles and grant Manage Roles.",
                ephemeral=True,
            )

    @discord.ui.button(label="Moderate+", style=discord.ButtonStyle.primary)
    async def moderate(self, interaction, _button):
        await self.finish(interaction, "moderate")

    @discord.ui.button(label="Severe+", style=discord.ButtonStyle.danger)
    async def severe(self, interaction, _button):
        await self.finish(interaction, "severe")

    @discord.ui.button(label="All Alerts", style=discord.ButtonStyle.secondary)
    async def all_alerts(self, interaction, _button):
        await self.finish(interaction, "minor")


class WeatherRoleMemberView(discord.ui.View):
    CATEGORIES = (
        ("storm", "Storm", "🌩"),
        ("winter", "Winter", "🌨"),
        ("tornado", "Tornado", "🌪"),
        ("flood", "Flood", "🌊"),
        ("heat", "Heat", "🔥"),
    )

    def __init__(self, cog, owner_id: int, guild: discord.Guild):
        super().__init__(timeout=600)
        self.cog, self.owner_id, self.guild = cog, int(owner_id), guild
        cfg = cog.store.get_weather_role_config(guild.id) or {}
        for category, label, emoji in self.CATEGORIES:
            role_id = cfg.get(f"{category}_role_id")
            role = guild.get_role(int(role_id)) if role_id else None
            if role:
                self.add_item(WeatherRoleToggleButton(category, label, emoji, role))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This role menu belongs to someone else.", ephemeral=True)
            return False
        return True


class WeatherRoleToggleButton(discord.ui.Button):
    def __init__(self, category: str, label: str, emoji: str, role: discord.Role):
        self.category, self.role = category, role
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        joined = self.role in getattr(member, "roles", [])
        try:
            if joined:
                await member.remove_roles(self.role, reason="Weather dashboard role opt-out")
            else:
                await member.add_roles(self.role, reason="Weather dashboard role opt-in")
            await interaction.response.send_message(
                f"✅ {'Removed' if joined else 'Added'} {self.role.mention}.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot manage that role. An admin needs to place my bot role above it and grant Manage Roles.",
                ephemeral=True,
            )




RADAR_SERVICE_URL = "https://mapservices.weather.noaa.gov/eventdriven/rest/services/radar/radar_base_reflectivity_time/ImageServer/exportImage"
RADAR_BASEMAP_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/export"
RADAR_ALLOWED_RANGES = (25, 50, 100, 250)
RADAR_ANIMATION_FRAMES = 8
RADAR_ANIMATION_STEP_MINUTES = 10
RADAR_ANIMATION_DURATION_MS = 550


class RadarLocationModal(discord.ui.Modal, title="Change Radar Location"):
    location = discord.ui.TextInput(
        label="US city, place, or ZIP code",
        placeholder="Chicago, IL or 60601",
        max_length=120,
    )

    def __init__(self, cog, owner_id: int, range_miles: int):
        super().__init__()
        self.cog = cog
        self.owner_id = owner_id
        self.range_miles = range_miles

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This radar menu belongs to someone else.", ephemeral=True)
        await self.cog.send_radar(interaction, str(self.location), self.range_miles, ephemeral=True)


class RadarView(discord.ui.View):
    def __init__(self, cog, owner_id: int, location: Dict[str, Any], range_miles: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.location = dict(location)
        self.range_miles = range_miles

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This radar menu belongs to someone else.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.primary, row=0)
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.send_radar(
            interaction,
            None,
            self.range_miles,
            ephemeral=True,
            resolved_location=self.location,
            edit_message=True,
        )

    @discord.ui.button(label="Animate", emoji="▶️", style=discord.ButtonStyle.success, row=0)
    async def animate(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.send_radar(
            interaction,
            None,
            self.range_miles,
            ephemeral=True,
            resolved_location=self.location,
            edit_message=True,
            animated=True,
        )

    @discord.ui.button(label="Change Location", emoji="📍", style=discord.ButtonStyle.secondary, row=0)
    async def change_location(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(RadarLocationModal(self.cog, self.owner_id, self.range_miles))

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)
    async def remove(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # Component interactions on ephemeral responses are most reliably removed
        # through the interaction webhook rather than Message.delete().
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
            await interaction.delete_original_response()
            self.stop()
            return
        except Exception:
            pass

        # Fallback for clients/messages Discord will not let us delete outright.
        try:
            await interaction.edit_original_response(
                content="Radar closed.",
                embed=None,
                attachments=[],
                view=None,
            )
        except Exception:
            if interaction.message is not None:
                await interaction.message.edit(
                    content="Radar closed.",
                    embed=None,
                    attachments=[],
                    view=None,
                )
        self.stop()

    @discord.ui.select(
        placeholder="Change radar range…",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="25 miles", value="25"),
            discord.SelectOption(label="50 miles", value="50"),
            discord.SelectOption(label="100 miles", value="100"),
            discord.SelectOption(label="250 miles", value="250"),
        ],
        row=1,
    )
    async def change_range(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.range_miles = int(select.values[0])
        await self.cog.send_radar(
            interaction,
            None,
            self.range_miles,
            ephemeral=True,
            resolved_location=self.location,
            edit_message=True,
        )

    @discord.ui.button(label="Forecast", emoji="🌤️", style=discord.ButtonStyle.secondary, row=2)
    async def forecast(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                loc = self.location
                tz = loc.get("timezone") or _get_user_tz_name(self.cog.store, interaction.user.id)
                embed = await self.cog._current_embed(session, loc, _get_user_units(self.cog.store, interaction.user.id), tz)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)

    @discord.ui.button(label="Briefing", emoji="📝", style=discord.ButtonStyle.secondary, row=2)
    async def briefing(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog._send_briefing_command(
            interaction,
            resolved_location=self.location,
            ephemeral=True,
        )

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
        self.server_weather_scheduler.start()

    def cog_unload(self):
        self.weather_scheduler.cancel(); self.wx_alerts_scheduler.cancel(); self.sticky_dashboard_scheduler.cancel(); self.server_weather_scheduler.cancel()

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
            "condition_value": state.condition_value, "condition_unit": _get_user_units(self.store, state.user_id), "enabled": 1, "report_type": state.report_type, "display_style": state.display_style,
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
        if sub.get("report_type", "forecast") == "briefing":
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                try:
                    air = await _fetch_air_quality(session, float(sub["latitude"]), float(sub["longitude"]), sub.get("tz_name") or DEFAULT_TZ_NAME)
                except Exception:
                    air = None
            embed = self._briefing_embed(sub, outlook, air)
            embed.description = "**Test delivery** — this does not affect the next scheduled run.\n\n" + (embed.description or "")
        else:
            embeds = self._outlook_embeds(sub, outlook)
            embeds[0].description = "**Test delivery** — this does not affect the next scheduled run.\n\n" + (embeds[0].description or "")
        if sub.get("destination_type") == "channel":
            channel = self.bot.get_channel(int(sub["channel_id"])) or await self.bot.fetch_channel(int(sub["channel_id"]))
            if sub.get("report_type", "forecast") == "briefing":
                await channel.send(embed=embed)
            else:
                await channel.send(embeds=embeds)
            return channel.mention
        target = self.bot.get_user(int(sub["user_id"])) or await self.bot.fetch_user(int(sub["user_id"]))
        if sub.get("report_type", "forecast") == "briefing":
            await target.send(embed=embed)
        else:
            await target.send(embeds=embeds)
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
        emb.add_field(name="More details", value=f"For logs, screenshots, and longer reports: [Open GitHub Issues]({GITHUB_ISSUES_URL})", inline=False)
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
        await inter.followup.send(
            f"✅ Submitted as **#{rid}**. You’ll be notified when its status changes.\n\n"
            f"For screenshots, logs, or a more detailed report, please also use [GitHub Issues]({GITHUB_ISSUES_URL}) when possible.",
            ephemeral=True,
        )

    def get_user_units(self, user_id: int) -> str:
        return _get_user_units(self.store, user_id)

    def get_user_timezone(self, user_id: int) -> str:
        return _get_user_tz_name(self.store, user_id)

    def dashboard_subscription_rows(self, inter: discord.Interaction) -> List[Dict[str, Any]]:
        rows = self.store.list_weather_subs(inter.user.id)
        if inter.guild and inter.user.guild_permissions.manage_guild:
            known = {r["id"] for r in rows}
            rows += [r for r in self.store.list_weather_subs(guild_id=inter.guild.id) if r["id"] not in known]
        return rows

    def subscription_dashboard_embed(self, rows: List[Dict[str, Any]]) -> discord.Embed:
        lines = []
        for row in rows[:25]:
            dest = f"<#{row['channel_id']}>" if row.get("destination_type") == "channel" else "DM"
            cond = f" · if {row['condition_metric']} {row['condition_operator']} {row['condition_value']}" if row.get("condition_metric") else " · always"
            status = "active" if row.get("enabled", 1) else "paused"
            lines.append(
                f"**#{row['id']}** · {status} · {row.get('report_type','forecast')} · {row['cadence']} at {row['hh']:02d}:{row['mi']:02d}\n"
                f"{row.get('location_name') or row.get('zip')} → {dest}{cond}"
            )
        embed = discord.Embed(title="🔔 Subscription Dashboard", description="\n\n".join(lines), colour=discord.Colour.blurple())
        embed.set_footer(text="Choose a subscription below to test, pause/resume, or delete it.")
        return embed

    def server_dashboard_embed(self, guild: discord.Guild) -> discord.Embed:
        posts = self.store.list_server_weather_posts(guild_id=guild.id)
        sticky = self.store.list_sticky_dashboards(guild_id=guild.id)
        roles = self.store.get_weather_role_config(guild.id)
        embed = discord.Embed(title=f"🏠 Server Weather — {guild.name}", description="Manage server-wide weather delivery and alerts.", colour=discord.Colour.blurple())
        embed.add_field(name="Scheduled posts", value=str(len(posts)), inline=True)
        embed.add_field(name="Sticky dashboards", value=str(len(sticky)), inline=True)
        embed.add_field(name="Weather roles", value="Configured" if roles and roles.get("enabled", 1) else "Not configured", inline=True)
        return embed

    async def open_weather_role_setup(self, inter: discord.Interaction, *, edit: bool = False):
        if not inter.guild or not inter.user.guild_permissions.manage_guild:
            return await inter.response.send_message("You need **Manage Server** to configure weather roles.", ephemeral=True)
        embed = discord.Embed(
            title="🚨 Weather Role Setup",
            description="This existing wizard chooses an alert channel, confirms a US location, selects a minimum severity, and creates the opt-in roles.",
            colour=discord.Colour.blurple(),
        )
        view = WeatherRoleChannelView(self, inter.user.id)
        if edit:
            await inter.response.edit_message(embed=embed, view=view)
        else:
            await inter.response.send_message(embed=embed, view=view, ephemeral=True)

    async def open_roles_panel(self, inter: discord.Interaction, *, edit: bool = False):
        if not inter.guild:
            return await inter.response.send_message("Use `/roles` inside a server.", ephemeral=True)
        cfg = self.store.get_weather_role_config(inter.guild.id)
        if not cfg or not cfg.get("enabled", 1):
            text = "Weather roles are not configured in this server."
            if inter.user.guild_permissions.manage_guild:
                text += " Open `/dashboard` and choose **Server Weather → Role Setup Wizard**."
            return await inter.response.send_message(text, ephemeral=True)
        configured = []
        for category, label, emoji in WeatherRoleMemberView.CATEGORIES:
            rid = cfg.get(f"{category}_role_id")
            role = inter.guild.get_role(int(rid)) if rid else None
            if role:
                configured.append(f"{emoji} {role.mention} — {'joined' if role in inter.user.roles else 'not joined'}")
        embed = discord.Embed(title="🚨 Weather Notification Roles", description="Click a button to join or leave that role.\n\n" + "\n".join(configured), colour=discord.Colour.blurple())
        view = WeatherRoleMemberView(self, inter.user.id, inter.guild)
        if edit:
            await inter.response.edit_message(embed=embed, view=view)
        else:
            await inter.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="dashboard", description="Open the unified Weather Bot dashboard.")
    async def dashboard_cmd(self, inter: discord.Interaction):
        await inter.response.send_message(embed=dashboard_home_embed(self, inter), view=WeatherDashboardView(self, inter.user.id), ephemeral=True)

    @app_commands.command(name="roles", description="Join or leave this server's weather notification roles.")
    async def roles_cmd(self, inter: discord.Interaction):
        await self.open_roles_panel(inter)

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
        temp_unit = "fahrenheit" if units == "standard" else "celsius"
        wind_unit = "mph" if units == "standard" else "kmh"
        precip_unit = "inch" if units == "standard" else "mm"
        deg = "°F" if units == "standard" else "°C"
        distance_unit = "mi" if units == "standard" else "km"
        pressure_unit = "inHg" if units == "standard" else "hPa"

        params = {
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "temperature_unit": temp_unit,
            "wind_speed_unit": wind_unit,
            "precipitation_unit": precip_unit,
            "timezone": tz_name,
            "current": (
                "temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,"
                "pressure_msl,visibility,wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
                "precipitation,weather_code"
            ),
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                "precipitation_probability_max,uv_index_max,sunrise,sunset,wind_speed_10m_max"
            ),
        }
        async with session.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status != 200:
                raise RuntimeError("Weather service unavailable")
            wx = await response.json()

        cur = wx.get("current") or {}
        daily = wx.get("daily") or {}
        current_code = cur.get("weather_code", 0)
        daily_code = (daily.get("weather_code") or [current_code])[0]
        icon, desc = wx_icon_desc(current_code)
        _daily_icon, daily_desc = wx_icon_desc(daily_code)
        temperature = cur.get("temperature_2m")
        apparent = cur.get("apparent_temperature", temperature)
        high = (daily.get("temperature_2m_max") or [None])[0]
        low = (daily.get("temperature_2m_min") or [None])[0]

        def first(key, default=None):
            values = daily.get(key) or []
            return values[0] if values else default

        def cardinal(degrees):
            if degrees is None:
                return "?"
            points = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
            return points[int((float(degrees) + 11.25) // 22.5) % 16]

        def uv_label(value):
            try:
                uv = float(value)
            except (TypeError, ValueError):
                return "Unknown"
            if uv < 3: return "Low"
            if uv < 6: return "Moderate"
            if uv < 8: return "High"
            if uv < 11: return "Very High"
            return "Extreme"

        visibility_m = cur.get("visibility")
        visibility = None
        if visibility_m is not None:
            visibility = float(visibility_m) / (1609.344 if units == "standard" else 1000.0)

        pressure_hpa = cur.get("pressure_msl")
        pressure = None
        if pressure_hpa is not None:
            pressure = float(pressure_hpa) * 0.0295299830714 if units == "standard" else float(pressure_hpa)

        colour_temp_f = None
        if temperature is not None:
            colour_temp_f = float(temperature) if units == "standard" else (float(temperature) * 9 / 5 + 32)

        embed = discord.Embed(
            title=f"{icon} Weather — {loc['display_name']}",
            description=f"**{desc}**",
            colour=wx_color_from_temp_f(colour_temp_f if colour_temp_f is not None else 70),
            timestamp=datetime.now(timezone.utc),
        )
        if temperature is not None:
            embed.add_field(
                name="🌡️ Current",
                value=f"**{round(temperature)}{deg}**\nFeels like **{round(apparent)}{deg}**",
                inline=True,
            )
        if high is not None and low is not None:
            embed.add_field(
                name="📅 Today",
                value=f"High **{round(high)}{deg}**\nLow **{round(low)}{deg}**",
                inline=True,
            )

        rain_chance = first("precipitation_probability_max", "?")
        rain_total = first("precipitation_sum", "?")
        embed.add_field(
            name="🌧️ Precipitation",
            value=f"Chance **{rain_chance}%**\nExpected **{rain_total} {precip_unit}**",
            inline=True,
        )

        # Keep the headline strictly tied to current observations. Daily precipitation
        # severity is summarized after the detailed weather fields so it does not
        # interrupt the scan-friendly forecast layout.
        forecast_note = _precipitation_forecast_text(
            daily_desc,
            daily_code,
            rain_chance if isinstance(rain_chance, (int, float)) else None,
        )

        wind_speed = cur.get("wind_speed_10m", 0)
        wind_gust = cur.get("wind_gusts_10m", 0)
        wind_dir = cardinal(cur.get("wind_direction_10m"))
        embed.add_field(
            name="🌬️ Wind",
            value=f"**{round(wind_speed)} {wind_unit} {wind_dir}**\nGusts **{round(wind_gust)} {wind_unit}**",
            inline=True,
        )

        humidity = cur.get("relative_humidity_2m", "?")
        dew_point = cur.get("dew_point_2m")
        dew_text = f"{round(dew_point)}{deg}" if dew_point is not None else "?"
        embed.add_field(
            name="💧 Humidity",
            value=f"Humidity **{humidity}%**\nDew point **{dew_text}**",
            inline=True,
        )

        uv = first("uv_index_max", "?")
        embed.add_field(
            name="☀️ UV Index",
            value=f"**{uv}** · {uv_label(uv)}",
            inline=True,
        )

        if visibility is not None:
            visibility_text = f"{visibility:.1f} {distance_unit}"
        else:
            visibility_text = "?"
        if pressure is not None:
            pressure_text = f"{pressure:.2f} {pressure_unit}" if units == "standard" else f"{pressure:.0f} {pressure_unit}"
        else:
            pressure_text = "?"
        embed.add_field(
            name="👁️ Atmosphere",
            value=f"Visibility **{visibility_text}**\nPressure **{pressure_text}**",
            inline=True,
        )

        sunrise = first("sunrise")
        sunset = first("sunset")
        def clock(value):
            if not value:
                return "?"
            try:
                return datetime.fromisoformat(value).strftime("%I:%M %p").lstrip("0")
            except Exception:
                return str(value)
        embed.add_field(
            name="🌅 Sun",
            value=f"Sunrise **{clock(sunrise)}**\nSunset **{clock(sunset)}**",
            inline=True,
        )

        if forecast_note:
            embed.add_field(
                name="📝 Forecast Summary",
                value=forecast_note.replace("**", ""),
                inline=False,
            )

        embed.set_footer(text=f"Open-Meteo • Units: {units} • Timezone: {tz_name}")
        return embed


    @staticmethod
    def _radar_bbox(latitude: float, longitude: float, range_miles: int) -> str:
        radius = min(RADAR_ALLOWED_RANGES, key=lambda value: abs(value - int(range_miles)))
        lat_delta = radius / 69.0
        cos_lat = max(0.20, abs(math.cos(math.radians(latitude))))
        lon_delta = radius / (69.172 * cos_lat)
        return f"{longitude-lon_delta:.6f},{latitude-lat_delta:.6f},{longitude+lon_delta:.6f},{latitude+lat_delta:.6f}"

    @staticmethod
    async def _fetch_map_image(session: aiohttp.ClientSession, url: str, params: Dict[str, str]) -> bytes:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
            data = await response.read()
            content_type = (response.headers.get("Content-Type") or "").lower()
            if response.status != 200 or not content_type.startswith("image/") or len(data) < 100:
                raise RuntimeError("Radar imagery is temporarily unavailable.")
            return data

    @staticmethod
    def _compose_radar_frame(basemap_bytes: bytes, radar_bytes: bytes) -> Image.Image:
        try:
            basemap = Image.open(io.BytesIO(basemap_bytes)).convert("RGBA")
            radar = Image.open(io.BytesIO(radar_bytes)).convert("RGBA")
            if radar.size != basemap.size:
                radar = radar.resize(basemap.size)
            composite = Image.alpha_composite(basemap, radar)
            draw = ImageDraw.Draw(composite)
            cx, cy = composite.width // 2, composite.height // 2
            draw.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=(255, 255, 255, 245), outline=(20, 20, 20, 255), width=2)
            draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=(220, 35, 35, 255))
            return composite.convert("RGB")
        except Exception as exc:
            raise RuntimeError("Radar image could not be rendered.") from exc

    async def _radar_common_images(self, session: aiohttp.ClientSession, loc: Dict[str, Any], range_miles: int):
        bbox = self._radar_bbox(float(loc["latitude"]), float(loc["longitude"]), range_miles)
        common = {
            "bbox": bbox,
            "bboxSR": "4326",
            "imageSR": "4326",
            "size": "720,560",
            "f": "image",
        }
        basemap_params = {**common, "format": "png32", "transparent": "false"}
        basemap_bytes = await self._fetch_map_image(session, RADAR_BASEMAP_URL, basemap_params)
        return common, basemap_bytes

    async def _fetch_radar_image(self, session: aiohttp.ClientSession, loc: Dict[str, Any], range_miles: int) -> bytes:
        common, basemap_bytes = await self._radar_common_images(session, loc, range_miles)
        radar_params = {**common, "format": "png32", "transparent": "true"}
        radar_bytes = await self._fetch_map_image(session, RADAR_SERVICE_URL, radar_params)
        composite = self._compose_radar_frame(basemap_bytes, radar_bytes)
        output = io.BytesIO()
        composite.save(output, format="PNG", optimize=True)
        return output.getvalue()

    async def _fetch_radar_animation(self, session: aiohttp.ClientSession, loc: Dict[str, Any], range_miles: int) -> bytes:
        common, basemap_bytes = await self._radar_common_images(session, loc, range_miles)

        # NOAA's time-enabled MRMS service accepts epoch milliseconds. Using a
        # short recent window keeps generation fast and the GIF below Discord's
        # normal upload limits while still clearly showing storm movement.
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        timestamps = [
            now - timedelta(minutes=RADAR_ANIMATION_STEP_MINUTES * offset)
            for offset in reversed(range(RADAR_ANIMATION_FRAMES))
        ]
        radar_requests = [
            self._fetch_map_image(
                session,
                RADAR_SERVICE_URL,
                {
                    **common,
                    "format": "png32",
                    "transparent": "true",
                    "time": str(int(moment.timestamp() * 1000)),
                },
            )
            for moment in timestamps
        ]
        radar_images = await asyncio.gather(*radar_requests)
        frames = [self._compose_radar_frame(basemap_bytes, radar) for radar in radar_images]

        # Adaptive palettes drastically reduce GIF size without making radar
        # colors unreadable. The last frame pauses slightly longer.
        palette_frames = [frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=192) for frame in frames]
        output = io.BytesIO()
        palette_frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=palette_frames[1:],
            duration=[RADAR_ANIMATION_DURATION_MS] * (len(palette_frames) - 1) + [1100],
            loop=0,
            optimize=True,
            disposal=2,
        )
        data = output.getvalue()
        if len(data) > 9_500_000:
            raise RuntimeError("The animated radar exceeded Discord's upload limit. Try a smaller radar range.")
        return data

    async def send_radar(
        self,
        interaction: discord.Interaction,
        location: Optional[str],
        range_miles: int = 100,
        *,
        ephemeral: bool = False,
        resolved_location: Optional[Dict[str, Any]] = None,
        edit_message: bool = False,
        animated: bool = False,
    ):
        range_miles = min(RADAR_ALLOWED_RANGES, key=lambda value: abs(value - int(range_miles)))
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                loc = dict(resolved_location) if resolved_location else await self._resolve_location(session, interaction.user.id, location)
                country = (loc.get("country_code") or "").upper()
                if country not in {"US", "USA"}:
                    raise RuntimeError("Radar currently supports US locations. Worldwide radar can be added through another provider later.")
                image = await (
                    self._fetch_radar_animation(session, loc, range_miles)
                    if animated
                    else self._fetch_radar_image(session, loc, range_miles)
                )

            filename = "radar.gif" if animated else "radar.png"
            file = discord.File(io.BytesIO(image), filename=filename)
            mode_text = "Animated recent radar" if animated else "Latest radar image"
            embed = discord.Embed(
                title=f"🛰️ Radar — {loc['display_name']}",
                description=f"{mode_text} · **{range_miles}-mile range**",
                colour=discord.Colour.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_image(url=f"attachment://{filename}")
            footer = "NOAA/NWS MRMS radar over street map"
            footer += " • 8 recent frames" if animated else " • Refreshes approximately every 5 minutes"
            embed.set_footer(text=footer)
            view = RadarView(self, interaction.user.id, loc, range_miles)

            if edit_message:
                # Ephemeral radar panels cannot reliably be edited through
                # interaction.message.edit(); Discord may return 10008 Unknown Message.
                # Editing the interaction's original response uses the webhook
                # endpoint and works for both ephemeral and normal component messages.
                await interaction.edit_original_response(
                    embed=embed,
                    attachments=[file],
                    view=view,
                )
            else:
                await interaction.followup.send(embed=embed, file=file, view=view, ephemeral=ephemeral)
            self.store.record_event("radar_animation" if animated else "radar_lookup", interaction.user.id, interaction.guild.id if interaction.guild else None)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)

    @app_commands.command(name="radar", description="Show NOAA weather radar for a US location.")
    @app_commands.describe(location="US city, place, ZIP code, or saved location", range_miles="Radar radius around the location")
    @app_commands.choices(range_miles=[
        app_commands.Choice(name="25 miles", value=25),
        app_commands.Choice(name="50 miles", value=50),
        app_commands.Choice(name="100 miles", value=100),
        app_commands.Choice(name="250 miles", value=250),
    ])
    async def radar_cmd(self, inter: discord.Interaction, location: Optional[str] = None, range_miles: Optional[app_commands.Choice[int]] = None):
        await self.send_radar(inter, location, range_miles.value if range_miles else 100)

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
        embed = wizard_embed("What should be delivered?", "Choose a traditional forecast outlook or a plain-language weather briefing.", state)
        await inter.response.send_message(embed=embed, view=ReportTypeView(self, state), ephemeral=True)

    @app_commands.command(name="weather_subscribe_advanced", description="Create a subscription using advanced command options.")
    @app_commands.describe(time="Delivery time, such as 7:00 AM", cadence="daily or weekly", location="City, place, postal code, or saved default", destination="dm or channel", channel="Required for channel delivery", weekly_days="Number of forecast days for weekly reports", metric="max_wind, max_temp, min_temp, rain_chance, precipitation, or uv", operator=">, >=, <, or <=", threshold="Only send when this condition matches")
    async def weather_subscribe_advanced(self, inter: discord.Interaction, time: str, cadence: str="daily", location: Optional[str]=None, destination: str="dm", channel: Optional[discord.TextChannel]=None, weekly_days: app_commands.Range[int,3,10]=7, report_type: str="forecast", display_style: str="automatic", metric: Optional[str]=None, operator: Optional[str]=None, threshold: Optional[float]=None):
        await inter.response.defer(ephemeral=True)
        try:
            cadence=cadence.lower(); destination=destination.lower()
            if cadence not in {"daily","weekly"}: raise ValueError("Cadence must be daily or weekly.")
            if destination not in {"dm","channel"}: raise ValueError("Destination must be dm or channel.")
            report_type=report_type.lower()
            if report_type not in {"forecast","briefing"}: raise ValueError("Report type must be forecast or briefing.")
            display_style=display_style.lower()
            if display_style not in {"automatic","expanded","condensed"}: raise ValueError("Display style must be automatic, expanded, or condensed.")
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
            sub={"user_id":inter.user.id,"zip":"","cadence":cadence,"hh":hh,"mi":mi,"weekly_days":weekly_days,"tz_name":tz_name,"units":_get_user_units(self.store,inter.user.id),"next_run_utc":first.astimezone(timezone.utc).isoformat(),"location_name":loc["display_name"],"latitude":loc["latitude"],"longitude":loc["longitude"],"country_code":loc.get("country_code"),"destination_type":destination,"guild_id":inter.guild.id if destination=="channel" else None,"channel_id":channel.id if channel else None,"created_by":inter.user.id,"condition_metric":metric,"condition_operator":operator,"condition_value":threshold,"condition_unit":_get_user_units(self.store,inter.user.id),"enabled":1,"report_type":report_type,"display_style":display_style}
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
                f"**#{row['id']}** · {status} · {row.get('report_type','forecast')} · {row['cadence']} at {row['hh']:02d}:{row['mi']:02d}\n"
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

    def _briefing_embed(self, sub, outlook, air=None):
        name = sub.get("location_name") or sub.get("zip") or "Saved location"
        emb = discord.Embed(
            title=f"🌤️ Weather Briefing — {name}",
            description=_weather_briefing(name, outlook, sub.get("units") or "standard", air),
            colour=discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        emb.set_footer(text=f"Scheduled in {sub.get('tz_name') or DEFAULT_TZ_NAME} • Units: {sub.get('units') or 'standard'}")
        return emb

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

    def _temperature_colour(self, temp: Optional[float], units: str) -> discord.Colour:
        if temp is None:
            return discord.Colour.blurple()
        temp_f = float(temp) if units == "standard" else (float(temp) * 9 / 5 + 32)
        if temp_f >= 100: return discord.Colour.from_rgb(220, 45, 45)
        if temp_f >= 90: return discord.Colour.from_rgb(239, 105, 44)
        if temp_f >= 80: return discord.Colour.from_rgb(245, 166, 35)
        if temp_f >= 70: return discord.Colour.from_rgb(241, 196, 15)
        if temp_f >= 60: return discord.Colour.from_rgb(111, 191, 115)
        if temp_f >= 50: return discord.Colour.from_rgb(54, 162, 235)
        if temp_f >= 35: return discord.Colour.from_rgb(72, 120, 210)
        return discord.Colour.from_rgb(126, 87, 194)

    def _outlook_summary(self, metrics: Dict[str, Any], units: str) -> str:
        condition = str(metrics.get("condition") or "Conditions unavailable")
        high = metrics.get("max_temp")
        low = metrics.get("min_temp")
        rain = metrics.get("rain_chance")
        wind = metrics.get("max_wind")
        parts = [condition.rstrip(".") + "."]
        if high is not None and low is not None:
            spread = float(high) - float(low)
            if float(high) >= (95 if units == "standard" else 35):
                parts.append("Very hot conditions are expected.")
            elif spread >= (20 if units == "standard" else 11):
                parts.append("Expect a noticeable temperature swing through the day.")
        if rain is not None:
            if float(rain) >= 70: parts.append("Rain is likely.")
            elif float(rain) >= 40: parts.append("Rain is possible.")
            elif float(rain) >= 20: parts.append("A few showers are possible.")
        if wind is not None and float(wind) >= (25 if units == "standard" else 40):
            parts.append("Breezy to windy conditions are possible.")
        return " ".join(parts)

    def _condensed_outlook_embed(self, sub, outlook):
        units = sub.get("units") or "standard"
        temp_unit = "°F" if units == "standard" else "°C"
        wind_unit = "mph" if units == "standard" else "km/h"
        location = sub.get("location_name") or sub.get("zip") or "Saved location"
        tz_name = sub.get("tz_name") or DEFAULT_TZ_NAME
        today = datetime.now(_tzinfo_from_name(tz_name)).date()
        sections = []
        hottest = None

        for row in outlook:
            d, line, sunrise, sunset, uv, *rest = row
            metrics = rest[1] if len(rest) > 1 else (rest[0] if rest and isinstance(rest[0], dict) else {})
            high = metrics.get("max_temp")
            low = metrics.get("min_temp")
            rain = metrics.get("rain_chance")
            wind = metrics.get("max_wind")
            condition = metrics.get("condition") or "Forecast"
            code = metrics.get("weather_code")
            icon, _ = wx_icon_desc(code) if code is not None else ("🌤️", condition)
            if high is not None:
                hottest = float(high) if hottest is None else max(hottest, float(high))
            try:
                date_obj = datetime.fromisoformat(str(d)).date()
                delta = (date_obj - today).days
                day_name = "Today" if delta == 0 else "Tomorrow" if delta == 1 else date_obj.strftime("%A")
                date_label = date_obj.strftime("%b %-d")
            except Exception:
                day_name, date_label = str(d), str(d)

            temp_bits = []
            if high is not None: temp_bits.append(f"**{round(high)}{temp_unit}**")
            if low is not None: temp_bits.append(f"**{round(low)}{temp_unit}**")
            temp_text = " / ".join(temp_bits) if temp_bits else "Temperatures unavailable"
            detail_bits = []
            if rain is not None: detail_bits.append(f"🌧️ {round(rain)}%")
            if wind is not None: detail_bits.append(f"💨 {round(wind)} {wind_unit}")
            if uv is not None: detail_bits.append(f"☀️ UV {round(uv,1)}")
            details = " · ".join(detail_bits)
            sections.append(
                f"**{icon} {day_name} · {date_label}**\n"
                f"{condition} · 🌡️ {temp_text}"
                + (f"\n{details}" if details else "")
            )

        title_prefix = "Daily Outlook" if sub.get("cadence") == "daily" else f"{len(outlook)}-Day Outlook"
        emb = discord.Embed(
            title=f"🌤️ {title_prefix} — {location}",
            description="\n\n".join(sections),
            colour=self._temperature_colour(hottest, units),
        )
        emb.set_footer(text=f"Condensed view • Scheduled in {tz_name} • Units: {units}")
        return emb

    def _outlook_embeds(self, sub, outlook):
        style = (sub.get("display_style") or "automatic").lower()
        use_condensed = style == "condensed" or (style == "automatic" and len(outlook) >= 3)
        if use_condensed:
            return [self._condensed_outlook_embed(sub, outlook)]
        units = sub.get("units") or "standard"
        temp_unit = "°F" if units == "standard" else "°C"
        wind_unit = "mph" if units == "standard" else "km/h"
        precip_unit = "inch" if units == "standard" else "mm"
        location = sub.get("location_name") or sub.get("zip") or "Saved location"
        embeds=[]
        for index, row in enumerate(outlook):
            d,line,sunrise,sunset,uv,*rest = row
            metrics = rest[1] if len(rest) > 1 else (rest[0] if rest and isinstance(rest[0], dict) else {})
            high = metrics.get("max_temp")
            low = metrics.get("min_temp")
            rain = metrics.get("rain_chance")
            precip = metrics.get("precipitation")
            wind = metrics.get("max_wind")
            condition = metrics.get("condition") or "Forecast"
            code = metrics.get("weather_code")
            icon,_ = wx_icon_desc(code) if code is not None else ("🌤️", condition)
            try:
                date_obj = datetime.fromisoformat(str(d)).date()
                today = datetime.now(_tzinfo_from_name(sub.get("tz_name") or DEFAULT_TZ_NAME)).date()
                delta = (date_obj - today).days
                day_name = "Today" if delta == 0 else "Tomorrow" if delta == 1 else date_obj.strftime("%A")
                date_label = date_obj.strftime("%a, %b %-d")
            except Exception:
                day_name, date_label = str(d), str(d)
            title_prefix = "Daily Outlook" if sub.get("cadence") == "daily" else "Weekly Outlook"
            title = f"{title_prefix} — {location}" if index == 0 else f"{icon} {day_name}"
            emb = discord.Embed(
                title=title,
                description=(f"**{icon} {day_name} ({date_label})**\n{self._outlook_summary(metrics, units)}" if index == 0 else f"**{date_label}**\n{self._outlook_summary(metrics, units)}"),
                colour=self._temperature_colour(high, units),
            )
            if high is not None or low is not None:
                temp_bits=[]
                if high is not None: temp_bits.append(f"🔥 High **{round(high)}{temp_unit}**")
                if low is not None: temp_bits.append(f"❄️ Low **{round(low)}{temp_unit}**")
                emb.add_field(name="🌡️ Temperature", value=" • ".join(temp_bits), inline=False)
            precip_bits=[]
            if rain is not None: precip_bits.append(f"**{round(rain)}%** chance")
            if precip is not None and float(precip) > 0: precip_bits.append(f"up to **{precip:.2f} {precip_unit}**")
            if precip_bits: emb.add_field(name="🌧️ Precipitation", value=" • ".join(precip_bits), inline=True)
            if wind is not None: emb.add_field(name="💨 Wind", value=f"Up to **{round(wind)} {wind_unit}**", inline=True)
            if uv is not None: emb.add_field(name="☀️ UV Index", value=f"**{round(uv,1)}**", inline=True)
            sun=[]
            if sunrise: sun.append(f"🌅 {fmt_sun(sunrise)}")
            if sunset: sun.append(f"🌇 {fmt_sun(sunset)}")
            if sun: emb.add_field(name="Sun", value=" • ".join(sun), inline=False)
            emb.set_footer(text=f"Scheduled in {sub.get('tz_name') or DEFAULT_TZ_NAME} • Units: {units}")
            embeds.append(emb)
        return embeds

    def _outlook_embed(self, sub, outlook):
        return self._outlook_embeds(sub, outlook)[0]

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
                        if s.get("report_type", "forecast") == "briefing":
                            try: air=await _fetch_air_quality(session,float(lat),float(lon),s.get("tz_name") or DEFAULT_TZ_NAME)
                            except Exception: air=None
                            emb=self._briefing_embed(s,outlook,air)
                        else:
                            embeds=self._outlook_embeds(s,outlook)
                        if s.get("destination_type")=="channel":
                            ch=self.bot.get_channel(int(s["channel_id"])) or await self.bot.fetch_channel(int(s["channel_id"]))
                            if s.get("report_type", "forecast") == "briefing": await ch.send(embed=emb)
                            else: await ch.send(embeds=embeds)
                        else:
                            user=self.bot.get_user(int(s["user_id"])) or await self.bot.fetch_user(int(s["user_id"]))
                            if s.get("report_type", "forecast") == "briefing": await user.send(embed=emb)
                            else: await user.send(embeds=embeds)
                    tz=_tzinfo_from_name(s.get("tz_name") or DEFAULT_TZ_NAME); nxt=datetime.now(tz).replace(hour=int(s["hh"]),minute=int(s["mi"]),second=0,microsecond=0)+timedelta(days=1 if s["cadence"]=="daily" else 7)
                    self.store.update_weather_sub(s["id"],nxt.astimezone(timezone.utc).isoformat(),failure_count=0,last_error=None,last_result=("sent: " if matched else "not sent: ")+result,last_sent_at=now.isoformat() if matched else s.get("last_sent_at"))
                    self.store.record_event("scheduled_sent" if matched else "scheduled_skipped", s.get("user_id"), s.get("guild_id"))
                except Exception as e:
                    failures=int(s.get("failure_count") or 0)+1; disable=failures>=5
                    self.store.update_weather_sub(s["id"],(now+timedelta(minutes=5)).isoformat(),failure_count=failures,last_error=str(e)[:300],last_result="delivery failed",enabled=0 if disable else 1)
                    self.store.record_event("scheduler_error", s.get("user_id"), s.get("guild_id"))

    @weather_scheduler.before_loop
    async def before_weather(self): await self.bot.wait_until_ready()


    async def _air_embed(self, loc: Dict[str,Any]) -> discord.Embed:
        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
            data=await _fetch_air_quality(session,float(loc["latitude"]),float(loc["longitude"]),loc.get("timezone") or DEFAULT_TZ_NAME)
        current=data["current"]; aqi=current.get("us_aqi"); label,color=_aqi_label(aqi)
        emb=discord.Embed(title=f"🌬️ Air & Pollen — {loc['display_name']}",description=f"**US AQI:** {round(aqi) if aqi is not None else 'Unavailable'} · **{label}**",colour=color,timestamp=datetime.now(timezone.utc))
        emb.add_field(name="Particles",value=f"PM2.5: **{current.get('pm2_5','—')} μg/m³**\nPM10: **{current.get('pm10','—')} μg/m³**",inline=True)
        pol=data["pollen"]; lines=[]
        for key,val in pol.items():
            if val is not None: lines.append(f"{key.replace('_pollen','').title()}: **{_pollen_level(val)}** ({val:.1f})")
        emb.add_field(name="Pollen (next 24h)",value="\n".join(lines) if lines else "Pollen data is unavailable for this location or season.",inline=True)
        emb.set_footer(text="Pollen coverage varies by region and season.")
        return emb

    @app_commands.command(name="air_quality", description="Show air quality and available pollen forecasts for a location.")
    async def air_quality(self, inter: discord.Interaction, location: Optional[str]=None):
        await inter.response.defer()
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session: loc=await self._resolve_location(session,inter.user.id,location)
            await inter.followup.send(embed=await self._air_embed(loc))
            self.store.record_event("air_quality_lookup",inter.user.id,inter.guild.id if inter.guild else None)
        except Exception as exc: await inter.followup.send(f"⚠️ Could not load air quality: {exc}")

    async def _send_briefing_command(
        self,
        inter: discord.Interaction,
        location: Optional[str] = None,
        resolved_location: Optional[Dict[str, Any]] = None,
        ephemeral: bool = False,
    ):
        await inter.response.defer(ephemeral=ephemeral)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
                loc = dict(resolved_location) if resolved_location is not None else await self._resolve_location(
                    session,
                    inter.user.id,
                    location,
                )
                units = _get_user_units(self.store, inter.user.id)
                tz = loc.get("timezone") or _get_user_tz_name(self.store, inter.user.id)
                outlook = await _fetch_outlook(session, float(loc["latitude"]), float(loc["longitude"]), 2, tz, units)
                try:
                    air = await _fetch_air_quality(session, float(loc["latitude"]), float(loc["longitude"]), tz)
                except Exception:
                    air = None
            temp_f = outlook[0][5] if units == "standard" else (outlook[0][5] * 9 / 5 + 32 if outlook[0][5] is not None else None)
            emb = discord.Embed(
                title=f"🌤️ Weather Briefing — {loc['display_name']}",
                description=_weather_briefing(loc["display_name"], outlook, units, air),
                colour=wx_color_from_temp_f(temp_f),
                timestamp=datetime.now(timezone.utc),
            )
            await inter.followup.send(embed=emb, ephemeral=ephemeral)
            self.store.record_event("weather_briefing", inter.user.id, inter.guild.id if inter.guild else None)
        except Exception as exc:
            await inter.followup.send(f"⚠️ Could not create the briefing: {exc}", ephemeral=ephemeral)

    @app_commands.command(name="brief", description="Quick plain-language weather, air-quality, and pollen briefing.")
    async def brief_cmd(self, inter: discord.Interaction, location: Optional[str] = None):
        await self._send_briefing_command(inter, location)

    @app_commands.command(name="weather_briefing", description="Legacy alias for /brief.")
    async def weather_briefing(self, inter: discord.Interaction, location: Optional[str] = None):
        await self._send_briefing_command(inter, location)

    @app_commands.command(name="server_weather_post_create", description="Create a daily or weekly server weather briefing.")
    @app_commands.describe(channel="Destination channel", cadence="daily or weekly", time="Local time such as 7:00am", location="City, place, postal code, or saved default", weekly_day="0=Monday through 6=Sunday")
    async def server_weather_post_create(self, inter: discord.Interaction, channel: discord.TextChannel, cadence: str, time: str, location: Optional[str]=None, weekly_day: app_commands.Range[int,0,6]=0):
        if not inter.guild or not inter.user.guild_permissions.manage_guild: return await inter.response.send_message("You need **Manage Server**.",ephemeral=True)
        cadence=cadence.lower()
        if cadence not in {"daily","weekly"}: return await inter.response.send_message("Cadence must be `daily` or `weekly`.",ephemeral=True)
        perms=channel.permissions_for(inter.guild.me)
        if not(perms.view_channel and perms.send_messages and perms.embed_links): return await inter.response.send_message("I need View Channel, Send Messages, and Embed Links there.",ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            hh,mi=_parse_time(time)
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session: loc=await self._resolve_location(session,inter.user.id,location)
            tz=loc.get("timezone") or _get_user_tz_name(self.store,inter.user.id); units=_get_user_units(self.store,inter.user.id); nxt=_server_next_run(tz,hh,mi,cadence,weekly_day)
            pid=self.store.add_server_weather_post({"guild_id":inter.guild.id,"channel_id":channel.id,"created_by":inter.user.id,"location_name":loc["display_name"],"latitude":loc["latitude"],"longitude":loc["longitude"],"country_code":loc.get("country_code"),"timezone":tz,"units":units,"cadence":cadence,"hh":hh,"mi":mi,"weekly_day":weekly_day,"include_air":1,"include_pollen":1,"enabled":1,"next_run_utc":nxt.isoformat()})
            await inter.followup.send(f"✅ Server weather post **#{pid}** will run {cadence} in {channel.mention}. Next run: <t:{int(nxt.timestamp())}:F>.",ephemeral=True)
        except Exception as exc: await inter.followup.send(f"⚠️ Could not create server post: {exc}",ephemeral=True)

    @app_commands.command(name="server_weather_posts", description="List this server's scheduled weather posts.")
    async def server_weather_posts(self, inter: discord.Interaction):
        if not inter.guild or not inter.user.guild_permissions.manage_guild:return await inter.response.send_message("You need **Manage Server**.",ephemeral=True)
        rows=self.store.list_server_weather_posts(guild_id=inter.guild.id)
        text="\n".join(f"**#{r['id']}** · <#{r['channel_id']}> · {r['cadence']} at {r['hh']:02d}:{r['mi']:02d} · {r['location_name']} · {'active' if r['enabled'] else 'paused'}" for r in rows) or "No scheduled server posts."
        await inter.response.send_message(embed=discord.Embed(title="Server Weather Posts",description=text,colour=discord.Colour.blurple()),ephemeral=True)

    @app_commands.command(name="server_weather_post_delete", description="Delete a scheduled server weather post.")
    async def server_weather_post_delete(self, inter: discord.Interaction, post_id: int):
        if not inter.guild or not inter.user.guild_permissions.manage_guild:return await inter.response.send_message("You need **Manage Server**.",ephemeral=True)
        ok=self.store.remove_server_weather_post(post_id,inter.guild.id); await inter.response.send_message("✅ Deleted." if ok else "Post not found in this server.",ephemeral=True)

    @app_commands.command(name="weather_roles_setup", description="Open the guided weather-role setup wizard (US alerts).")
    async def weather_roles_setup(self, inter: discord.Interaction):
        await self.open_weather_role_setup(inter)

    @app_commands.command(name="weather_roles_setup_advanced", description="Advanced: configure existing opt-in weather alert roles (US alerts).")
    async def weather_roles_setup_advanced(self, inter: discord.Interaction, channel: discord.TextChannel, location: str, storm_role: Optional[discord.Role]=None, winter_role: Optional[discord.Role]=None, tornado_role: Optional[discord.Role]=None, flood_role: Optional[discord.Role]=None, heat_role: Optional[discord.Role]=None, min_severity: str="moderate"):
        if not inter.guild or not inter.user.guild_permissions.manage_guild:return await inter.response.send_message("You need **Manage Server**.",ephemeral=True)
        await inter.response.defer(ephemeral=True)
        try:
            async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session: loc=await self._resolve_location(session,inter.user.id,location)
            if loc.get("country_code") != "US": return await inter.followup.send("Server role alerts currently use the US National Weather Service and require a US location.",ephemeral=True)
            self.store.set_weather_role_config({"guild_id":inter.guild.id,"channel_id":channel.id,"location_name":loc["display_name"],"latitude":loc["latitude"],"longitude":loc["longitude"],"country_code":loc.get("country_code"),"timezone":loc.get("timezone") or DEFAULT_TZ_NAME,"min_severity":min_severity.lower(),"storm_role_id":storm_role.id if storm_role else None,"winter_role_id":winter_role.id if winter_role else None,"tornado_role_id":tornado_role.id if tornado_role else None,"flood_role_id":flood_role.id if flood_role else None,"heat_role_id":heat_role.id if heat_role else None,"enabled":1,"created_by":inter.user.id})
            await inter.followup.send("✅ Weather roles configured. Members can use `/weather_role_join` and `/weather_role_leave`.",ephemeral=True)
        except Exception as exc: await inter.followup.send(f"⚠️ Setup failed: {exc}",ephemeral=True)

    async def _change_weather_role(self, inter: discord.Interaction, category: str, add: bool):
        if not inter.guild:return await inter.response.send_message("Use this command in a server.",ephemeral=True)
        cfg=self.store.get_weather_role_config(inter.guild.id); category=category.lower()
        if not cfg or category not in {"storm","winter","tornado","flood","heat"}:return await inter.response.send_message("That weather role is not configured.",ephemeral=True)
        rid=cfg.get(f"{category}_role_id"); role=inter.guild.get_role(int(rid)) if rid else None
        if not role:return await inter.response.send_message(f"The {category} role is not configured.",ephemeral=True)
        try:
            if add: await inter.user.add_roles(role,reason="Weather alert opt-in")
            else: await inter.user.remove_roles(role,reason="Weather alert opt-out")
            await inter.response.send_message(f"✅ {'Added' if add else 'Removed'} {role.mention}.",ephemeral=True)
        except discord.Forbidden: await inter.response.send_message("I cannot manage that role. Move my bot role above it and grant Manage Roles.",ephemeral=True)

    @app_commands.command(name="weather_role_join", description="Join an opt-in server weather alert role.")
    async def weather_role_join(self, inter: discord.Interaction, category: str): await self._change_weather_role(inter,category,True)
    @app_commands.command(name="weather_role_leave", description="Leave an opt-in server weather alert role.")
    async def weather_role_leave(self, inter: discord.Interaction, category: str): await self._change_weather_role(inter,category,False)

    async def _send_server_weather_post(self,row:Dict[str,Any]):
        async with aiohttp.ClientSession(headers=HTTP_HEADERS) as session:
            days=7 if row['cadence']=='weekly' else 2
            outlook=await _fetch_outlook(session,row['latitude'],row['longitude'],days,row['timezone'],row['units'])
            try: air=await _fetch_air_quality(session,row['latitude'],row['longitude'],row['timezone']) if row.get('include_air') else None
            except Exception: air=None
        emb=discord.Embed(title=("📅 Weekly Weather Outlook" if row['cadence']=='weekly' else "🌤️ Daily Weather Briefing")+f" — {row['location_name']}",description=_weather_briefing(row['location_name'],outlook,row['units'],air),colour=discord.Colour.blurple(),timestamp=datetime.now(timezone.utc))
        for d,line,*_ in outlook[:7]: emb.add_field(name=datetime.fromisoformat(d).strftime('%A, %b %d'),value=line,inline=False)
        if air:
            aqi=air['current'].get('us_aqi'); label,_=_aqi_label(aqi); pollen=[f"{k.replace('_pollen','').title()}: {_pollen_level(v)}" for k,v in air['pollen'].items() if v is not None]
            emb.add_field(name="Air quality",value=f"US AQI **{round(aqi) if aqi is not None else '—'}** · {label}",inline=True)
            emb.add_field(name="Pollen",value="\n".join(pollen[:6]) or "Unavailable",inline=True)
        channel=self.bot.get_channel(int(row['channel_id'])) or await self.bot.fetch_channel(int(row['channel_id'])); await channel.send(embed=emb)

    @tasks.loop(minutes=1)
    async def server_weather_scheduler(self):
        now=datetime.now(timezone.utc)
        for row in self.store.list_server_weather_posts(due_before=now.isoformat(),enabled_only=True):
            try:
                await self._send_server_weather_post(row)
                nxt=_server_next_run(row['timezone'],row['hh'],row['mi'],row['cadence'],row['weekly_day'])
                self.store.update_server_weather_post(row['id'],next_run_utc=nxt.isoformat(),last_sent_at=now.isoformat(),last_error=None); self.store.record_event('server_weather_post',row['created_by'],row['guild_id'])
            except Exception as exc:
                self.store.update_server_weather_post(row['id'],last_error=str(exc)[:300],next_run_utc=(now+timedelta(minutes=15)).isoformat()); self.store.record_event('server_weather_error',row['created_by'],row['guild_id'])

    @server_weather_scheduler.before_loop
    async def before_server_weather(self): await self.bot.wait_until_ready()

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
            # Server role alerts use the same NWS source and are deduplicated per guild.
            for cfg in self.store.list_weather_role_configs(enabled_only=True):
                try:
                    alerts=await self._fetch_nws_alerts(session,cfg["latitude"],cfg["longitude"]); fresh=[]
                    order={"minor":0,"moderate":1,"severe":2,"extreme":3}; minimum=order.get((cfg.get("min_severity") or "moderate").lower(),1)
                    for a in alerts:
                        aid=a.get("id") or a.get("@id"); sev=(a.get("severity") or "minor").lower()
                        if aid and order.get(sev,0)>=minimum and not self.store.server_alert_seen(cfg["guild_id"],aid): fresh.append(a)
                    if not fresh: continue
                    channel=self.bot.get_channel(int(cfg["channel_id"])) or await self.bot.fetch_channel(int(cfg["channel_id"]))
                    for a in fresh[:10]:
                        category=_alert_category(a.get("event")); rid=cfg.get(f"{category}_role_id") or cfg.get("storm_role_id"); mention=f"<@&{rid}>" if rid else None
                        emb=discord.Embed(title=f"⚠️ {a.get('event','Weather Alert')} — {cfg['location_name']}",description=(a.get("headline") or a.get("description") or "Details unavailable")[:4000],colour=discord.Colour.orange())
                        emb.add_field(name="Severity",value=a.get("severity","Unknown"),inline=True); emb.add_field(name="Category",value=category.title(),inline=True)
                        await channel.send(content=mention,embed=emb,allowed_mentions=discord.AllowedMentions(roles=True))
                        self.store.mark_server_alert_seen(cfg["guild_id"],a.get("id") or a.get("@id")); self.store.record_event("server_weather_alert",guild_id=cfg["guild_id"])
                except Exception: continue

    @wx_alerts_scheduler.before_loop
    async def before_alerts(self): await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Weather(bot, store=getattr(bot,"store",None)))
