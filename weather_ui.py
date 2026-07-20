from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import discord


HELP_PAGES = {
    "weather": ("🌤️ Weather & Forecasts", "`/weather` current conditions · `/hourly` hourly forecast · `/moon` moon phase · `/weather_briefing` plain-language weather, air, and pollen summary."),
    "air": ("🌬️ Air & Pollen", "Use `/air_quality` for US AQI, particulate levels, and available pollen forecasts. Pollen availability varies by region and season."),
    "locations": ("📍 Locations", "`/location_set` saves a worldwide city, place, or postal code. `/locations` lists saved locations. `/weather_set_zip` remains available for legacy US ZIP setup."),
    "subscriptions": ("🔔 Personal & Channel Subscriptions", "Use `/weather_subscribe` for the guided wizard. Choose either a forecast outlook or a weather briefing, then destination, location, schedule, and optional threshold. `/weather_subscriptions` manages existing subscriptions; `/weather_subscribe_advanced` is the power-user fallback."),
    "server_posts": ("📅 Scheduled Server Posts", "Admins can create dedicated daily or weekly channel briefings with `/server_weather_post_create`, list them with `/server_weather_posts`, and remove them with `/server_weather_post_delete`."),
    "roles": ("🚨 Weather Roles", "Admins use `/weather_roles_setup` for the guided US alert-role wizard. Members then opt in with `/weather_role_join` and opt out with `/weather_role_leave`. The bot needs Manage Roles and must sit above the alert roles."),
    "sticky": ("📌 Sticky Dashboard", "Admins can create an automatically refreshed weather message with `/sticky_weather_create`, list dashboards with `/sticky_weather_list`, and delete one with `/sticky_weather_delete`."),
    "alerts": ("⚠️ Personal Alerts", "Use `/wx_alerts mode:on` for active US National Weather Service alerts by DM. NWS alerts require a saved US location."),
    "settings": ("⚙️ Settings", "`/units` changes standard or metric units, `/timezone` controls scheduling, and `/settings` reviews your preferences."),
    "feedback": ("💬 Feedback & Requests", "Use `/feature`, `/bug`, or `/feedback`. `/my_requests` shows status updates. Owners can use `/request_update`."),
    "owner": ("📊 Owner Tools", "The configured bot owner can open `/owner_analytics` for live guild totals, stored usage totals, scheduler activity, and request counts."),
}



class OwnedView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
            return False
        return True


class HelpSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Choose a help topic…",
            options=[
                discord.SelectOption(label="Weather & Forecasts", value="weather", emoji="🌤️"),
                discord.SelectOption(label="Air & Pollen", value="air", emoji="🌬️"),
                discord.SelectOption(label="Locations", value="locations", emoji="📍"),
                discord.SelectOption(label="Subscriptions", value="subscriptions", emoji="🔔"),
                discord.SelectOption(label="Scheduled Server Posts", value="server_posts", emoji="📅"),
                discord.SelectOption(label="Weather Roles", value="roles", emoji="🚨"),
                discord.SelectOption(label="Sticky Dashboard", value="sticky", emoji="📌"),
                discord.SelectOption(label="Personal Alerts", value="alerts", emoji="⚠️"),
                discord.SelectOption(label="Settings", value="settings", emoji="⚙️"),
                discord.SelectOption(label="Feedback", value="feedback", emoji="💬"),
                discord.SelectOption(label="Owner Tools", value="owner", emoji="📊"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        title, body = HELP_PAGES[self.values[0]]
        embed = discord.Embed(title=title, description=body, colour=discord.Colour.blurple())
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(OwnedView):
    def __init__(self, cog, owner_id: int):
        super().__init__(owner_id)
        self.cog = cog
        self.add_item(HelpSelect())

    @discord.ui.button(label="Create Subscription", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def create_subscription(self, interaction: discord.Interaction, _button: discord.ui.Button):
        state = SubscriptionDraft(user_id=interaction.user.id)
        embed = wizard_embed("What should be delivered?", "Choose a traditional forecast outlook or a plain-language weather briefing.", state)
        await interaction.response.edit_message(embed=embed, view=ReportTypeView(self.cog, state))


@dataclass
class SubscriptionDraft:
    user_id: int
    destination_type: Optional[str] = None
    guild_id: Optional[int] = None
    channel_id: Optional[int] = None
    channel_mention: Optional[str] = None
    location: Optional[Dict[str, Any]] = None
    cadence: Optional[str] = None
    hh: Optional[int] = None
    mi: Optional[int] = None
    weekly_days: int = 7
    condition_metric: Optional[str] = None
    condition_operator: Optional[str] = None
    condition_value: Optional[float] = None
    report_type: str = "forecast"


def wizard_embed(title: str, description: str, state: SubscriptionDraft) -> discord.Embed:
    embed = discord.Embed(title=f"🔔 Subscription Setup — {title}", description=description, colour=discord.Colour.blurple())
    details = []
    details.append(f"**Report:** {'Plain-language briefing' if state.report_type == 'briefing' else 'Forecast outlook'}")
    if state.destination_type:
        details.append(f"**Destination:** {state.channel_mention if state.destination_type == 'channel' else 'My DMs'}")
    if state.location:
        details.append(f"**Location:** {state.location['display_name']}")
    if state.cadence:
        details.append(f"**Schedule:** {state.cadence.title()}" + (f" at {state.hh:02d}:{state.mi:02d}" if state.hh is not None else ""))
    if state.condition_metric:
        details.append(f"**Condition:** {state.condition_metric.replace('_', ' ').title()} {state.condition_operator} {state.condition_value:g}")
    elif state.cadence:
        details.append("**Condition:** Always send")
    if details:
        embed.add_field(name="Current choices", value="\n".join(details), inline=False)
    embed.set_footer(text="This setup menu expires after 5 minutes.")
    return embed


class ReportTypeView(OwnedView):
    def __init__(self, cog, state: SubscriptionDraft):
        super().__init__(state.user_id)
        self.cog, self.state = cog, state

    @discord.ui.button(label="Forecast Outlook", emoji="📅", style=discord.ButtonStyle.primary)
    async def forecast(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.state.report_type = "forecast"
        await interaction.response.edit_message(
            embed=wizard_embed("Where should this report go?", "Choose a personal DM or a server channel.", self.state),
            view=DestinationView(self.cog, self.state),
        )

    @discord.ui.button(label="Weather Briefing", emoji="🌤️", style=discord.ButtonStyle.success)
    async def briefing(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.state.report_type = "briefing"
        await interaction.response.edit_message(
            embed=wizard_embed("Where should this briefing go?", "Choose a personal DM or a server channel.", self.state),
            view=DestinationView(self.cog, self.state),
        )


class DestinationView(OwnedView):
    def __init__(self, cog, state: SubscriptionDraft):
        super().__init__(state.user_id)
        self.cog, self.state = cog, state

    @discord.ui.button(label="My DMs", emoji="📨", style=discord.ButtonStyle.primary)
    async def dm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.state.destination_type = "dm"
        await show_location_step(interaction, self.cog, self.state)

    @discord.ui.button(label="Server Channel", emoji="🏠", style=discord.ButtonStyle.secondary)
    async def channel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Server channel subscriptions must be created inside a server.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need **Manage Server** to create a server subscription.", ephemeral=True)
        embed = wizard_embed("Choose a channel", "Select the channel where scheduled forecasts should be posted.", self.state)
        await interaction.response.edit_message(embed=embed, view=ChannelPickView(self.cog, self.state))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(content="Subscription setup cancelled.", embed=None, view=None)


class ChannelPicker(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Choose a text channel…", channel_types=[discord.ChannelType.text, discord.ChannelType.news])

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        guild = interaction.guild
        me = guild.me if guild else None
        perms = channel.permissions_for(me) if me else None
        if not perms or not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                "I need **View Channel**, **Send Messages**, and **Embed Links** in that channel.", ephemeral=True
            )
        view: ChannelPickView = self.view
        view.state.destination_type = "channel"
        view.state.guild_id = guild.id
        view.state.channel_id = channel.id
        view.state.channel_mention = channel.mention
        await show_location_step(interaction, view.cog, view.state)


class ChannelPickView(OwnedView):
    def __init__(self, cog, state: SubscriptionDraft):
        super().__init__(state.user_id)
        self.cog, self.state = cog, state
        self.add_item(ChannelPicker())

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=wizard_embed("Where should this report go?", "Choose a personal DM or a server channel.", self.state),
            view=DestinationView(self.cog, self.state),
        )


class LocationSelect(discord.ui.Select):
    def __init__(self, locations: List[Dict[str, Any]]):
        options = []
        for loc in locations[:24]:
            options.append(discord.SelectOption(
                label=(loc.get("name") or "Saved location")[:100],
                description=(loc.get("display_name") or "")[:100],
                value=str(loc["id"]),
                emoji="⭐" if loc.get("is_default") else "📍",
            ))
        super().__init__(placeholder="Choose a saved location…", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: LocationView = self.view
        loc = view.cog.store.get_location(int(self.values[0]))
        if not loc:
            return await interaction.response.send_message("That saved location no longer exists.", ephemeral=True)
        view.state.location = loc
        embed = wizard_embed("How often?", "Choose whether this report should run daily or weekly.", view.state)
        await interaction.response.edit_message(embed=embed, view=CadenceView(view.cog, view.state))


class NewLocationModal(discord.ui.Modal, title="Use a new location"):
    location_query = discord.ui.TextInput(label="City, place, or postal code", placeholder="Auckland, New Zealand", max_length=150)

    def __init__(self, cog, state: SubscriptionDraft):
        super().__init__()
        self.cog, self.state = cog, state

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            loc = await self.cog.resolve_location_query(self.location_query.value)
            self.state.location = loc
            embed = wizard_embed("How often?", "Choose whether this report should run daily or weekly.", self.state)
            await interaction.edit_original_response(embed=embed, view=CadenceView(self.cog, self.state))
        except Exception as exc:
            await interaction.followup.send(f"Could not find that location: {exc}", ephemeral=True)


class LocationView(OwnedView):
    def __init__(self, cog, state: SubscriptionDraft, locations: List[Dict[str, Any]]):
        super().__init__(state.user_id)
        self.cog, self.state = cog, state
        if locations:
            self.add_item(LocationSelect(locations))

    @discord.ui.button(label="Search New Location", emoji="🔎", style=discord.ButtonStyle.primary, row=1)
    async def search(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(NewLocationModal(self.cog, self.state))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=wizard_embed("Where should this report go?", "Choose a personal DM or a server channel.", self.state),
            view=DestinationView(self.cog, self.state),
        )


async def show_location_step(interaction: discord.Interaction, cog, state: SubscriptionDraft):
    locations = cog.store.list_locations(state.user_id)
    text = "Choose one of your saved locations or search for a new place."
    if not locations:
        text = "You do not have a saved location yet. Search for a city, place, or postal code."
    await interaction.response.edit_message(embed=wizard_embed("Which location?", text, state), view=LocationView(cog, state, locations))


class CadenceView(OwnedView):
    def __init__(self, cog, state: SubscriptionDraft):
        super().__init__(state.user_id)
        self.cog, self.state = cog, state

    async def choose(self, interaction: discord.Interaction, cadence: str):
        self.state.cadence = cadence
        await interaction.response.send_modal(ScheduleModal(self.cog, self.state))

    @discord.ui.button(label="Daily", emoji="☀️", style=discord.ButtonStyle.primary)
    async def daily(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.choose(interaction, "daily")

    @discord.ui.button(label="Weekly", emoji="📅", style=discord.ButtonStyle.secondary)
    async def weekly(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.choose(interaction, "weekly")


class ScheduleModal(discord.ui.Modal, title="Choose delivery time"):
    time_value = discord.ui.TextInput(label="Time", placeholder="7:00 AM or 19:00", max_length=20)
    forecast_days = discord.ui.TextInput(
        label="Weekly forecast days (weekly only)", placeholder="7", default="7", required=False, max_length=2
    )

    def __init__(self, cog, state: SubscriptionDraft):
        super().__init__()
        self.cog, self.state = cog, state

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hh, mi = self.cog.parse_subscription_time(self.time_value.value)
            weekly_days = int((self.forecast_days.value or "7").strip() or "7")
            if not 3 <= weekly_days <= 10:
                raise ValueError("Weekly forecast days must be between 3 and 10.")
            self.state.hh, self.state.mi, self.state.weekly_days = hh, mi, weekly_days
            embed = wizard_embed("When should it send?", "Choose whether the report always sends or only sends when a threshold is met.", self.state)
            await interaction.response.edit_message(embed=embed, view=ConditionView(self.cog, self.state))
        except Exception as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


class ConditionView(OwnedView):
    def __init__(self, cog, state: SubscriptionDraft):
        super().__init__(state.user_id)
        self.cog, self.state = cog, state

    @discord.ui.button(label="Always Send", emoji="✅", style=discord.ButtonStyle.success)
    async def always(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.state.condition_metric = self.state.condition_operator = None
        self.state.condition_value = None
        await show_confirmation(interaction, self.cog, self.state)

    @discord.ui.button(label="Add Threshold", emoji="🎯", style=discord.ButtonStyle.primary)
    async def threshold(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(ConditionModal(self.cog, self.state))


class ConditionModal(discord.ui.Modal, title="Add a threshold"):
    metric = discord.ui.TextInput(
        label="Metric", placeholder="max wind, max temp, min temp, rain chance, precipitation, or UV", max_length=40
    )
    operator = discord.ui.TextInput(label="Comparison", placeholder=">, >=, <, or <=", max_length=2)
    value = discord.ui.TextInput(label="Threshold value", placeholder="20", max_length=20)

    def __init__(self, cog, state: SubscriptionDraft):
        super().__init__()
        self.cog, self.state = cog, state

    async def on_submit(self, interaction: discord.Interaction):
        try:
            metric = self.cog.normalize_condition_metric(self.metric.value)
            operator = self.operator.value.strip()
            if operator not in {">", ">=", "<", "<="}:
                raise ValueError("Comparison must be >, >=, <, or <=.")
            value = float(self.value.value.strip())
            self.state.condition_metric, self.state.condition_operator, self.state.condition_value = metric, operator, value
            await show_confirmation(interaction, self.cog, self.state)
        except Exception as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)


async def show_confirmation(interaction: discord.Interaction, cog, state: SubscriptionDraft):
    embed = wizard_embed("Review", "Review the subscription below, then create it or go back.", state)
    await interaction.response.edit_message(embed=embed, view=ConfirmView(cog, state))


class ConfirmView(OwnedView):
    def __init__(self, cog, state: SubscriptionDraft):
        super().__init__(state.user_id)
        self.cog, self.state = cog, state

    @discord.ui.button(label="Create Subscription", emoji="✅", style=discord.ButtonStyle.success)
    async def create(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            sub_id, first = self.cog.create_subscription_from_draft(self.state)
            embed = discord.Embed(
                title="✅ Subscription Created",
                description=f"Subscription **#{sub_id}** is active.\nNext evaluation: **{first.strftime('%Y-%m-%d %I:%M %p %Z')}**",
                colour=discord.Colour.green(),
            )
            await interaction.edit_original_response(embed=embed, view=CreatedSubscriptionView(self.cog, self.state.user_id, sub_id))
        except Exception as exc:
            await interaction.followup.send(f"Could not create the subscription: {exc}", ephemeral=True)

    @discord.ui.button(label="Back to Condition", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=wizard_embed("When should it send?", "Choose whether the report always sends or only sends when a threshold is met.", self.state),
            view=ConditionView(self.cog, self.state),
        )


class CreatedSubscriptionView(OwnedView):
    def __init__(self, cog, owner_id: int, sub_id: int):
        super().__init__(owner_id)
        self.cog, self.sub_id = cog, sub_id

    @discord.ui.button(label="Test Now", emoji="🧪", style=discord.ButtonStyle.primary)
    async def test(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            destination = await self.cog.test_subscription(self.sub_id, interaction.user, interaction.guild)
            await interaction.followup.send(f"✅ Test forecast sent to {destination}.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ Test failed: {exc}", ephemeral=True)


class SubscriptionSelect(discord.ui.Select):
    def __init__(self, rows: List[Dict[str, Any]]):
        options = []
        for row in rows[:25]:
            dest = "channel" if row.get("destination_type") == "channel" else "DM"
            options.append(discord.SelectOption(
                label=f"#{row['id']} · {(row.get('location_name') or row.get('zip') or 'Location')[:75]}",
                description=f"{row['cadence']} {row['hh']:02d}:{row['mi']:02d} · {dest} · {'active' if row.get('enabled', 1) else 'paused'}"[:100],
                value=str(row["id"]),
            ))
        super().__init__(placeholder="Choose a subscription to manage…", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: SubscriptionManageView = self.view
        view.selected_id = int(self.values[0])
        await interaction.response.send_message(f"Selected subscription **#{view.selected_id}**. Use the buttons below.", ephemeral=True)


class SubscriptionManageView(OwnedView):
    def __init__(self, cog, owner_id: int, rows: List[Dict[str, Any]], guild_id: Optional[int]):
        super().__init__(owner_id)
        self.cog, self.rows, self.guild_id = cog, rows, guild_id
        self.selected_id: Optional[int] = rows[0]["id"] if len(rows) == 1 else None
        self.add_item(SubscriptionSelect(rows))

    def selected(self) -> int:
        if not self.selected_id:
            raise ValueError("Choose a subscription from the menu first.")
        return self.selected_id

    @discord.ui.button(label="Test", emoji="🧪", style=discord.ButtonStyle.primary, row=1)
    async def test(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            destination = await self.cog.test_subscription(self.selected(), interaction.user, interaction.guild)
            await interaction.followup.send(f"✅ Test forecast sent to {destination}.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)

    @discord.ui.button(label="Pause / Resume", emoji="⏯️", style=discord.ButtonStyle.secondary, row=1)
    async def toggle(self, interaction: discord.Interaction, _button: discord.ui.Button):
        try:
            row = self.cog.get_manageable_subscription(self.selected(), interaction.user, interaction.guild)
            enabled = 0 if row.get("enabled", 1) else 1
            self.cog.store.update_weather_sub(row["id"], enabled=enabled, failure_count=0, last_error=None)
            await interaction.response.send_message(f"Subscription **#{row['id']}** is now **{'active' if enabled else 'paused'}**.", ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def delete(self, interaction: discord.Interaction, _button: discord.ui.Button):
        try:
            row = self.cog.get_manageable_subscription(self.selected(), interaction.user, interaction.guild)
            admin = bool(interaction.guild and interaction.user.guild_permissions.manage_guild)
            ok = self.cog.store.remove_weather_sub(row["id"], interaction.user.id, interaction.guild.id if interaction.guild else None, admin)
            await interaction.response.send_message("✅ Subscription deleted." if ok else "Could not delete that subscription.", ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
