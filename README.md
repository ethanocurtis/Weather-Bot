# Weather Bot v2.0.1

A Dockerized Discord weather bot with international location search, personal and server forecasts, conditional reports, US NWS alerts, and trackable user feedback.

## Highlights

- Search by city, place name, or postal code worldwide
- Saved default locations with automatic local timezone detection
- Current and hourly weather through Open-Meteo
- Daily or weekly subscriptions delivered by DM or to a server channel
- Optional thresholds such as `max_wind > 20`, `rain_chance >= 60`, or `min_temp < 32`
- Persistent feature, bug, and feedback requests with IDs and status notifications
- US National Weather Service alert DMs
- SQLite storage with automatic migration of the previous ZIP/DM subscription schema

## Version 2.3.3 radar remove fix

- Fixed the radar **Remove** button for ephemeral radar panels.
- The button now deletes the original interaction response through Discord's webhook API, with a safe close-panel fallback.
- Retains the v2.3.2 radar briefing location fix.

## Version 2.3.2 radar briefing fix

The **Briefing** button on radar panels now reuses the radar panel's already-resolved coordinates instead of trying to geocode its formatted display name again. Radar briefings now open reliably and remain private to the user.

## Version 2.3.1 radar fixes

Radar images now include a street-map basemap beneath the NOAA reflectivity layer. Range changes and refreshes update the same panel without re-geocoding the location, and the radar panel includes a **Remove** button.

## Version 2.0.1

### Owner analytics

`/owner_analytics` opens an owner-only control panel showing:

- Current server count
- Known users and saved locations
- Active personal and server subscriptions
- Active sticky dashboards
- Weather lookups, scheduled reports, threshold skips, and scheduler errors recorded today
- Pending feature requests and bug reports

Set `BOT_OWNER_ID` in `.env` to restrict this command. Event-based analytics begin accumulating after the 2.0.1 upgrade; aggregate totals are available immediately.

### Sticky weather dashboards

Server administrators can create a single live weather message that updates in place instead of posting repeated messages:

```text
/sticky_weather_create channel:#weather location:Auckland, New Zealand refresh_minutes:15
/sticky_weather_list
/sticky_weather_delete dashboard_id:1
```

The dashboard includes **Hourly**, **7-Day**, and **Refresh** buttons. The automatic refresh interval can be set from 5 to 60 minutes. Creating or deleting one requires **Manage Server**. The bot needs **View Channel**, **Send Messages**, **Embed Links**, and **Read Message History** in the target channel.

Existing databases migrate automatically by adding the dashboard and analytics tables. Back up `data/wxbot.sqlite3` before upgrading.

## Quick start

```bash
cp .env.example .env
# edit .env and add DISCORD_TOKEN, BOT_OWNER_ID, and optionally FEEDBACK_CHANNEL_ID
docker compose up -d --build
```

Data is persisted in `./data/wxbot.sqlite3`.

The invite should include the `bot` and `applications.commands` scopes. For server forecast channels, the bot needs **View Channel**, **Send Messages**, and **Embed Links**. The administrator creating a channel subscription needs **Manage Server**.

## What's new in this release

- Interactive `/help` menu organized by topic
- Guided `/weather_subscribe` wizard with DM/server destination, saved or searched location, daily/weekly schedule, and optional threshold
- `/weather_subscribe_advanced` preserves the original all-options command for power users
- Interactive `/weather_subscriptions` dashboard with test delivery, pause/resume, and delete controls
- Permission checks before server-channel subscriptions are created
- Test delivery immediately after creating a subscription

## Main commands

### Locations and forecasts

- `/location_set location:<city, postal code, or place> [name]`
- `/locations`
- `/weather [location]`
- `/hourly [location] [hours:6-24]`
- `/moon [location]`
- `/weather_set_zip <zip>` — retained for US compatibility
- `/units <standard|metric>`
- `/timezone <IANA timezone>`
- `/settings`

Examples:

```text
/location_set location:Auckland, New Zealand
/location_set location:SW1A 1AA, United Kingdom
/weather location:Christchurch, NZ
```

### Forecast subscriptions

Run `/weather_subscribe` to open the guided setup wizard. The wizard asks for:

1. DM or server-channel delivery
2. A saved location or a new location search
3. Daily or weekly cadence
4. Delivery time
5. Always-send behavior or an optional threshold
6. Final confirmation

The original command-style flow remains available as `/weather_subscribe_advanced`:

```text
/weather_subscribe_advanced time:7:00am cadence:daily
/weather_subscribe_advanced time:6:30am cadence:daily destination:channel channel:#weather
/weather_subscribe_advanced time:7:00am cadence:daily metric:max_wind operator:> threshold:20
```

Supported threshold metrics:

- `max_wind`
- `max_temp`
- `min_temp`
- `rain_chance`
- `precipitation`
- `uv`

The threshold uses the subscription's saved unit system. A skipped conditional report is recorded in `/weather_subscriptions`, so users can see that the scheduler evaluated it successfully.

Management commands:

- `/weather_subscriptions` — interactive test, pause/resume, and delete dashboard
- `/weather_unsubscribe <sub_id>`

Server subscriptions belong to the guild operationally: members with **Manage Server** can list and remove subscriptions for the current server.

After five consecutive delivery failures, a subscription is automatically paused rather than retrying forever.

### Feedback tracking

Users submit through:

- `/feature <message>`
- `/bug <message>`
- `/feedback <message>`
- `/my_requests`

Each submission receives a persistent request number. The configured owner can update it using buttons in the feedback channel or:

```text
/request_update request_id:184 status:completed note:Released in v2.0
```

The requester is notified by DM when the status changes. Their status remains visible in `/my_requests` if DMs are blocked.

