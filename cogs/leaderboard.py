"""
In-house Leaderboard;

Provides a leaderboard for users to see how they stack up against others
"""

import discord
from discord.ext import commands

from utils import config, db

GUILD_ID = config.secrets["discord"]["guild_id"]
GAME_CHOICES = list(config.game_data.keys())
POINTS_BOARD = "points"
BOARD_CHOICES = [*GAME_CHOICES, POINTS_BOARD]
PAGE_SIZE = 10
PRIOR_GAMES = 10  # phantom average-record games blended into win rate so a few games can't swing rank

def is_game_board(board: str) -> bool:
    """Whether a /leaderboard choice is a configured game, as opposed to a synthetic board
    like Points that has no game_data entry, and so no roles, no elo, and no win/loss.

    Every config lookup on the chosen value has to go through this first. is_per_role_ranks
    and rankable_roles subscript game_data directly, so they raise KeyError rather than
    returning False for anything that isn't a game."""
    return board in config.game_data

def leaderboard_label(game: str, role: str | None) -> str:
    """Display label for a leaderboard: just the game, or "game role" for a per-role one."""
    if not is_game_board(game):
        return game.title()  # synthetic boards have no role dimension to name
    return f"{game.title()} {role}" if role else game.title()

async def fetch_leaderboard_rows(game: str, role: str | None = None) -> list[tuple]:
    """Fetch every player's win/loss + tag for a game, ranked by win rate (elo only breaks ties).

    Win rate is Bayesian-smoothed with PRIOR_GAMES phantom average games, so a few games can't
    swing rank the way a raw win% would -- see compute_win_rate.

    - `role` given: that role's players (profile_role_elo).
    - `role` omitted on a per-role-ranks game: a "mixed" leaderboard -- each player's
      single best role by elo, ranked by win rate across all roles.
    - `role` omitted otherwise: the one game-wide pool (profile_elo).

    Only players with at least one recorded match (a profile_stats row) show up -- an
    elo-seeded player who's never actually played doesn't belong on a results scoreboard.

    Every row is (discordid, wins, losses, tag, role_or_none) -- the role slot is only
    ever populated for the mixed leaderboard, to show which role each entry is from.
    """
    if role is not None:
        rows = await db.fetch_all(
            """
            SELECT pre.discordid, ps.wins, ps.losses, p.tag
            FROM profile_role_elo pre
            JOIN profile_stats ps ON ps.discordid = pre.discordid AND ps.game = pre.game
            LEFT JOIN profiles p ON p.discordid = pre.discordid
            WHERE pre.game = %s AND pre.role = %s
            ORDER BY (ps.wins + %s * 0.5) / (ps.wins + ps.losses + %s) DESC, pre.elo DESC;
            """,
            (game, role, PRIOR_GAMES, PRIOR_GAMES),
        )
        return [(discordid, wins, losses, tag, None) for discordid, wins, losses, tag in rows]

    if config.is_per_role_ranks(game):
        rows = await db.fetch_all(
            """
            SELECT discordid, role, wins, losses, tag FROM (
                SELECT DISTINCT ON (pre.discordid)
                    pre.discordid, pre.role, pre.elo,
                    ps.wins, ps.losses, p.tag
                FROM profile_role_elo pre
                JOIN profile_stats ps ON ps.discordid = pre.discordid AND ps.game = pre.game
                LEFT JOIN profiles p ON p.discordid = pre.discordid
                WHERE pre.game = %s
                ORDER BY pre.discordid, pre.elo DESC
            ) best_role
            ORDER BY (wins + %s * 0.5) / (wins + losses + %s) DESC, elo DESC;
            """,
            (game, PRIOR_GAMES, PRIOR_GAMES),
        )
        return [(discordid, wins, losses, tag, entry_role) for discordid, entry_role, wins, losses, tag in rows]

    rows = await db.fetch_all(
        """
        SELECT pe.discordid, ps.wins, ps.losses, p.tag
        FROM profile_elo pe
        JOIN profile_stats ps ON ps.discordid = pe.discordid AND ps.game = pe.game
        LEFT JOIN profiles p ON p.discordid = pe.discordid
        WHERE pe.game = %s
        ORDER BY (ps.wins + %s * 0.5) / (ps.wins + ps.losses + %s) DESC, pe.elo DESC;
        """,
        (game, PRIOR_GAMES, PRIOR_GAMES),
    )
    return [(discordid, wins, losses, tag, None) for discordid, wins, losses, tag in rows]

