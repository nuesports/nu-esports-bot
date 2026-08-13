import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from utils import config, db

GUILD_ID = config.secrets["discord"]["guild_id"]
CENTRAL_TZ = ZoneInfo("America/Chicago")


class Gameroom(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def _get_hours_for_day(adjusted_hours: dict, day: datetime.date, default: str):
        value = adjusted_hours.get(day)
        if value is None:
            value = adjusted_hours.get(day.strftime("%Y-%m-%d"))
        return default if value is None else value

    gameroom = discord.SlashCommandGroup(
        "gameroom", "Game Room and Nexus Gaming Lounge commands"
    )

    @gameroom.command(
            name="sethours", description="Set or clear an hours override for a date", guild_ids=[GUILD_ID]
    )
    async def sethours(self, 
                       ctx: discord.ApplicationContext,
                       start_date: discord.Option(
                            str,
                            description="Start date in YYYY-MM-DD format"
                        ),
                       end_date: discord.Option(
                           str,
                           description="(optional) End date in YYYY-MM-DD format",
                           required=False
                       ),
                       regular_text: discord.Option(
                           str,
                           description="Text to display, leave blank to clear override(s)",
                           required=False
                       ),
                       weekend_text: discord.Option(
                           str,
                           description="Text to display, on Fri/Sat/Sun",
                           required=False
                       )
                    ):
        if not config.is_gameroom_staff(ctx.author):
            await ctx.respond("You do not have permission to use this command.", ephemeral=True)
            return

        try:
            start = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            await ctx.respond("Invalid start date. Use YYYY-MM-DD.", ephemeral=True)
            return

        if end_date:
            try:
                end = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError:
                await ctx.respond("Invalid end date. Use YYYY-MM-DD.", ephemeral=True)
                return
        else:
            end = start

        if end < start:
            await ctx.respond("End date can't be before start date.", ephemeral=True)
            return

        span_days = (end - start).days + 1
        if span_days > 90:
            await ctx.respond("Range too large (max 90 days) double check your range", ephemeral=True)
            return

        await ctx.defer()

        #wipe past-due rows table-wide (not just this range) -- piggybacks on every sethours call
        #instead of a separate cleanup job, using Central time's "today" rather than the DB server's own timezone
        today_central = datetime.datetime.now(CENTRAL_TZ).date()
        await db.perform_one(
            "DELETE FROM gameroom_hours_overrides WHERE date < %s", (today_central,)
        )

        dates = [start + datetime.timedelta(days=i) for i in range(span_days)]
        weekday_dates = [d for d in dates if d.weekday() not in (4, 5, 6)]  # Fri, Sat, Sun
        weekend_dates = [d for d in dates if d.weekday() in (4, 5, 6)]

        to_set = []
        to_clear = []

        if regular_text:
            to_set += [(d, regular_text) for d in weekday_dates]
        elif not weekend_text:
            to_clear += weekday_dates
        # else: regular_text blank, weekend_text set -- leave weekdays untouched

        if weekend_text:
            to_set += [(d, weekend_text) for d in weekend_dates]
        elif regular_text:
            to_set += [(d, regular_text) for d in weekend_dates]
        else:
            to_clear += weekend_dates

        if to_set:
            await db.perform_many(
                """
                INSERT INTO gameroom_hours_overrides (date, hours)
                VALUES (%s, %s)
                ON CONFLICT (date) DO UPDATE SET hours = EXCLUDED.hours
                """,
                to_set,
            )
        if to_clear:
            await db.perform_one(
                "DELETE FROM gameroom_hours_overrides WHERE date = any(%s)",
                (to_clear,)
            )


        date_range = (
            start.strftime("%-m/%-d/%Y")
            if span_days == 1
            else f"{start.strftime('%-m/%-d/%Y')} - {end.strftime('%-m/%-d/%Y')}"
        )
        day_word = "day" if span_days == 1 else "days"
        if not regular_text and not weekend_text:
            await ctx.respond(f"Cleared overrides for {date_range} ({span_days} {day_word})")
        elif weekend_text and not regular_text:
            await ctx.respond(f"Set Fri-Sun hours for {date_range} ({span_days} {day_word}) to: {weekend_text} (weekdays left unchanged)")
        elif regular_text and not weekend_text:
            await ctx.respond(f"Set hours for {date_range} ({span_days} {day_word}) to: {regular_text}")
        else:
            await ctx.respond(f"Updated hours for {date_range} ({span_days} {day_word}) -- weekdays: {regular_text}, Fri-Sun: {weekend_text}")
        


    @gameroom.command(
        name="hours", description="Lists current game room hours", guild_ids=[GUILD_ID]
    )
    async def hours(self, ctx):
        default_hours = config.gameroom_data["default_hours"]

        today = datetime.datetime.now(tz=CENTRAL_TZ).date()
        start = today - datetime.timedelta(days=today.weekday())
        end = start + datetime.timedelta(days=6)
        week = [start + datetime.timedelta(days=i) for i in range(7)]

        
        rows = await db.fetch_all(
            "SELECT date, hours FROM gameroom_hours_overrides WHERE date BETWEEN %s AND %s",
            (start, end),
        )
        adjusted_hours = {row[0]: row[1] for row in rows}

        embed = discord.Embed(
            title="Game Room Hours",
            color=discord.Color.from_rgb(78, 42, 132),
        )

        embed.add_field(
            name=f"Week of {start.strftime('%-m/%-d')} - {end.strftime('%-m/%-d')}",
            value="",
        )

        for i, day in enumerate(week):
            value = self._get_hours_for_day(adjusted_hours, day, default_hours[i])
            embed.add_field(name=day.strftime("%A"), value=value, inline=False)

        embed.set_image(
            url="https://www.northwestern.edu/norris/arts-recreation/game-room/nexus_general_awareness-01.png"
        )

        await ctx.respond("", embed=embed)

    @gameroom.command(
        name="games",
        description="Lists games available on game room consoles",
        guild_ids=[GUILD_ID],
    )
    async def games(self, ctx):
        games = config.gameroom_data["games"]

        embed = discord.Embed(
            title="Game Room Games",
            color=discord.Color.from_rgb(78, 42, 132),
        )

        embed.add_field(name="PS4", value="\n".join(games["ps4"]), inline=True)
        embed.add_field(name="PS5", value="\n".join(games["ps5"]), inline=True)
        embed.add_field(name="Nintendo 64", value="\n".join(games["n64"]), inline=True)
        embed.add_field(
            name="Nintendo Switch", value="\n".join(games["switch"]), inline=True
        )
        embed.add_field(name="Wii U", value="\n".join(games["wii_u"]), inline=True)
        embed.add_field(name="Xbox One", value="\n".join(games["xbox"]), inline=True)

        await ctx.respond("", embed=embed)


def setup(bot):
    bot.add_cog(Gameroom(bot))
