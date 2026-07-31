import discord
import emoji
import re
import random
from discord.ext import commands
from urllib.parse import urlsplit
from pathlib import Path

from utils import config
from utils import db
from utils.images import get_character_image, image_attachment


GUILD_ID = config.secrets["discord"]["guild_id"]
GAME_CHOICES = list(config.game_data.keys())
CUSTOM_EMOJI_RE = re.compile(r"^<a?:\w+:(?P<id>\d+)>$")

def get_tiers(game: str) -> list[str]:
    """Returns the ordered list of rank tiers for a game, lowest to highest."""
    return config.game_data[game]["tiers"]

def get_divisions(game: str) -> int:
    """Return how many divisions each tier has for a game (e.g. 4 for League)."""
    return config.game_data[game]["divisions"]

def get_roles(game: str) -> list[str]:
    """Return the selectable roles for a game (includes "Flex")."""
    return config.game_data[game]["roles"]

def get_mains(game: str) -> list[str]:
    """Return the full character/agent/champion roster for a game."""
    return config.game_data[game]["characters"]

def tier_has_divisions(game: str, tier: str) -> bool:
    """Return whether a given tier is divided (e.g. "Gold 3") rather than flat (e.g. "Challenger")."""
    return tier not in config.game_data[game]["no_division_tiers"]

def normalize_tag(value: str | None, bot: discord.Bot) -> str | None:
    """Validate and normalize a user-supplied emoji tag.
    
    Accepts a real unicode emoji, an ascii shortcode like ":star", or a custom Disord emoji (<:name:id>). 
    Returns None if the input isnt exactly one emoji.
    """
    if not value:
        return None
    value = emoji.emojize(value.strip(), language="alias").replace("\uFE0F", "")
    match = CUSTOM_EMOJI_RE.fullmatch(value)
    if match:
        return value if bot.get_emoji(int(match.group("id"))) is not None else None
    matches = emoji.emoji_list(value)
    if len(matches) == 1 and sum(len(m["emoji"]) for m in matches) == len(value):
        return value
    return None

def compute_rank_value(game: str, tier: str, division: int) -> int:
    """Convert a tier+division into a single comprable integer."""
    index = get_tiers(game).index(tier)
    divisions = get_divisions(game)
    if tier_has_divisions(game, tier):
        return index * divisions + (division - 1)
    else:
        return index * divisions
    
def format_rank_label(game: str, tier: str, division: int) -> str:
    """Format a tier+division as a human-readable string, e.g. "Gold 3" or "Challenger"."""
    return f"{tier} {division}" if tier_has_divisions(game, tier) else tier


async def tier_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """Suggest valid tiers one the user has picked a game."""
    game = ctx.options.get("game")
    return [discord.OptionChoice(t) for t in get_tiers(game)] if game else []

async def division_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    """Suggest valid division numbers for the game+tier already picked.
    
    Returns ["1"] for tiers that don't have divisions, since the option still needs some value."""
    game, tier = ctx.options.get("game"), ctx.options.get("tier")
    if not game or not tier_has_divisions(game, tier):
        return ["1"]
    divisions_per_tier = get_divisions(game)
    return [str(d) for d in range(1, divisions_per_tier+1)]

async def roles_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """Suggest valid roles once the user has picked a game."""
    game = ctx.options.get("game")
    return [discord.OptionChoice(r) for r in get_roles(game)] if game else []

async def mains_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """Suggest valid characters/agents/champions once the user has picked a game."""
    game = ctx.options.get("game")
    return [discord.OptionChoice(m) for m in get_mains(game)] if game else []

async def picture_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    """Static choices for where a profile picture URL should go."""
    return ["Main", "Thumbnail"]