async def fetch_points_rows(caller_id: int) -> list[tuple]:
    """Fetch every user's points balance and profile tag, richest first.

    COALESCE rather than a bare ORDER BY points DESC because users.points is nullable:
    Postgres sorts NULLs first under DESC, so one NULL row would crown itself #1.

    Every row is (discordid, points, tag), users are defaulted to 0 if they have no row.
    """
    rows = await db.fetch_all(
        """
        SELECT u.discordid, COALESCE(u.points, 0), p.tag
        FROM users u
        LEFT JOIN profiles p ON p.discordid = u.discordid
        ORDER BY COALESCE(u.points, 0) DESC, u.discordid;
        """
    )
    rows = [(discordid, points, tag) for discordid, points, tag in rows]
    if not any(row[0] == caller_id for row in rows):
        rows.append((caller_id, 0, None))
    return rows

async def role_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """Suggest rankable roles once a per-role-ranks game has been picked."""
    game = ctx.options.get("game")
    if not game or not is_game_board(game) or not config.is_per_role_ranks(game):
        return []
    return [discord.OptionChoice(r) for r in config.rankable_roles(game)]

def format_entry(guild: discord.Guild, rank: int, discordid: int, wins: int, losses: int, tag: str | None, game: str, entry_role: str | None) -> str:
    """Format one leaderboard row: rank, tag (defaulting to a star), display name, and W/L record.

    `entry_role` is only set on a mixed (no-role) per-role leaderboard, and adds an
    icon marking which role that player's shown elo was their best in."""
    member = guild.get_member(discordid)
    name = member.display_name if member else f"<@{discordid}>"
    tag = tag or "⭐"
    icon = f" {config.role_icon(game, entry_role)}" if entry_role else ""
    return f"{rank}. {tag} *{name}* — **{wins}W** / **{losses}L**{icon}"

def format_points_entry(guild: discord.Guild, rank: int, discordid: int, points: int, tag: str | None) -> str:
    """Format one Points row: rank, tag (defaulting to a star), display name, and balance.

    Same shape as format_entry, with a balance where the win/loss record goes."""
    member = guild.get_member(discordid)
    name = member.display_name if member else f"<@{discordid}>"
    tag = tag or "⭐"
    return f"{rank}. {tag} *{name}* — **{points:,} points**"

