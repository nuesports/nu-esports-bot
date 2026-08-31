import random
from collections.abc import Awaitable, Callable

import discord
from discord.ext import commands, tasks

from utils import config, db, wallet

GUILD_ID = config.secrets["discord"]["guild_id"]


class Points(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot: discord.Bot = bot
        self.points_buffer: dict[int, int] = {}
        self.predictions: dict[int, Prediction] = {}
        self.update_points.start()

    points = discord.SlashCommandGroup("points", "points :)")
    points_prediction = points.create_subgroup("prediction", "Predictions with points")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        user = message.author
        if user == self.bot.user or user.bot:
            return

        self.points_buffer[user.id] = random.randint(7, 25)

    @points.command(
        name="balance",
        description="Get your points balance or another user's point balance",
        guild_ids=[GUILD_ID],
    )
    async def balance(
        self,
        ctx: discord.ApplicationContext,
        user: discord.User = discord.Option(default=None),
    ) -> None:
        await ctx.defer()

        target_user = user if user else ctx.user

        # COALESCE because users.points is nullable: a NULL would reach the :, format spec
        # below as None and raise TypeError, where the unformatted version just printed it.
        sql = "SELECT COALESCE(points, 0) FROM users WHERE discordid = %s;"
        data = [target_user.id]
        result = await db.fetch_one(sql, data)

        points = result[0] if result else 0
        embed = discord.Embed(
            title=f"{target_user.display_name}'s points",
            description=f"{points:,} points",
            color=discord.Color.from_rgb(78, 42, 132),
        )
        await ctx.followup.send(embed=embed)

    @points_prediction.command(
        name="start", description="Start a prediction", guild_ids=[GUILD_ID]
    )
    async def start_prediction(
        self,
        ctx: discord.ApplicationContext,
        title: str,
        option_a: str,
        option_b: str,
    ) -> None:
        if ctx.user.id in self.predictions:
            await ctx.respond("You already have a prediction open.", ephemeral=True)
            return
        if option_a == option_b:
            await ctx.respond("Options must be different.", ephemeral=True)
            return

        message = await ctx.send(f"PREDICTION: **{title}**")
        thread = await message.create_thread(name=f"PREDICTION: {title}")

        prediction = Prediction(title, option_a, option_b, thread)
        await prediction.create_prediction()

        self.predictions[ctx.user.id] = prediction
        await ctx.respond(f"Prediction started: {thread.mention}", ephemeral=True)

    @points_prediction.command(
        name="lock",
        description="Lock prediction and stop further users from joining",
        guild_ids=[GUILD_ID],
    )
    async def lock_prediction(self, ctx: discord.ApplicationContext) -> None:
        prediction = self.predictions.get(ctx.user.id, None)
        if not prediction:
            await ctx.respond("You don't have a prediction open.", ephemeral=True)
            return

        await prediction.lock_prediction()
        await ctx.respond("Prediction locked.", ephemeral=True)

    @points_prediction.command(
        name="complete",
        description="Complete prediction and reward users",
        guild_ids=[GUILD_ID],
    )
    async def complete_prediction(
        self, ctx: discord.ApplicationContext, winner: str
    ) -> None:
        prediction = self.predictions.get(ctx.user.id, None)
        if not prediction:
            await ctx.respond("You don't have a prediction open.", ephemeral=True)
            return
        if winner not in [prediction.option_a, prediction.option_b]:
            await ctx.respond(
                f"Winner must be one of the options: `{prediction.option_a}` or `{prediction.option_b}`",
                ephemeral=True,
            )
            return

        await prediction.complete_prediction(winner)
        del self.predictions[ctx.user.id]

        await ctx.respond(f"Prediction completed for {winner}.", ephemeral=True)

    @points_prediction.command(
        name="refund",
        description="Cancel prediction and refund users",
        guild_ids=[GUILD_ID],
    )
    async def cancel_prediction(self, ctx: discord.ApplicationContext) -> None:
        prediction = self.predictions.get(ctx.user.id, None)
        if not prediction:
            await ctx.respond("You don't have a prediction open.", ephemeral=True)
            return

        await prediction.refund_prediction()
        del self.predictions[ctx.user.id]

        await ctx.respond("Prediction refunded.", ephemeral=True)

    @tasks.loop(seconds=60)
    async def update_points(self) -> None:
        if not self.points_buffer:
            return

        sql = """INSERT INTO users (discordid, points)
            VALUES (%s, %s)
            ON CONFLICT (discordid)
            DO UPDATE SET points = users.points + EXCLUDED.points;
        """
        data = [(user_id, points) for user_id, points in self.points_buffer.items()]
        await db.perform_many(sql, data)

        self.points_buffer.clear()


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Points(bot))