async def primary_autocomplete(ctx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
    """Suggest the user's own previously-set mains for the picked game, as primary-main candidates."""
    game = ctx.options.get("game")
    if not game:
        return []
    rows = await db.fetch_all(
        "SELECT main FROM profile_mains WHERE discordid = %s AND game = %s;",
        (ctx.interaction.user.id, game),
    )
    return [discord.OptionChoice(r[0]) for r in rows]

async def fetch_profile_data(discordid: int) -> dict:
    """Fetch and aggregate a member's full profile: bio/pictures, stats, roles, mains, and primary mains per game"""

    profile_row = await db.fetch_one(
        "SELECT bio, picture_url, thumbnail_url, tag FROM profiles WHERE discordid = %s;",
        (discordid,)
    )
    stats_rows = await db.fetch_all(
        "SELECT game, rank_label, wins, losses FROM profile_stats WHERE discordid = %s",
        (discordid,)
    )
    role_rows = await db.fetch_all(
        "SELECT game, role FROM profile_roles WHERE discordid = %s;",
        (discordid,)
    )
    main_rows = await db.fetch_all(
        "SELECT game, main FROM profile_mains WHERE discordid = %s;",
        (discordid,)
    )
    primary_rows = await db.fetch_all(
        "SELECT game, prime FROM profile_primary_mains WHERE discordid = %s;",
        (discordid,)
    )

    stats_by_game = {row[0]: row for row in stats_rows}
    roles_by_game = {}
    for g, r in role_rows:
        roles_by_game.setdefault(g, []).append(r)
    mains_by_game = {}
    for g, m in main_rows:
        mains_by_game.setdefault(g, []).append(m)
    primary_by_game = {g: p for g, p in primary_rows}

    return {
        "profile_row": profile_row,
        "stats_by_game": stats_by_game,
        "roles_by_game": roles_by_game,
        "mains_by_game": mains_by_game,
        "primary_by_game": primary_by_game,
        "total_wins": sum(row[2] for row in stats_rows),
        "total_losses": sum(row[3] for row in stats_rows),
    }

def build_home_embed(target: discord.Member, profile_row: tuple | None, total_pages: int, total_wins: int, total_losses: int, setup: bool) -> discord.Embed:
    """Build the first page of a profile: bio, win/loss record, and member-since date."""
    bio = profile_row[0] if profile_row and profile_row[0] else "No bio set."
    picture_url = profile_row[1] if profile_row and profile_row[1] else None
    thumbnail_url = profile_row[2] if profile_row and profile_row[2] else None
    tag = profile_row[3] if profile_row and profile_row[3] else "💬"

    if not setup:
        embed = discord.Embed(
            title=f"{tag} {target.display_name}'s Profile",
            color=discord.Color.from_rgb(78, 42, 132),
        )
    else:
        embed = discord.Embed(
                title=f"Editing {tag} {target.display_name}'s Profile...",
                color=discord.Color.from_rgb(78, 42, 132),
            )
        
    embed.add_field(name="Bio", value=bio, inline=False)
    embed.add_field(name="Overall Record", value=f"{total_wins}W - {total_losses}L", inline=True)
    embed.add_field(name="Member Since", value=f"<t:{int(target.joined_at.timestamp())}:D>", inline=True)
    embed.set_thumbnail(url=thumbnail_url or target.display_avatar.url)
    if picture_url:
        embed.set_image(url=picture_url)
    embed.set_footer(text=f"Page 1/{total_pages}")
    return embed

def build_game_embed(
                     target: discord.Member, 
                     game: str, 
                     row: tuple | None, 
                     roles: list[str], 
                     mains: list[str], 
                     primary_main: str | None, 
                     tag: str, 
                     page_number: int, 
                     total_pages: int,
                     setup: bool) -> tuple[discord.Embed, "Path | None"]:
    """Build one per-game page of a profile: rank, roles, mains, wins/losses.
    
    Sets a champion splash_art thumbnail if primary_main is set and an image exists."""
    rank_label = row[1] if row else "Not set"
    wins = row[2] if row else "N/A"
    losses = row[3] if row else "N/A"
    role_display = ", ".join(roles) if roles else "Not set"
    main_display = ", ".join(mains) if mains else "Not set"
    if not setup:
            embed = discord.Embed(
                    title=f"{tag} {target.display_name} - {game.title()}",
                    color=discord.Color.from_rgb(78, 42, 132),
                )
    else:
        embed = discord.Embed(
                title=f"Editing {tag} {target.display_name} - {game.title()}...",
                color=discord.Color.from_rgb(78, 42, 132),
            )
    embed.add_field(name="Rank", value=rank_label, inline=True)
    if get_roles(game):
        embed.add_field(name="Role", value=role_display, inline=True)
    embed.add_field(name="Main", value=main_display, inline=True)
    if not get_roles(game):
        embed.add_field(name="​", value="​", inline=True)
    embed.add_field(name="Wins", value=f"{wins}", inline=True)
    embed.add_field(name="Losses", value=f"{losses}", inline=True)

    # if primary_main:
    #     primary_main = (primary_main[0].upper() + primary_main[1:].lower()).replace(" ", "")
    #     if game == "league":
    #         embed.set_thumbnail(url=f"https://static.bigbrain.gg/assets/lol/riot_static/16.13.1/img/champion/{primary_main}.webp")
    image_path = None
    if primary_main:
        image_path = get_character_image(game, primary_main)
        if image_path:
            embed.set_thumbnail(url=f"attachment://{image_path.name}")
    embed.set_footer(text=f"Page {page_number}/{total_pages}")
    return embed, image_path

def is_game_head(member: discord.Member) -> bool:
    """Check if a member has a role with "game head" in its name (case-insensitive, substring match)"""
    return member.guild_permissions.administrator or any("game head" in role.name.lower() for role in member.roles)

class Profile(commands.Cog):
    """Cog housing the /profile command group:"""
    def __init__(self, bot):
        self.bot = bot

    profile = discord.SlashCommandGroup("profile", "Profile tools")
    set_grp = profile.create_subgroup("set", "Set something on your profile")

    @discord.slash_command(
        name="profile-help",
        description="Explains what each /profile command does",
        guild_ids=[GUILD_ID],
    )
    async def profile_help(self, ctx: discord.ApplicationContext) -> None:
        """Show a static help embed explaining every /profile subcommand."""
        embed = discord.Embed(
            title="Profile Commands",
            description="Everything you can do with /profile:",
            color=discord.Color.from_rgb(78, 42, 132),
        )
        embed.add_field(name="/profile view [user] [game]", value="See your (or someone else's) profile: bio, rank, roles, mains, and win/loss record.", inline=False)
        embed.add_field(name="/profile setup", value="Interactive paginated editor for your own profile — flip through pages with buttons that edit each field in place.", inline=False)
        embed.add_field(name="/profile edit", value="Alias for `/profile setup`.", inline=False)
        embed.add_field(name="/profile set bio <bio>", value="Set your profile bio.", inline=False)
        embed.add_field(name="/profile set picture [url] [position]", value="Set your profile's main image or thumbnail via a direct image URL. Clears if left blank. Defaults to main image.", inline=False)
        embed.add_field(name="/profile set rank <game> <tier> [division]", value="Set your rank for a game. Defaults to `1`.", inline=False)
        embed.add_field(name="/profile set role <game>", value="Open a menu to pick your role(s) for a game.", inline=False)
        embed.add_field(name="/profile set main <game>", value="Open a menu to set your mains for a game.", inline=False)
        embed.add_field(name="/profile set primary <game> <primary>", value="Choose which of your mains shows as your profile thumbnail/splash art.", inline=False)
        embed.add_field(name="/profile set tag [tag]", value="Set the emoji shown next to your name on your profile and in lobbies. Clears if left blank.", inline=False)
        embed.add_field(name="/profile elo [user]", value="Show a player's elo per game. Defaults to caller. For game head use only.", inline=False)
        embed.set_footer(text="🗝️ Key: /command <mandatory-arguments> [optional-arguments]")
        await ctx.respond(embed=embed, ephemeral=True)

    @set_grp.command(
            name = "bio",
            guild_ids = [GUILD_ID]
    )
    async def bio(
        self,
        ctx: discord.ApplicationContext,
        bio: discord.Option(
            str,
            name="bio",
            description="About you!"
        )
    ) -> None:
        """Set (or overwrite) your profile bio."""
        await ctx.defer(ephemeral=True)

        sql = """
            INSERT INTO profiles (discordid, bio, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid)
            DO UPDATE SET
                bio = EXCLUDED.bio,
                updated_at = CURRENT_TIMESTAMP;
        """
        await db.perform_one(sql, (ctx.author.id, bio))

        embed = discord.Embed(
            title="Bio Updated",
            description=f"{bio}",
            color=discord.Color.from_rgb(78, 42, 132),
        )
        await ctx.followup.send(embed=embed, ephemeral=True)

    @set_grp.command(
            name = "picture",
            guild_ids = [GUILD_ID]
    )
    async def picture(
        self,
        ctx: discord.ApplicationContext,
        picture: discord.Option(
            str,
            name="url",
            description="URL to picture to set on your profile",
            default=None
        ),
        option: discord.Option(
            str,
            name="position",
            description="Main or thumbnail",
            autocomplete=picture_autocomplete,
            default="main"
        )
    ) -> None:
        """Set (or overwrite) your profile's main image or thumbnail via direct image URL
        
        Rejects URLs that don't end in a known image extension before touching the database."""
        await ctx.defer(ephemeral=True)

        path = urlsplit(picture).path if picture else ""

        if picture and (not path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"))):
            await ctx.followup.send("URL must point directly to an image file (.png, .jpg, .gif, .webp, .svg).", ephemeral=True)
            return
        sql = None
        option = option.lower()
        if option == "main":
            sql = """
            INSERT INTO profiles (discordid, picture_url, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid)
            DO UPDATE SET
                picture_url = EXCLUDED.picture_url,
                updated_at = CURRENT_TIMESTAMP;
            """
        elif option == "thumbnail":
            sql = """
            INSERT INTO profiles (discordid, thumbnail_url, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid)
            DO UPDATE SET
                thumbnail_url = EXCLUDED.thumbnail_url,
                updated_at = CURRENT_TIMESTAMP;
            """

        await db.perform_one(sql, (ctx.author.id, picture))

        embed = discord.Embed(
            title="Picture updated",
            color=discord.Color.from_rgb(78, 42, 132)
        )
        embed.set_image(url=picture)
        try:
            await ctx.followup.send(embed=embed, ephemeral=True)
        except discord.HTTPException:
            await ctx.followup.send("Picture saved, but but Discord couldn't render that image — double check the link works in a browser.", ephemeral=True)

    @set_grp.command(
            name = "rank",
            guild_ids = [GUILD_ID]
    )
    async def rank(
        self, 
        ctx: discord.ApplicationContext,
        game: discord.Option(
            str,
            name="game",
            description="Game to change something about",
            choices=GAME_CHOICES,
        ),
        tier: discord.Option(
            str,
            name="tier",
            description="Your rank tier",
            autocomplete=tier_autocomplete,
        ),
        division: discord.Option(
            str,
            name="division",
            description="Your division (if applicable)",
            autocomplete=division_autocomplete,
            default="1",
        )
    ) -> None:
        """Set your rank for a game, storing both a numeric value (for balancing) and a string (for display)."""
        await ctx.defer(ephemeral=True)

        if tier not in get_tiers(game):
            await ctx.followup.send(
                "Invalid tier. Please select from dropdown.", ephemeral=True
            )
            return

        try:
            division_int = int(division)
            if division_int > get_divisions(game):
                await ctx.followup.send(
                    "Invalid division. Please select from dropdown.", ephemeral=True
                )
                return
        except ValueError:
            await ctx.followup.send(
                "Invalid division. Please select from dropdown.", ephemeral=True
            )
            return

        rank_value = compute_rank_value(game, tier, division_int)
        rank_label = format_rank_label(game, tier, division_int)

        sql = """
            INSERT INTO profile_stats (discordid, game, rank_value, rank_label, updated_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid, game)
            DO UPDATE SET
                rank_value = EXCLUDED.rank_value,
                rank_label = EXCLUDED.rank_label,
                updated_at = CURRENT_TIMESTAMP;
        """
        await db.perform_one(sql, (ctx.author.id, game, rank_value, rank_label))

        embed = discord.Embed(
            title="Rank Updated",
            description=f"{game.title()}: **{rank_label}**",
            color=discord.Color.from_rgb(78, 42, 132),
        )
        await ctx.followup.send(embed=embed, ephemeral=True)

    @set_grp.command(
            name = "role",
            guild_ids = [GUILD_ID]
    )
    async def role(
        self,
        ctx: discord.ApplicationContext,
        game: discord.Option(
            str,
            name="game",
            description="Game to change something about",
            choices=GAME_CHOICES,
        )
    ) -> None:
        """Open a multi-select menu to set your role(s) for a game."""
        await ctx.defer(ephemeral=True)

        if not get_roles(game):
            await ctx.followup.send(f"{game.title()} doesn't have roles to set!", ephemeral=True)
            return

        rows = await db.fetch_all(
            "SELECT role FROM profile_roles WHERE discordid = %s AND game = %s;",
            (ctx.author.id , game),
        )

        current_roles = [r[0] for r in rows]

        view = RoleSelectView(requester_id=ctx.author.id, game=game, current_roles=current_roles)
        await ctx.followup.send("Pick your role(s):", view=view, ephemeral=True)

    @set_grp.command(
            name = "main",
            guild_ids = [GUILD_ID]
    )
    async def main(
        self,
        ctx: discord.ApplicationContext,
        game: discord.Option(
            str,
            name="game",
            description="Game to change something about",
            choices=GAME_CHOICES,
        ),
    ) -> None:
        """Open a modal to set your mans for a game, as a free-text comma-seperated input."""
        rows = await db.fetch_all(
            "SELECT main FROM profile_mains WHERE discordid = %s AND game = %s;",
            (ctx.author.id, game),
        )
        current_mains = [r[0] for r in rows]
        await ctx.send_modal(MainsModal(requester_id=ctx.author.id, game=game, current_mains=current_mains))

    @set_grp.command(
            name = "primary",
            guild_ids = [GUILD_ID]
    )
    async def primary(
        self,
        ctx: discord.ApplicationContext,
        game: discord.Option(
            str,
            name="game",
            description="Game to change something about",
            choices=GAME_CHOICES,
        ),
        primary: discord.Option(
            str,
            name="primary",
            description="Used for the little picture on your profile",
            autocomplete=primary_autocomplete
        )
    ) -> None:
        """Set which of your own mains is used for the profile thumbnail/splash art.
        
        Must already be one of your set mains for that game.
        """
        await ctx.defer(ephemeral=True)

        rows = await db.fetch_all(
            "SELECT main FROM profile_mains WHERE discordid = %s AND game = %s;",
            (ctx.author.id, game),
        )
        mains = [r[0] for r in rows]

        if not mains:
            await ctx.followup.send("You haven't set any mains for this game yet! Use `/profile set main` first.", ephemeral = True)
            return

        if primary not in mains:
            await ctx.followup.send(f"{primary} not in your list of mains, {', '.join(mains)}.", ephemeral = True)
            return

        sql = """
            INSERT INTO profile_primary_mains (discordid, game, prime)
            VALUES (%s, %s, %s)
            ON CONFLICT (discordid, game)
            DO UPDATE SET
                prime = EXCLUDED.prime
            """

        await db.perform_one(sql, (ctx.author.id, game, primary))

        embed = discord.Embed(
            title="Primary updated",
            description=f"New primary: {primary}",
            color=discord.Color.from_rgb(78, 42, 132)
        )

        primary = primary[0].upper() + primary[1:].lower()
        if game == "league":
            embed.set_image(url=f"https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{primary}_0.jpg")
        await ctx.followup.send(embed=embed, ephemeral=True)

    @set_grp.command(
            name = "tag",
            guild_ids = [GUILD_ID]
    )
    async def tag(
        self,
        ctx: discord.ApplicationContext,
        tag: discord.Option(
            str,
            name="tag",
            description="Emoji tag to identify yourself by!",
            default=None
        )
    ) -> None:
        """Set the emoji shown next to your name on your profile and in lobbies, or clear it if ommitted."""
        await ctx.defer(ephemeral=True)

        if tag is not None:
            normalized = normalize_tag(tag, ctx.bot)
            if normalized is None:
                await ctx.followup.send("That's not a valid emoji :<", ephemeral=True)
                return
            tag = normalized

        sql = """
            INSERT INTO profiles (discordid, tag, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid)
            DO UPDATE SET
                tag = EXCLUDED.tag,
                updated_at = CURRENT_TIMESTAMP;
        """
        await db.perform_one(sql, (ctx.author.id, tag))

        embed = discord.Embed(
            title="Tag Updated!",
            description=f"New tag: {tag}" if tag else "New Tag: Default",
            color=discord.Color.from_rgb(78, 42, 132),
        )
        await ctx.followup.send(embed=embed, ephemeral=True)

    @profile.command(
            name = "view",
            guild_ids = [GUILD_ID]
    )
    async def view(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(
            discord.Member,
            description="Defaults to you",
            default=None
        ),
        game: discord.Option(
            str,
            name="game",
            description="Game to change something about",
            choices=GAME_CHOICES,
            default=None
        )
    ) -> None:
        """Show a paginated profile: A home page plus one page per game with data.
        
        Games with no rank/role/mains on file are skipped entirely, unless `game` is explicitly requested, where it opens directly to it."""
        await ctx.defer()

        target = user or ctx.author

        data = await fetch_profile_data(target.id)
        profile_row = data["profile_row"]
        stats_by_game = data["stats_by_game"]
        roles_by_game = data["roles_by_game"]
        mains_by_game = data["mains_by_game"]
        primary_by_game = data["primary_by_game"]
        total_wins = data["total_wins"]
        total_losses = data["total_losses"]

        games_with_data = {
            g for g in GAME_CHOICES if g in stats_by_game or roles_by_game.get(g) or mains_by_game.get(g) or g in primary_by_game
        }
        if game is not None:
            games_with_data.add(game)

        pages_games = [g for g in GAME_CHOICES if g in games_with_data]

        total_pages = len(pages_games) + 1
        pages = [(build_home_embed(target, profile_row, total_pages, total_wins, total_losses, setup=False), None)]
        for i, g in enumerate(pages_games, start=2):
            row = stats_by_game.get(g)
            roles = roles_by_game.get(g, [])
            mains = mains_by_game.get(g, [])
            primary_main = primary_by_game.get(g)
            tag = profile_row[3] if profile_row and profile_row[3] else "💬"
            pages.append(build_game_embed(target, g, row, roles, mains, primary_main, tag, i, total_pages, setup=False))

        if game is not None:
            start_index = pages_games.index(game) +1
        else:
            start_index = 0

        paginator = ProfilePaginator(requester_id=ctx.author.id, pages=pages, start_index=start_index)
        embed, image_path = pages[start_index]
        file = image_attachment(image_path)
        message = await ctx.followup.send(embed=embed, view=paginator, file=file)
        paginator.message = message
        await message.edit(embed=embed, view=paginator)
    
    @profile.command(
        name = "elo",
        guild_ids = [GUILD_ID]
    )
    async def elo_view(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(
            discord.Member,
            description="Player to check elo for",
            default=None
        )
    ) -> None:
        """Show a player's elo for every game they've played. Game heads only."""
        await ctx.defer(ephemeral=True)

        if not is_game_head(ctx.author):
            await ctx.followup.send("You're not a game head! Feel free to apply though...", ephemeral=True)
            return
        
        target = user or ctx.author

        rows = await db.fetch_all(
            "SELECT game, elo, games_played FROM profile_elo WHERE discordid = %s;",
            (target.id,)
        )

        tag_row = await db.fetch_one(
            "SELECT tag FROM profiles WHERE discordid = %s;",
            (target.id,)
        )
        tag = tag_row[0] if tag_row and tag_row[0] else "⚔️"

        embed = discord.Embed(
            title=f"{tag} {target.display_name}'s Elo",
            color=discord.Color.from_rgb(78, 42, 132),
        )
        elo_by_game = {game: (value, games_played) for game, value, games_played in rows}
        for game in GAME_CHOICES:
            if game in elo_by_game:
                value, games_played = elo_by_game[game]
                embed.add_field(name=game.title(), value=f"{value:.0f} elo ({games_played} games)", inline=True)

        # covers no rows at all, and rows for games no longer in GAME_CHOICES
        if not embed.fields:
            embed.description = "No elo on record for any game yet."

        await ctx.followup.send(embed=embed, ephemeral=True)


    
        
    async def _run_setup(self, ctx: discord.ApplicationContext) -> None:
        """Shared implementation behind /profile setup and /profile edit."""
        await ctx.defer(ephemeral=True)

        view = ProfileSetupView(requester_id=ctx.author.id, target=ctx.author)
        embed, image_path = await view.build_embed()
        file = image_attachment(image_path)
        message = await ctx.followup.send(embed=embed, view=view, ephemeral=True, file=file)
        view.message = message

    @profile.command(
        name = "setup",
        guild_ids = [GUILD_ID]
    )
    async def setup_cmd(self, ctx: discord.ApplicationContext) -> None:
        """Interactive paginated setup for your profile."""
        await self._run_setup(ctx)

    @profile.command(
        name = "edit",
        guild_ids = [GUILD_ID]
    )
    async def edit_cmd(self, ctx: discord.ApplicationContext) -> None:
        """Interactive paginated editor for your profile."""
        await self._run_setup(ctx)


class ProfilePaginator(discord.ui.View):
    """Left/right paginator over a list of embeds, restricted to whoever ran the command."""
    def __init__(self, requester_id, pages, start_index=0):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.pages = pages
        self.index = start_index
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Block anyone but the requester from flipping through someone else's profile"""
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "This isnt your profile call to flip through!", ephemeral=True
            )
            return False
        return True

    
    def update_buttons(self) -> None:
        """Disable ◀ on the first page and ▶ on the last page."""
        self.back.disabled = (self.index == 0)
        self.forward.disabled = (self.index == len(self.pages)-1)
    
    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def back(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Go to the previous page."""
        self.index -= 1
        self.update_buttons()
        embed, image_path = self.pages[self.index]
        file = image_attachment(image_path)
        await interaction.response.edit_message(embed=embed, view=self, file=file, attachments=[])

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def forward(self, button: discord.ui.Button, interaction: discord.Interaction) -> None:
        """Go to the next page."""
        self.index += 1
        self.update_buttons()
        embed, image_path = self.pages[self.index]
        file = image_attachment(image_path)
        await interaction.response.edit_message(embed=embed, view=self, file=file, attachments=[])
    
    async def on_timeout(self) -> None:
        """Disable both buttons once the view times out, so it doesn't look interactive anymore."""
        for child in self.children:
            child.disabled = True
        await self.message.edit(view=self)

class RoleSelectView(discord.ui.View):
    """Multi-select dropdown for a player's roles in one game.
    
    min_values=0 lets a player clear all their roles by submitting an empty selection.
    On submit, this replaces the player's full role list for that game (delete then insert) 
    rather than diffing against what was there before.
    """
    def __init__(self, requester_id: int, game: str, current_roles: list[str], on_done=None) -> None:
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.game = game
        self.on_done = on_done

        options = [
            discord.SelectOption(label=r, value=r, default=(r in current_roles))
            for r in get_roles(game)
        ]
        self.select = discord.ui.Select(
            placeholder="Choose your role(s)",
            min_values=0,
            max_values=len(options),
            options=options
        )
        self.select.callback = self.on_select
        self.add_item(self.select)
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Checks if the interactor is the original requester"""
        return interaction.user.id == self.requester_id
    
    async def on_select(self, interaction: discord.Interaction) -> bool:
        """Overwrite the player's roles for this game with whatever's currently selected."""
        chosen = self.select.values

        await db.perform_one(
            "DELETE FROM profile_roles WHERE discordid = %s AND game = %s;",
            (self.requester_id, self.game)
        )
        if chosen:
            await db.perform_many(
                "INSERT INTO profile_roles (discordid, game, role) VALUES (%s, %s, %s);",
                [(self.requester_id, self.game, r) for r in chosen],
            )
        
        if self.on_done:
            await self.on_done(interaction)
        else:
            await interaction.response.edit_message(
                content=f"Roles updated: {', '.join(chosen) if chosen else 'None'}",
                view=None,
            )

class MainsModal(discord.ui.Modal):
    """Free-text modal for setting a player's mains, as a comma-separated list.
    
    Each entry is matched case-insensitively against the game's real roster.
    If any entry doesn't match, the whole submission is rejected. 
    On success, this replaces the player's full mains list for that game, 
    same delete-then-insert pattern as RoleSelectView.
    """
    def __init__(self, requester_id: int, game: str, current_mains: list[str], on_done=None) -> None:
        super().__init__(title=f"Set your {game.title()} mains")
        self.requester_id = requester_id
        self.game = game
        self.on_done = on_done
        example_mains = ", ".join(random.sample(get_mains(game), 3))
        self.add_item(
            discord.ui.InputText(
                label="Mains (comma-seperated)",
                placeholder=f"e.g. {example_mains}",
                value=", ".join(current_mains),
                required=False
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Parse, validate, and save the submitted mains list."""
        raw = self.children[0].value or ""
        candidates = [c.strip() for c in raw.split(",") if c.strip()]

        lookup = {m.lower(): m for m in get_mains(self.game)}
        resolved, invalid = [], []
        for c in candidates:
            match = lookup.get(c.lower())
            (resolved if match else invalid).append(match or c)

        if invalid:
            await interaction.response.send_message(
                f"Didn't recognize \"{', '.join(invalid)}\". Nothing was saved, double check and try again",
                ephemeral=True
            )
            return
        
        current_primary_row = await db.fetch_one(
            "SELECT prime FROM profile_primary_mains WHERE discordid = %s AND game = %s;",
            (self.requester_id, self.game)
        )
        current_primary = current_primary_row[0] if current_primary_row else None

        await db.perform_one(
            "DELETE FROM profile_mains WHERE discordid = %s AND game = %s;",
            (self.requester_id, self.game),
        )
        if resolved:
            await db.perform_many(
                "INSERT INTO profile_mains (discordid, game, main) VALUES (%s, %s, %s);",
                [(self.requester_id, self.game, m) for m in resolved]
            )
        if current_primary and current_primary in resolved:
            await db.perform_one(
                """
                INSERT INTO profile_primary_mains (discordid, game, prime)
                VALUES (%s, %s, %s)
                ON CONFLICT (discordid, game)
                DO UPDATE SET prime = EXCLUDED.prime;
                """,
                (self.requester_id, self.game, current_primary),
            )
        if self.on_done:
            await self.on_done(interaction)
            return

        if len(resolved) == 1:
            await interaction.response.send_message(
                f"Main updated to {resolved}.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Mains updated to {', '.join(resolved[:-1])} and {resolved[-1]}.",
            ephemeral=True
        )

class RankSelectView(discord.ui.View):
    """Tier -> division cascade for setting a player's rank in one game.

    Starts with a tier dropdown. If the chosen tier has divisions, swaps to a division
    dropdown in the same message; otherwise saves immediately with division=1.
    """
    def __init__(self, requester_id: int, game: str, on_done) -> None:
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.game = game
        self.on_done = on_done
        self.tier = None

        options = [discord.SelectOption(label=t, value=t) for t in get_tiers(game)]
        self.select = discord.ui.Select(placeholder="Choose your tier", options=options)
        self.select.callback = self.on_tier_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester_id

    async def on_tier_select(self, interaction: discord.Interaction) -> None:
        self.tier = self.select.values[0]

        if not tier_has_divisions(self.game, self.tier):
            await self.save(interaction, division=1)
            return

        self.clear_items()
        divisions = get_divisions(self.game)
        options = [discord.SelectOption(label=str(d), value=str(d)) for d in range(1, divisions + 1)]
        self.division_select = discord.ui.Select(placeholder="Choose your division", options=options)
        self.division_select.callback = self.on_division_select
        self.add_item(self.division_select)
        await interaction.response.edit_message(content=f"Tier: {self.tier}. Now pick a division:", view=self)

    async def on_division_select(self, interaction: discord.Interaction) -> None:
        division = int(self.division_select.values[0])
        await self.save(interaction, division)

    async def save(self, interaction: discord.Interaction, division: int) -> None:
        rank_value = compute_rank_value(self.game, self.tier, division)
        rank_label = format_rank_label(self.game, self.tier, division)

        sql = """
            INSERT INTO profile_stats (discordid, game, rank_value, rank_label, updated_at)
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid, game)
            DO UPDATE SET
                rank_value = EXCLUDED.rank_value,
                rank_label = EXCLUDED.rank_label,
                updated_at = CURRENT_TIMESTAMP;
        """
        await db.perform_one(sql, (self.requester_id, self.game, rank_value, rank_label))
        await self.on_done(interaction)

class PrimarySelectView(discord.ui.View):
    """Single-select dropdown for choosing a primary main from a player's own mains for one game."""
    def __init__(self, requester_id: int, game: str, mains: list[str], current_primary: str | None, on_done) -> None:
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.game = game
        self.on_done = on_done

        options = [
            discord.SelectOption(label=m, value=m, default=(m == current_primary))
            for m in mains
        ]
        self.select = discord.ui.Select(placeholder="Choose your primary main", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.requester_id

    async def on_select(self, interaction: discord.Interaction) -> None:
        primary = self.select.values[0]

        sql = """
            INSERT INTO profile_primary_mains (discordid, game, prime)
            VALUES (%s, %s, %s)
            ON CONFLICT (discordid, game)
            DO UPDATE SET
                prime = EXCLUDED.prime
            """
        await db.perform_one(sql, (self.requester_id, self.game, primary))
        await self.on_done(interaction)

class TagModal(discord.ui.Modal):
    """Single-field modal for setting a player's emoji tag."""
    def __init__(self, requester_id: int, current_tag: str | None, on_done) -> None:
        super().__init__(title="Set your tag")
        self.requester_id = requester_id
        self.on_done = on_done
        self.add_item(
            discord.ui.InputText(
                label="Emoji tag",
                placeholder="e.g. :star: or an emoji",
                value=current_tag or "",
                required=False
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        raw = self.children[0].value or ""
        tag = None
        if raw.strip():
            tag = normalize_tag(raw, interaction.client)
            if tag is None:
                await interaction.response.send_message("That's not a valid emoji :<", ephemeral=True)
                return

        sql = """
            INSERT INTO profiles (discordid, tag, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid)
            DO UPDATE SET
                tag = EXCLUDED.tag,
                updated_at = CURRENT_TIMESTAMP;
        """
        await db.perform_one(sql, (self.requester_id, tag))
        await self.on_done(interaction)

class BioModal(discord.ui.Modal):
    """Single-field modal for setting a player's bio."""
    def __init__(self, requester_id: int, current_bio: str | None, on_done) -> None:
        super().__init__(title="Set your bio")
        self.requester_id = requester_id
        self.on_done = on_done
        self.add_item(
            discord.ui.InputText(
                label="Bio",
                style=discord.InputTextStyle.long,
                placeholder="About you!",
                value=current_bio or "",
                max_length=1024,
                required=False
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bio = self.children[0].value or ""

        sql = """
            INSERT INTO profiles (discordid, bio, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (discordid)
            DO UPDATE SET
                bio = EXCLUDED.bio,
                updated_at = CURRENT_TIMESTAMP;
        """
        await db.perform_one(sql, (self.requester_id, bio))
        await self.on_done(interaction)

class PictureModal(discord.ui.Modal):
    """Single-field modal for setting a player's main picture or thumbnail URL, for one fixed position."""
    def __init__(self, requester_id: int, position: str, current_url: str | None, on_done) -> None:
        super().__init__(title=f"Set your {position} picture")
        self.requester_id = requester_id
        self.position = position
        self.on_done = on_done
        self.add_item(
            discord.ui.InputText(
                label="Image URL",
                placeholder="https://example.com/image.png",
                value=current_url or "",
                required=False
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        url = self.children[0].value or None
        path = urlsplit(url).path if url else ""

        if url and not path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            await interaction.response.send_message(
                "URL must point directly to an image file (.png, .jpg, .gif, .webp, .svg).",
                ephemeral=True
            )
            return

        if self.position == "main":
            sql = """
                INSERT INTO profiles (discordid, picture_url, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (discordid)
                DO UPDATE SET
                    picture_url = EXCLUDED.picture_url,
                    updated_at = CURRENT_TIMESTAMP;
            """
        else:
            sql = """
                INSERT INTO profiles (discordid, thumbnail_url, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (discordid)
                DO UPDATE SET
                    thumbnail_url = EXCLUDED.thumbnail_url,
                    updated_at = CURRENT_TIMESTAMP;
            """

        await db.perform_one(sql, (self.requester_id, url))
        await self.on_done(interaction)

class ProfileSetupView(discord.ui.View):
    """Paginated profile editor: left/right buttons, plus edit buttons for whatever page is currently shown.

    Unlike ProfilePaginator, this never holds a static list of embeds — every page is rebuilt
    from a fresh DB fetch each time it's shown, so edits (and concurrent changes elsewhere) are
    always reflected immediately.
    """
    def __init__(self, requester_id: int, target: discord.Member, start_index: int = 0) -> None:
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.target = target
        self.index = start_index
        self.message = None
        self.build_buttons()

    @property
    def total_pages(self) -> int:
        return len(GAME_CHOICES) + 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "This isnt your profile to set up!", ephemeral=True
            )
            return False
        return True

    async def build_embed(self) -> tuple[discord.Embed, "Path | None"]:
        """Fetch fresh data and build the embed for whichever page is currently active."""
        data = await fetch_profile_data(self.target.id)
        self._data = data
        profile_row = data["profile_row"]
        tag = profile_row[3] if profile_row and profile_row[3] else "❓"

        if self.index == 0:
            return build_home_embed(self.target, profile_row, self.total_pages, data["total_wins"], data["total_losses"], setup=True), None

        game = GAME_CHOICES[self.index - 1]
        row = data["stats_by_game"].get(game)
        roles = data["roles_by_game"].get(game, [])
        mains = data["mains_by_game"].get(game, [])
        primary_main = data["primary_by_game"].get(game)
        return build_game_embed(self.target, game, row, roles, mains, primary_main, tag, self.index + 1, self.total_pages, setup=True)

    def build_buttons(self) -> None:
        """Clear and rebuild every button: nav buttons plus this page's field-edit buttons."""
        self.clear_items()

        back = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=(self.index == 0))
        back.callback = self.on_back
        self.add_item(back)

        forward = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, disabled=(self.index == self.total_pages - 1))
        forward.callback = self.on_forward
        self.add_item(forward)

        if self.index == 0:
            self.add_home_buttons()
        else:
            self.add_game_buttons(GAME_CHOICES[self.index - 1])

    def add_home_buttons(self) -> None:
        tag_btn = discord.ui.Button(label="Tag", style=discord.ButtonStyle.primary)
        tag_btn.callback = self.on_edit_tag
        self.add_item(tag_btn)

        bio_btn = discord.ui.Button(label="Bio", style=discord.ButtonStyle.primary)
        bio_btn.callback = self.on_edit_bio
        self.add_item(bio_btn)

        thumb_btn = discord.ui.Button(label="Thumbnail", style=discord.ButtonStyle.primary)
        thumb_btn.callback = self.on_edit_thumbnail
        self.add_item(thumb_btn)

        pic_btn = discord.ui.Button(label="Picture", style=discord.ButtonStyle.primary)
        pic_btn.callback = self.on_edit_picture
        self.add_item(pic_btn)

    def add_game_buttons(self, game: str) -> None:
        rank_btn = discord.ui.Button(label="Rank", style=discord.ButtonStyle.primary)
        rank_btn.callback = self.on_edit_rank
        self.add_item(rank_btn)

        if get_roles(game):
            roles_btn = discord.ui.Button(label="Roles", style=discord.ButtonStyle.primary)
            roles_btn.callback = self.on_edit_roles
            self.add_item(roles_btn)

        mains_btn = discord.ui.Button(label="Mains", style=discord.ButtonStyle.primary)
        mains_btn.callback = self.on_edit_mains
        self.add_item(mains_btn)

        data = getattr(self, "_data", {})
        has_mains = bool(data.get("mains_by_game", {}).get(game))
        primary_btn = discord.ui.Button(label="Primary", style=discord.ButtonStyle.primary, disabled=not has_mains)
        primary_btn.callback = self.on_edit_primary
        self.add_item(primary_btn)

    async def on_back(self, interaction: discord.Interaction) -> None:
        self.index -= 1
        await self.refresh_page(interaction)

    async def on_forward(self, interaction: discord.Interaction) -> None:
        self.index += 1
        await self.refresh_page(interaction)

    async def refresh_page(self, interaction: discord.Interaction) -> None:
        """Rebuild the embed and buttons for the current page, then edit the message in place.

        be careful cuz discord.ui.View already defines a sync `refresh(components)`, don't accidentally
        shadow it with your own refresh()... like I did before changing it to this :P
        """
        embed, image_path = await self.build_embed()
        self.build_buttons()
        file = image_attachment(image_path)
        await interaction.response.edit_message(content=None, embed=embed, view=self, file=file, attachments=[])

    async def on_edit_tag(self, interaction: discord.Interaction) -> None:
        data = await fetch_profile_data(self.target.id)
        current_tag = data["profile_row"][3] if data["profile_row"] else None
        await interaction.response.send_modal(TagModal(self.requester_id, current_tag, self.on_field_done))

    async def on_edit_bio(self, interaction: discord.Interaction) -> None:
        data = await fetch_profile_data(self.target.id)
        current_bio = data["profile_row"][0] if data["profile_row"] else None
        await interaction.response.send_modal(BioModal(self.requester_id, current_bio, self.on_field_done))

    async def on_edit_thumbnail(self, interaction: discord.Interaction) -> None:
        data = await fetch_profile_data(self.target.id)
        current_url = data["profile_row"][2] if data["profile_row"] else None
        await interaction.response.send_modal(PictureModal(self.requester_id, "thumbnail", current_url, self.on_field_done))

    async def on_edit_picture(self, interaction: discord.Interaction) -> None:
        data = await fetch_profile_data(self.target.id)
        current_url = data["profile_row"][1] if data["profile_row"] else None
        await interaction.response.send_modal(PictureModal(self.requester_id, "main", current_url, self.on_field_done))

    async def on_edit_rank(self, interaction: discord.Interaction) -> None:
        game = GAME_CHOICES[self.index - 1]
        view = RankSelectView(requester_id=self.requester_id, game=game, on_done=self.on_field_done)
        await interaction.response.edit_message(content=f"Pick your {game.title()} tier:", embed=None, view=view, attachments=[])

    async def on_edit_roles(self, interaction: discord.Interaction) -> None:
        game = GAME_CHOICES[self.index - 1]
        data = await fetch_profile_data(self.target.id)
        current_roles = data["roles_by_game"].get(game, [])
        view = RoleSelectView(requester_id=self.requester_id, game=game, current_roles=current_roles, on_done=self.on_field_done)
        await interaction.response.edit_message(content="Pick your role(s):", embed=None, view=view, attachments=[])

    async def on_edit_mains(self, interaction: discord.Interaction) -> None:
        game = GAME_CHOICES[self.index - 1]
        data = await fetch_profile_data(self.target.id)
        current_mains = data["mains_by_game"].get(game, [])
        await interaction.response.send_modal(MainsModal(self.requester_id, game, current_mains, self.on_field_done))

    async def on_edit_primary(self, interaction: discord.Interaction) -> None:
        game = GAME_CHOICES[self.index - 1]
        data = await fetch_profile_data(self.target.id)
        mains = data["mains_by_game"].get(game, [])
        current_primary = data["primary_by_game"].get(game)
        view = PrimarySelectView(requester_id=self.requester_id, game=game, mains=mains, current_primary=current_primary, on_done=self.on_field_done)
        await interaction.response.edit_message(content="Pick your primary main:", embed=None, view=view, attachments=[])

    async def on_field_done(self, interaction: discord.Interaction) -> None:
        """Called by field modals after a successful save; refresh this same page in place."""
        await self.refresh_page(interaction)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            await self.message.edit(view=self)


def setup(bot: discord.Bot):
    bot.add_cog(Profile(bot))