# Weather Bot

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

## Quick start

```bash
cp .env.example .env
# edit .env and add DISCORD_TOKEN, BOT_OWNER_ID, and optionally FEEDBACK_CHANNEL_ID
docker compose up -d --build
```

Data is persisted in `./data/wxbot.sqlite3`.

The invite should include the `bot` and `applications.commands` scopes. For server forecast channels, the bot needs **View Channel**, **Send Messages**, and **Embed Links**. The administrator creating a channel subscription needs **Manage Server**.

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

```text
/weather_subscribe time:7:00am cadence:daily
/weather_subscribe time:6:30am cadence:daily destination:channel channel:#weather
/weather_subscribe time:7:00am cadence:daily metric:max_wind operator:> threshold:20
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

- `/weather_subscriptions`
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