def build_leaderboard_pages(guild: discord.Guild, game: str, rows: list[tuple], caller_id: int, role: str | None = None, format_row=None, unranked_note: str | None = None) -> list[discord.Embed] | None:
    """Build one embed per page of 10 leaderboard entries, ordered by win rate (see fetch_leaderboard_rows).

    Pass `role` for a per-role-ranks game's leaderboard, just to title the embed correctly.
    """
    if format_row is None:
        def format_row(rank, row):
            discordid, wins, losses, tag, entry_role = row
            return format_entry(guild, rank, discordid, wins, losses, tag, game, entry_role)

    present_rows = [row for row in rows if guild.get_member(row[0]) is not None]
    if not present_rows:
        return None

    total_pages = max(1, -(-len(present_rows) // PAGE_SIZE)) #ceiling divison

    caller_rank = None
    caller_line = None
    for rank, row in enumerate(present_rows, start=1):
        if row[0] == caller_id:
            caller_rank = rank
            caller_line = format_row(rank, row)
            break

    pages = []
    for page in range(total_pages):
        start = page * PAGE_SIZE
        chunk = present_rows[start:start+ PAGE_SIZE]
        lines = [
            format_row(start + i + 1, row)
            for i, row in enumerate(chunk)
        ]

        page_start_rank = start + 1
        page_end_rank = start + len(chunk)

        label = leaderboard_label(game, role)

        if caller_rank is None:
            note = unranked_note or f"You haven't played {label} yet!"
            lines.append(f"...\n{note}")
        elif caller_rank < page_start_rank:
            lines.insert(0, f"{caller_line}\n...")
        elif caller_rank > page_end_rank:
            lines.append(f"...\n{caller_line}")
        # else: caller's own rank falls on this page already

        embed = discord.Embed(
            title=f"{label} Leaderboard",
            description="\n".join(lines),
            color=discord.Color.from_rgb(78, 42, 132),
        )
        embed.set_footer(text=f"Page {page+1}/{total_pages}")
        pages.append(embed)

    return pages

async def build_points_pages(guild: discord.Guild, caller_id: int) -> list[discord.Embed] | None:
    """Fetch and page the Points board, shared by /leaderboard and the Change Game select.."""
    rows = await fetch_points_rows(caller_id)
    return build_leaderboard_pages(
        guild, POINTS_BOARD, rows, caller_id,
        format_row=lambda rank, row: format_points_entry(guild, rank, *row),
        unranked_note="You have no points yet!",
    )

class GameSelectView(discord.ui.View):
    """Dropdown for switching the leaderboard to a different game, restricted to whoever ran /leaderboard."""

    def __init__(self, requester_id: int, guild: discord.Guild):
        super().__init__(timeout=120, disable_on_timeout=True)
        self.requester_id = requester_id
        self.guild = guild

        options = [discord.SelectOption(label=b.title(), value=b) for b in BOARD_CHOICES]
        self.select = discord.ui.Select(placeholder="Pick a leaderboard", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Block anyone but whoever ran /leaderboard from switching games."""
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "This isn't your leaderboard call to flip through!", ephemeral=True
            )
            return False
        return True
    
    async def on_select(self, interaction: discord.Interaction) -> None:
        """Rebuild the leaderboard for the newly chosen game. Per-role-ranks games
        default to the mixed (best-role) leaderboard; a "Change Role" button lets
        the player narrow to one role from there."""
        game = self.select.values[0]
        self.stop()

        if game == POINTS_BOARD:
            pages = await build_points_pages(self.guild, self.requester_id)
            empty_message = "No one currently in the server has any points yet!"
        else:
            rows = await fetch_leaderboard_rows(game)
            pages = build_leaderboard_pages(self.guild, game, rows, self.requester_id)
            empty_message = f"No one currently in the server has played {game.title()} yet!"

        if pages is None:
            await interaction.response.edit_message(
                content=empty_message,
                embed=None,
                view=EmptyLeaderboardView(requester_id=self.requester_id, guild=self.guild),
            )
            return

        paginator = LeaderboardPaginator(requester_id=self.requester_id, pages=pages, guild=self.guild, game=game)
        await interaction.response.edit_message(content=None, embed=pages[0], view=paginator)
        paginator.message = await interaction.original_response()

class LeaderboardRoleSelectView(discord.ui.View):
    """Role picker shown before a per-role-ranks game's leaderboard, since elo (and so
    ranking) is tracked separately per role rather than once per game."""

    _MIXED = "__mixed__"

    def __init__(self, requester_id: int, guild: discord.Guild, game: str):
        super().__init__(timeout=120, disable_on_timeout=True)
        self.requester_id = requester_id
        self.guild = guild
        self.game = game

        options = [discord.SelectOption(label="Mixed (best role)", value=self._MIXED)]
        options += [discord.SelectOption(label=r, value=r) for r in config.rankable_roles(game)]
        self.select = discord.ui.Select(placeholder="Choose a role", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "This isn't your leaderboard call to flip through!", ephemeral=True
            )
            return False
        return True

    async def on_select(self, interaction: discord.Interaction) -> None:
        role = self.select.values[0]
        role = None if role == self._MIXED else role
        self.stop()

        rows = await fetch_leaderboard_rows(self.game, role)
        pages = build_leaderboard_pages(self.guild, self.game, rows, self.requester_id, role=role)

        if pages is None:
            await interaction.response.edit_message(
                content=f"No one currently in the server has played {leaderboard_label(self.game, role)} yet!",
                embed=None,
                view=EmptyLeaderboardView(requester_id=self.requester_id, guild=self.guild),
            )
            return

        paginator = LeaderboardPaginator(requester_id=self.requester_id, pages=pages, guild=self.guild, game=self.game, role=role)
        await interaction.response.edit_message(content=None, embed=pages[0], view=paginator)
        # discord.ui.View.message is only set once someone clicks, so without this a
        # paginator opened from here has nothing for on_timeout to grey out.
        paginator.message = await interaction.original_response()

class EmptyLeaderboardView(discord.ui.View):
    """Shown when a game's leaderboard has nobody on it, just a way to switch games."""

    def __init__(self, requester_id: int, guild: discord.Guild):
        super().__init__(timeout=120, disable_on_timeout=True)
        self.requester_id = requester_id
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Block anyone but whoever ran /leaderboard from switching games."""
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "This isn't your leaderboard call to flip through!", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Change Game", style=discord.ButtonStyle.primary)
    async def change_game(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Swap to a dropdown for picking a different game's leaderboard."""
        self.stop()
        await interaction.response.edit_message(content=None, view=GameSelectView(requester_id=self.requester_id, guild=self.guild))

class LeaderboardPaginator(discord.ui.View):
    """Left/right paginator over a leaderboard's pages of 10, restricted to whoever ran the command."""

    def __init__(self, requester_id: int, pages: list[discord.Embed], guild: discord.Guild, game: str, role: str | None = None):
        super().__init__(timeout=120, disable_on_timeout=True)
        self.requester_id = requester_id
        self.pages = pages
        self.guild = guild
        self.game = game
        self.role = role
        self.index = 0
        self.update_buttons()

        if is_game_board(game) and config.is_per_role_ranks(game):
            change_role_btn = discord.ui.Button(label="Change Role", style=discord.ButtonStyle.primary, row=1)
            change_role_btn.callback = self.on_change_role
            self.add_item(change_role_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Block anyone but whoever ran /leaderboard from flipping through it."""
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "This isn't your leaderboard call to flip through!", ephemeral=True,
            )
            return False
        return True
    
    def update_buttons(self) -> None:
        """Disable ◀ on the first page and ▶ on the last page."""
        self.back.disabled = (self.index == 0)
        self.forward.disabled = (self.index == len(self.pages) - 1)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def back(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Go to the previous page."""
        self.index -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def forward(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Go to the next page."""
        self.index += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Change Game", style=discord.ButtonStyle.primary, row=1)
    async def change_game(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Swap to a dropdown for picking a different game's leaderboard."""
        self.stop()
        await interaction.response.edit_message(view=GameSelectView(requester_id=self.requester_id, guild=self.guild))

    async def on_change_role(self, interaction: discord.Interaction) -> None:
        """Swap to a dropdown for picking a different role's leaderboard within the same game."""
        self.stop()
        view = LeaderboardRoleSelectView(requester_id=self.requester_id, guild=self.guild, game=self.game)
        await interaction.response.edit_message(
            content=f"Pick a role for the {self.game.title()} leaderboard:", embed=None, view=view
        )

class Leaderboard(commands.Cog):
    """Cog housing the /leaderboard command: per-game rankings, ordered by elo but displayed by win/loss record."""

    def __init__(self, bot):
        self.bot = bot

    @discord.slash_command(
        name="leaderboard",
        description="Show the top players for a game",
        guild_ids=[GUILD_ID]
    )
    async def leaderboard(
        self,
        ctx: discord.ApplicationContext,
        game: str = discord.Option (
            description= "Game (or Points) to show leaderboard for",
            choices=BOARD_CHOICES,
        ),
        role: str = discord.Option(
            description="Role to rank by (per-role-rank games only; omit for a mixed best-role leaderboard)",
            autocomplete=role_autocomplete,
            default=None,
        )
    ) -> None:
        """Show the top 10 players for a game"""
        await ctx.defer()

        if game == POINTS_BOARD:
            # `role` is deliberately never read here: points aren't per-role, so one passed
            # alongside Points is ignored rather than rejected or worked into the title.
            pages = await build_points_pages(ctx.guild, ctx.author.id)

            if pages is None:
                await ctx.followup.send(
                    "No one currently in the server has any points yet!",
                    view=EmptyLeaderboardView(requester_id=ctx.author.id, guild=ctx.guild),
                )
                return

            paginator = LeaderboardPaginator(requester_id=ctx.author.id, pages=pages, guild=ctx.guild, game=POINTS_BOARD)
            await ctx.followup.send(embed=pages[0], view=paginator)
            return

        if config.is_per_role_ranks(game):
            if role is not None and role not in config.rankable_roles(game):
                await ctx.followup.send(
                    f"Pick a role from the dropdown to see the {game.title()} leaderboard (e.g. Tank, Damage, Support)."
                )
                return
        else:
            role = None

        rows = await fetch_leaderboard_rows(game, role)
        label = leaderboard_label(game, role)

        if not rows:
            await ctx.followup.send(f"No one's played {label} yet!")
            return

        pages = build_leaderboard_pages(ctx.guild, game, rows, ctx.author.id, role=role)

        if pages is None:
            await ctx.followup.send(
                f"No one currently in the server has played {label} yet!",
                view=EmptyLeaderboardView(requester_id=ctx.author.id, guild=ctx.guild),
            )
            return

        paginator = LeaderboardPaginator(requester_id=ctx.author.id, pages=pages, guild=ctx.guild, game=game, role=role)
        await ctx.followup.send(embed=pages[0], view=paginator)

def setup(bot: discord.Bot) -> None:
    bot.add_cog(Leaderboard(bot))