class Prediction:
    def __init__(
        self, title: str, option_a: str, option_b: str, thread: discord.Thread
    ) -> None:
        self.title: str = title
        self.option_a: str = option_a
        self.option_b: str = option_b
        self.thread: discord.Thread = thread

    async def create_prediction(self) -> None:
        embed = discord.Embed(
            title=self.title,
            color=discord.Color.from_rgb(78, 42, 132),
        )
        self.view: PredictionView = PredictionView(self.option_a, self.option_b, embed)
        self.message: discord.Message = await self.thread.send(
            "", embed=self.view.update_embed(), view=self.view
        )
        self.view.message = self.message

    async def lock_prediction(self) -> None:
        if self.view.locked:
            return
        await self.view.lock_view()
        await self.message.reply("Prediction locked.")

    async def complete_prediction(self, winner: str) -> None:
        if not self.view.option_a_points or not self.view.option_b_points:
            await wallet.credit_many(self.view.every_stake())
            await self.view.lock_view()
            await self.message.reply("Everyone voted the same way! Points refunded.")
            return

        if winner == self.option_a:
            payout = self.view.odds_a
            winning_stakes = self.view.option_a_points
        else:
            payout = self.view.odds_b
            winning_stakes = self.view.option_b_points
        await wallet.credit_many(
            [
                (round(stake * payout), user_id)
                for user_id, stake in winning_stakes.items()
            ]
        )
        format = "Prediction completed -- {} points distributed to {} ({}x payout)."
        if winner == self.option_a:
            message = format.format(
                sum(self.view.option_b_points.values()),
                self.option_a,
                round(payout, 2),
            )
        else:
            message = format.format(
                sum(self.view.option_a_points.values()),
                self.option_b,
                round(payout, 2),
            )
        await self.view.lock_view()
        await self.message.reply(message)

    async def refund_prediction(self) -> None:
        await wallet.credit_many(self.view.every_stake())
        await self.view.lock_view()
        await self.message.reply("Prediction cancelled. Points refunded.")