### US weather alerts

- `/wx_alerts mode:on min_severity:moderate`
- `/wx_alerts mode:off`

NWS alerts are available only for saved locations in the United States. International forecasts and subscriptions continue to work normally.

## Upgrading from the previous version

The application performs additive SQLite migrations at startup. Existing `weather_zips`, notes, and `weather_subs` rows are preserved.

- Existing ZIP users are converted to a geocoded default location when they next use a location-aware command.
- Existing subscriptions continue as DM subscriptions.
- New subscription fields default safely, so the database does not need to be deleted.

Back up `./data/wxbot.sqlite3` before deployment as normal operational practice.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Project layout

```text
Weather-Bot-main/
├── main.py
├── weather.py
├── weather_store.py
├── weather_ui.py
├── location_service.py
├── tests/
├── Dockerfile
├── docker-compose.yml
└── data/
```

## APIs

- Open-Meteo Geocoding API — location search
- Open-Meteo Forecast API — forecasts
- National Weather Service API — US alerts
- Astral — moon phases

## Version 2.1 server features

### Weather briefings and air quality

- `/weather_briefing [location]` creates a plain-language summary of the day's temperature, rain, wind, UV, and available air quality.
- `/air_quality [location]` shows US AQI, PM2.5, PM10, and pollen data when the provider has coverage.
- Pollen coverage varies by region and season. Open-Meteo currently provides its pollen variables primarily for Europe during pollen season.

### Daily and weekly server posts

Administrators with **Manage Server** can schedule polished forecast briefings in a channel:

- `/server_weather_post_create`
- `/server_weather_posts`
- `/server_weather_post_delete`

The creation command accepts `daily` or `weekly`, a local delivery time, a destination channel, and an optional location. Weekly days use `0` for Monday through `6` for Sunday. Reports include a forecast briefing, daily outlook rows, air quality, and pollen when available.

### Opt-in weather roles

Use `/weather_roles_setup` to connect optional server roles to US National Weather Service alerts. Supported categories are:

- `storm`
- `winter`
- `tornado`
- `flood`
- `heat`

Members opt in and out with `/weather_role_join` and `/weather_role_leave`. The bot needs **Manage Roles**, and its highest role must be above every alert role it manages. Server role alerts currently require a US location because they use the National Weather Service alert feed.


## v2.1.1 usability additions

- `/weather_roles_setup` launches a guided setup and automatically creates recommended roles.
- `/weather_roles_setup_advanced` remains available for servers that already have custom roles.
- `/weather_subscribe` now starts by choosing **Forecast Outlook** or **Weather Briefing**.
- Briefing subscriptions include a plain-language forecast plus air-quality and available pollen context.
- `/help` now documents sticky dashboards, server posts, weather roles, air and pollen, briefings, alerts, feedback, and owner analytics.

## Unified dashboard (v2.2)

The primary command layout is now:

- `/weather` — quick current weather lookup
- `/brief` — quick plain-language weather, air-quality, and pollen briefing
- `/dashboard` — personal and server weather management
- `/roles` — join or leave configured server weather roles
- `/help` — feature documentation

The dashboard builds on the existing subscription and weather-role setup wizards instead of introducing duplicate setup flows. Existing configuration commands remain available as compatibility shortcuts.

> Discord slash commands cannot simultaneously provide an executable `/weather` command and a `/weather dashboard` subcommand. The bot keeps `/weather` for quick access and uses `/dashboard` for management.


## Radar

Use `/radar` for a current NOAA/NWS MRMS base-reflectivity image around a US location. The command uses your saved default location when no location is provided and supports 25, 50, 100, and 250-mile ranges. Radar is also available from `/dashboard` and documented in `/help`.

Radar panels now include an **Animate** button that generates an eight-frame loop from recent NOAA/NWS MRMS imagery. Static radar remains the fast default, while animation can be generated on demand. Radar is currently US-focused; the provider layer remains isolated for future worldwide providers.


## v2.4.0

### Animated radar

- Added an **Animate** button to radar panels.
- Generates an eight-frame looping GIF showing recent storm movement.
- Reuses the selected location and range without another geocoding request.
- Keeps Refresh, Change Location, Forecast, Briefing, and Remove controls.
- Records animated radar usage separately in owner analytics.

### Richer current weather

The `/weather` result and radar panel's Forecast button now include:

- Current and feels-like temperatures
- Daily high and low
- Precipitation chance and expected amount
- Wind direction, speed, and gusts
- Humidity and dew point
- UV index with risk category
- Visibility and atmospheric pressure
- Local sunrise and sunset

Both standard and metric unit preferences are supported.

## v2.4.1

- Live weather headlines now use the current observed weather code rather than the day's most severe forecast condition.
- Added a separate forecast note when rain, snow, or storms are possible later in the day.
- Briefings now combine precipitation probability with type and intensity, avoiding contradictory wording such as “heavy rain” followed by “rain unlikely.”


## v2.4.3

- Fixed radar animation and refresh updates on ephemeral panels returning Discord error 10008 (`Unknown Message`).
- Radar panels now update through the interaction webhook, preserving all existing controls and attachments.

## v2.4.2

- Moved the precipitation forecast interpretation to the bottom of current-weather embeds.
- Renamed **Forecast Note** to **Forecast Summary** for a clearer, more intentional layout.
- Current observations and detailed metrics now remain grouped together without interruption.

## v2.5.0

- Redesigned scheduled Daily and Weekly Outlooks into cleaner, easy-to-scan day cards.
- Added plain-language daily summaries with relative day labels such as Today and Tomorrow.
- Separated temperature, precipitation, wind, UV, and sun times into readable sections.
- Each day's Discord embed accent bar now changes color based on that day's forecast high temperature.