class PredictionView(discord.ui.View):
    def __init__(
        self, option_a: str, option_b: str, embed: discord.Embed
    ) -> None:
        super().__init__(timeout=1200)

        self.option_a: str = option_a
        self.option_a_points: dict[int, int] = {}
        self.option_b: str = option_b
        self.option_b_points: dict[int, int] = {}

        self.message: discord.Message | None = None
        self.embed: discord.Embed = embed
        self.locked: bool = False

        def create_button(label: str) -> discord.ui.Button:
            async def button_callback(interaction: discord.Interaction) -> None:
                if any(
                    [
                        label == self.option_a
                        and interaction.user.id in self.option_b_points,
                        label == self.option_b
                        and interaction.user.id in self.option_a_points,
                    ]
                ):
                    await interaction.response.send_message(
                        f"{interaction.user.mention} tried to change sides..."
                    )
                    return
                # COALESCE because points is nullable, and `result[0] if result else 0`
                # only guards a missing row -- a NULL column reaches the modal as None
                # and raises on the `user_points < points` comparison.
                sql = "SELECT COALESCE(points, 0) FROM users WHERE discordid = %s;"
                data = [interaction.user.id]
                result = await db.fetch_one(sql, data)

                await interaction.response.send_modal(
                    PredictionModal(
                        self.modal_callback, label, result[0] if result else 0
                    )
                )

            button = discord.ui.Button(label=label)
            button.callback = button_callback
            return button

        self.add_item(create_button(self.option_a))
        self.add_item(create_button(self.option_b))

    def every_stake(self) -> list[tuple[int, int]]:
        """Both sides' stakes as (amount, discordid) rows, for refunding the whole board."""
        return [
            (stake, user_id)
            for stakes in (self.option_a_points, self.option_b_points)
            for user_id, stake in stakes.items()
        ]

    def update_embed(self) -> discord.Embed:
        self.embed.clear_fields()
        format = "{} points\n{} users\n{}x payout"
        self.odds_a: float = wallet.payout_multiplier(
            sum(self.option_a_points.values()), sum(self.option_b_points.values())
        )
        self.odds_b: float = wallet.payout_multiplier(
            sum(self.option_b_points.values()), sum(self.option_a_points.values())
        )
        self.embed.add_field(
            name=self.option_a,
            value=format.format(
                sum(self.option_a_points.values()),
                len(self.option_a_points),
                round(self.odds_a, 2),
            ),
        )
        self.embed.add_field(
            name=self.option_b,
            value=format.format(
                sum(self.option_b_points.values()),
                len(self.option_b_points),
                round(self.odds_b, 2),
            ),
        )
        return self.embed

    async def reply(self, content: str) -> None:
        """Reply to the live prediction message, if there still is one.

        message is None until the prediction is posted, and stays None if the
        post failed -- an unguarded .reply() here crashed on quiet expiry.
        """
        if self.message is not None:
            await self.message.reply(content)

    async def on_timeout(self) -> None:
        if self.locked:
            return
        await self.reply("Prediction locked.")
        await self.lock_view()

    async def lock_view(self) -> None:
        self.locked = True
        self.disable_all_items()
        if self.message is not None:
            await self.message.edit(view=self)

    async def modal_callback(
        self, user: discord.User | discord.Member, points: int, option: str
    ) -> None:
        # A modal opened before the lock can still be submitted after it, and a late
        # stake would recompute the odds the payout was already announced with.
        if self.locked:
            await self.reply(f"{user.mention} tried to bet on a locked prediction!")
            return

        # Atomic conditional UPDATE. PredictionModal's earlier balance check is a stale
        # snapshot, so this guard is what actually prevents overdrafting.
        deducted = await wallet.try_deduct(user.id, points)
        if not deducted:
            await self.reply(f"{user.mention} tried to bet more points than they have!")
            return

        # Re-check after the await, the way matchmaking's BetModal re-checks its epoch:
        # the lock can land mid-deduction, and complete_prediction pays out off these
        # dicts, so a stake booked afterwards is deducted and never seen again.
        if self.locked:
            await wallet.credit(user.id, points)
            await self.reply(f"{user.mention} tried to bet on a locked prediction!")
            return

        if option == self.option_a:
            prev = self.option_a_points.pop(user.id, 0)
            self.option_a_points[user.id] = prev + points
        else:
            prev = self.option_b_points.pop(user.id, 0)
            self.option_b_points[user.id] = prev + points

        if self.message is not None:
            await self.message.edit(embed=self.update_embed())

        format = "{} bet {} points on **{}**"
        format_prev = "\n(up from {})"
        message = format.format(user.mention, prev + points, option)
        if prev > 0:
            message += format_prev.format(prev)

        await self.reply(message)


class PredictionModal(discord.ui.Modal):
    def __init__(
        self,
        callback: Callable[
            [discord.User | discord.Member, int, str], Awaitable[None]
        ],
        option: str,
        user_points: int,
    ) -> None:
        super().__init__(title="Prediction")
        self.view_callback: Callable[
            [discord.User | discord.Member, int, str], Awaitable[None]
        ] = callback
        self.option: str = option
        self.user_points: int = user_points

        self.add_item(
            discord.ui.InputText(
                label=f"How many points? ({self.user_points} available)",
                required=True,
                min_length=1,
                placeholder="Enter a number greater than 0",
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.children[0].value
        if not value.isdigit():
            await interaction.response.send_message(
                "You must wager a numeric amount!", ephemeral=True
            )
            return

        points = int(value)
        if points <= 0:
            await interaction.response.send_message(
                "You must wager more than 0 points!", ephemeral=True
            )
            return

        if self.user_points < points:
            await interaction.response.send_message(
                "You don't have enough points!", ephemeral=True
            )
            return

        await interaction.response.defer()
        await self.view_callback(interaction.user, points, self.option)
