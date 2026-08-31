import asyncio
import contextlib
import random
import time

import discord
from discord.ext import commands

from utils import config, db, elo, wallet

GUILD_ID = config.secrets["discord"]["guild_id"]
GAME_CHOICES = list(config.game_data.keys())
DEFAULT_TAG = {"Lobby": "🖱️", "Winner": "🏆"}
TEAM_NAMES = [tuple(pair) for pair in config.matchmaking_data["team_names"]]
ROLE_REQUIREMENTS = {
    game: data.get("role_requirements") or {} for game, data in config.game_data.items()
}
LOBBY_SIZE = {
    game: data.get("lobby_size", 10) for game, data in config.game_data.items()
}
# Full map pool, not just maps_active. Games with no maps list just skip shuffling.
MAPS = {
    game: data["maps"] for game, data in config.game_data.items() if data.get("maps")
}
RANK_JITTER = (
    200  # half-width of the jitter range for a player exactly at the lobby average
)
JITTER_PULL_SCALE = (
    1500  # elo deviation from average that fully saturates the pull toward one side
)
_GAME_WIDE_ELO = (
    "__game__"  # elo-dict key balance_teams uses for games without per-role ranks
)
BETTING_WINDOW_SECONDS = 120  # how long after a shuffle betting stays open


def generate_embed(session: MatchmakingSession) -> discord.Embed:
    """Builds the embed for a lobby.

    Shows the waiting-room roster (two columns of joined players) if no shuffle has happened yet,
    or the shuffled team layout if it has
    """
    if session.role_assignments:
        return generate_match_embed(session)
    lobby_size = LOBBY_SIZE[session.game]
    description = f"({len(session.joined)}/{lobby_size})"
    if session.map:
        description += f" - {session.map}"
    embed = discord.Embed(
        title=f"{session.game.title()} Lobby",
        description=description,
        color=discord.Color.from_rgb(78, 42, 132),
    )
    rows_per_column = -(-lobby_size // 2)  # ceiling, so an odd lobby_size still fits
    left_rows = ["-"] * rows_per_column
    right_rows = ["-"] * rows_per_column
    for i, member in enumerate(session.joined):
        tag = session.tags.get(member.id, DEFAULT_TAG.get("Lobby"))
        entry = f"{tag} <@{member.id}>"
        row = i // 2
        if i % 2 == 0:
            left_rows[row] = entry
        else:
            right_rows[row] = entry
    embed.add_field(
        name=f"{session.team_names[0]}", value="\n".join(left_rows), inline=True
    )
    embed.add_field(
        name=f"{session.team_names[1]}", value="\n".join(right_rows), inline=True
    )
    return embed


def generate_postgame_embed(
    session: MatchmakingSession,
    team: str,
    players: list[discord.Member],
    richest_chatter: str | None = None,
) -> discord.Embed:
    """Build the "X team wins" embed after a winner is declared.

    team: the winning team's display name (not team_a/team_b, the actual name string).
    players: list of players on the winning team.
    richest_chatter: pre-built field value from build_richest_chatter_field, or None if nobody bet.
    """
    embed = discord.Embed(
        title=f"{team} Win!", color=discord.Color.from_rgb(78, 42, 132)
    )
    rows = []
    for i, member in enumerate(players):
        tag = session.tags.get(member.id, DEFAULT_TAG.get("Winner"))
        entry = f"{tag} <@{member.id}>"
        rows.append(entry)

    embed.add_field(name="Players", value="\n".join(rows), inline=True)
    if richest_chatter is not None:
        # Discord requires a non-empty name and value for embed fields. Zero-width
        # space matches the spacer pattern already used in cogs/pcs.py.
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="Richest Chatter", value=richest_chatter, inline=True)
    return embed


def generate_cancelled_embed(session: MatchmakingSession) -> discord.Embed:
    """Build the "lobby cancelled" embed shown after an admin cancels a game."""
    return discord.Embed(
        title=f"{session.game.title()} Lobby — Cancelled",
        description="This lobby was cancelled by a game head.",
        color=discord.Color.from_rgb(78, 42, 132),
    )


def generate_chatters_field(session: MatchmakingSession) -> str:
    """Build the "Chatters" field value: bettors sorted by stake (highest first).

    Each row puts the @username on the same side as the team they back.
    """
    if not session.bets:
        rows = ["No bets yet"]
    else:
        ordered = sorted(
            session.bets.items(), key=lambda item: item[1]["points"], reverse=True
        )
        rows = []
        for user_id, bet in ordered:
            if bet["team"] == "a":
                rows.append(f"<@{user_id}> - {bet['points']} points")
            else:
                rows.append(f"{bet['points']} points - <@{user_id}>")

        # Cap the list at one team's worth of rows so a heavily-bet match doesn't
        # dwarf the two team columns. Keeps the highest stakes since they're sorted first.
        team_size = LOBBY_SIZE[session.game] // 2
        if len(rows) > team_size:
            keep = max(
                team_size - 1, 1
            )  # a 1v1 game would otherwise show no rows at all
            rows = rows[:keep] + [f"...and {len(rows) - keep} more"]

    if session.betting_open and session.betting_closes_at:
        status = f"*Betting closes <t:{int(session.betting_closes_at)}:R>*"
    elif session.betting_closes_at:
        status = "*Betting closed*"
    else:
        status = None

    value = "\n".join(rows)
    if status:
        value += f"\n\n{status}"
    return value


def generate_match_embed(session: MatchmakingSession) -> discord.Embed:
    """Build the embed for a lobby that's already been shuffled into teams.

    Players are grouped by team and ordered by role (via ROLE_REQUIREMENTS).
    """
    title_suffix = session.map if session.map else "Teams"
    embed = discord.Embed(
        title=f"{session.game.title()} Lobby — {title_suffix}",
        color=discord.Color.from_rgb(78, 42, 132),
    )
    has_roles = bool(ROLE_REQUIREMENTS[session.game])
    lane_order = {lane: i for i, lane in enumerate(ROLE_REQUIREMENTS[session.game])}

    def team_rows(team):
        ordered = sorted(
            team,
            key=lambda m: lane_order.get(session.role_assignments.get(m.id, ""), 99),
        )
        rows = []
        for member in ordered:
            tag = session.tags.get(member.id, DEFAULT_TAG.get("Lobby"))
            if has_roles:
                lane = session.role_assignments.get(member.id, "?")
                rows.append(f"**{lane}** — {tag} <@{member.id}>")
            else:
                rows.append(f"{tag} <@{member.id}>")
        return "\n".join(rows) if rows else "-"

    embed.add_field(
        name=session.team_names[0], value=team_rows(session.team_a), inline=True
    )
    embed.add_field(
        name=session.team_names[1], value=team_rows(session.team_b), inline=True
    )
    embed.add_field(
        name="Chatters", value=generate_chatters_field(session), inline=True
    )
    return embed


async def get_game_shuffle_data(
    joined: list[discord.Member], game: str
) -> tuple[dict[int, float] | dict[int, dict[str, float]], dict[int, list[str]]]:
    """Fetch each joined player's elo and roles for a game, filling in defaults for missing data.

    Players missing an elo row get one seeded from their rank (see get_team_elos/
    get_team_role_elos), or the game's default_tier if they have no rank either.
    Players with no role default to ["Flex"].

    Returns (elo_by_id, roles_by_id), both keyed by discord member id. For per_role_ranks
    games, elo_by_id maps to {role: elo} per member instead of a single float.
    """
    if config.is_per_role_ranks(game):
        elo_by_id = await get_team_role_elos(game, joined)
    else:
        elo_by_id = await get_team_elos(game, joined)

    ids = [m.id for m in joined]
    role_rows = await db.fetch_all(
        "SELECT discordid, role FROM profile_roles WHERE discordid = ANY(%s) AND game = %s;",
        (ids, game),
    )
    roles_by_id = {}
    for discordid, role in role_rows:
        roles_by_id.setdefault(discordid, []).append(role)

    for member in joined:
        roles_by_id.setdefault(member.id, ["Flex"])

    return elo_by_id, roles_by_id


def balance_teams(
    game: str,
    joined: list[discord.Member],
    elo_by_id: dict[int, float] | dict[int, dict[str, float]],
    roles_by_id: dict[int, list[str]],
) -> tuple[list[discord.Member], list[discord.Member], dict[int, str]]:
    """Split joined players into two balanced teams

    Process each required role (in random order, so repeated shuffles vary) and greedily assign the needed number of players per team,
    preferring players who actually have that role, falling back to "Flex", then anyone left. Within roles, players are handed to teams
    with fewer members (tied broken by lower total rank), which matters because effective_rank can go negative when nobody has a rank set.

    A small random "jitter" `RANK_JITTER` is added to each player's rank before comparing, so the lobby doesn't shuffle to the same result every time.

    `elo_by_id` is a single float per player for games without per-role ranks, or
    {role: elo} per player for ones with (aka overwatch) -- see get_game_shuffle_data.

    Returns (team_a, team_b, assignments), where assignments maps member id ->  lane/role.
    """
    if not joined:
        return [], [], {}

    per_role = config.is_per_role_ranks(game)
    rankable = ROLE_REQUIREMENTS[game]

    # From here on both kinds of game share one shape: {player_id: {key: elo}}.
    # Per-role games are already keyed by role name; the rest file their single
    # elo under _GAME_WIDE_ELO, so every lookup below is elo_by_id[m.id][key].
    if not per_role:
        elo_by_id = {mid: {_GAME_WIDE_ELO: value} for mid, value in elo_by_id.items()}

    requirements = list(rankable.items())
    random.shuffle(requirements)

    slots_per_team = len(joined) // 2
    selected = []
    used = 0
    for role, count in requirements:
        if used + count <= slots_per_team:
            selected.append((role, count))
            used += count

    # Jittered elo for one lookup key, cached so repeated requests for the 
    # same key reuse one random draw instead of redrawing jitter each time.
    elo_cache: dict[str, dict[int, float]] = {}

    def effective_elo_for(key: str) -> dict[int, float]:
        if key not in elo_cache:
            avg = sum(elo_by_id[m.id][key] for m in joined) / len(joined)
            elo_cache[key] = {
                m.id: jittered_elo(elo_by_id[m.id][key], avg) for m in joined
            }
        return elo_cache[key]

    remaining = list(joined)
    team_a, team_b = [], []
    team_a_total, team_b_total = 0, 0
    assignments = {}

    for role, count in selected:
        # Non-per-role games always look up _GAME_WIDE_ELO (one elo, reused for
        # every bucket); per-role games look up that bucket's own role, since a
        # player's candidacy for Tank shouldn't be decided by their Support elo.
        effective_elo = effective_elo_for(role if per_role else _GAME_WIDE_ELO)

        needed_total = count * 2
        role_pool = [m for m in remaining if role in roles_by_id[m.id]]
        role_pool_ids = {m.id for m in role_pool}
        flex_pool = [
            m
            for m in remaining
            if "Flex" in roles_by_id[m.id] and m.id not in role_pool_ids
        ]

        candidates = role_pool
        if len(candidates) < needed_total:
            candidate_ids = {m.id for m in candidates}
            needed = needed_total - len(candidates)
            candidates += [m for m in flex_pool if m.id not in candidate_ids][:needed]
        if len(candidates) < needed_total:
            candidate_ids = {m.id for m in candidates}
            needed = needed_total - len(candidates)
            candidates += [m for m in remaining if m.id not in candidate_ids][:needed]

        candidates = sorted(
            candidates, key=lambda m: effective_elo[m.id], reverse=True
        )[:needed_total]

        for m in candidates:
            if len(team_a) < len(team_b) or (
                len(team_a) == len(team_b) and team_a_total <= team_b_total
            ):
                team_a.append(m)
                team_a_total += effective_elo[m.id]
            else:
                team_b.append(m)
                team_b_total += effective_elo[m.id]
            assignments[m.id] = role

        chosen_ids = {m.id for m in candidates}
        remaining = [m for m in remaining if m.id not in chosen_ids]

    # Only reachable if role_requirements doesn't fill every slot -- true for any
    # non-full Overwatch lobby, since its roles only add up to exactly lobby_size/2.
    def leftover_key(m: discord.Member) -> str:
        """Elo lookup key for a leftover player. Non-per-role games always use
        _GAME_WIDE_ELO. Flex isn't an assignable lane in a per-role-ranks game,
        so a Flex-only queue resolves to whichever rankable role they have the
        best elo in instead -- Tank/Damage/Support always have an elo entry,
        Flex never does."""
        if not per_role:
            return _GAME_WIDE_ELO
        preferred = roles_by_id[m.id][0]
        if preferred in rankable:
            return preferred
        return max(rankable, key=lambda r: elo_by_id[m.id].get(r, 0.0))

    remaining_sorted = sorted(
        remaining, key=lambda m: effective_elo_for(leftover_key(m))[m.id], reverse=True
    )
    for m in remaining_sorted:
        key = leftover_key(m)
        value = effective_elo_for(key)[m.id]
        if len(team_a) < len(team_b) or (
            len(team_a) == len(team_b) and team_a_total <= team_b_total
        ):
            team_a.append(m)
            team_a_total += value

        else:
            team_b.append(m)
            team_b_total += value
        # leftover players beyond the defined roles are always Flex, not a doubled-up role
        assignments[m.id] = key if per_role else "Flex"

    return team_a, team_b, assignments


def has_privilege(interaction: discord.Interaction) -> bool:
    """Check whether whoever clicked a button is allowed to use admin controls."""
    return config.is_game_head(interaction.user)


def must_forfeit_bet_on_declare(interaction: discord.Interaction) -> bool:
    """True if this user has a gamehead role but isn't a server admin.
    Admins skip the self-officiating forfeit rule, same trust call has_privilege already makes."""
    return not interaction.user.guild_permissions.administrator and config.is_game_head(
        interaction.user
    )


async def edit_lobby_message(
    session: MatchmakingSession, embed: discord.Embed, view: discord.ui.View | None
) -> bool:
    """Edit the public lobby message, reporting whether the edit landed.

    Every write to that message goes through here. session.message is None until the
    post-send fetch resolves and the message can be deleted out from under any caller,
    so neither case belongs at nine separate call sites.
    """
    if session.message is None:
        return False
    try:
        await session.message.edit(embed=embed, view=view)
    except discord.NotFound, discord.HTTPException:
        return False
    return True


async def refresh_lobby_message(session: MatchmakingSession) -> bool:
    """Re-render the public lobby message in whatever state the session is in now."""
    return await edit_lobby_message(
        session, generate_embed(session), LobbyView(session)
    )


async def refresh_admin_panels(session: MatchmakingSession) -> None:
    """Re-render every currently-open admin panel so they reflect the latest lobby state.

    Panels that have been dismissed/deleted are dropped instead of retried"""
    still_open = {}
    for user_id, msg in session.admin_panels.items():
        try:
            await msg.edit(embed=generate_embed(session), view=AdminView(session))
            still_open[user_id] = msg
        except discord.NotFound, discord.HTTPException:
            pass
    session.admin_panels = still_open


async def close_admin_panels(session: MatchmakingSession) -> None:
    """Delete every open admin panel. They're ephemeral messages that outlive the lobby,
    and a stale one's Shuffle would reopen betting on a match that's already over."""
    for msg in session.admin_panels.values():
        with contextlib.suppress(discord.NotFound, discord.HTTPException):
            await msg.delete()
    session.admin_panels = {}


async def start_betting_window(session: MatchmakingSession) -> None:
    """Refund any bets from the previous shuffle and open a fresh betting window.
    Cancels a previous close-timer first so re-shuffling twice doesn't leave an
    orphaned task, and refunds rather than wipes since those points were already deducted.
    """
    if session.betting_close_task is not None:
        previous = session.betting_close_task
        session.betting_close_task = None
        previous.cancel()
        # Await it out -- if its sleep already resolved, cancel() only lands at its next await.
        with contextlib.suppress(asyncio.CancelledError):
            await previous
    await refund_bets(session)
    session.betting_open = True
    session.betting_closes_at = time.time() + BETTING_WINDOW_SECONDS
    session.betting_close_task = asyncio.create_task(close_betting_after_delay(session))


async def close_betting_after_delay(session: MatchmakingSession) -> None:
    """Close betting BETTING_WINDOW_SECONDS after a shuffle and refresh the public message
    so the Bet button disables. Keeps the task reference alive through the message edit
    so a mid-edit cancel from stop_betting_window can still interrupt it.
    """
    try:
        await asyncio.sleep(BETTING_WINDOW_SECONDS)
    except asyncio.CancelledError:
        return
    # A re-shuffle landing in the same tick as the deadline can't cancel us in time.
    if session.betting_close_task is not asyncio.current_task():
        return
    session.betting_open = False
    try:
        await refresh_lobby_message(session)
    except asyncio.CancelledError:
        return
    session.betting_close_task = None


def stop_betting_window(session: MatchmakingSession) -> None:
    """Cancel any pending betting-close timer without reopening it.
    Used when the match is ending, not just being reshuffled."""
    if session.betting_close_task is not None:
        session.betting_close_task.cancel()
        session.betting_close_task = None
    session.betting_open = False


async def refund_bets(session: MatchmakingSession) -> None:
    """Refund every outstanding bet's stake and void any bet still in flight.

    Called from every point the lineup stops matching what people backed: join, leave,
    swap, re-shuffle and cancel.
    """
    # Bump before the empty check: a first bet mid-submission isn't in the book yet, but
    # it's just as stale as one that is.
    session.betting_epoch += 1
    if not session.bets:
        return
    # Clear before awaiting, not after. On the reshuffle path betting_open is still True
    # here, so a bet landing mid-credit would be booked into a dict we then throw away --
    # deducted, unrecorded, unrefunded.
    refunds = [(bet["points"], user_id) for user_id, bet in session.bets.items()]
    session.bets = {}
    await wallet.credit_many(refunds)


async def reset_to_lobby(session: MatchmakingSession) -> None:
    """Drop the shuffled teams and put the lobby back in its waiting-room state.

    Anyone who'd bet was backing a matchup that no longer exists, so their stakes go
    back and the window closes -- there's nothing to bet on until the next shuffle.
    """
    session.team_a = []
    session.team_b = []
    session.role_assignments = {}
    stop_betting_window(session)
    await refund_bets(session)


def swap_slots(session: MatchmakingSession, id_a: int, id_b: int) -> bool:
    """Swap two players' team+lane slots.

    If they're on different teams, both their team assignment and lane swap.
    If they're on the same team, only their lanes swap (team stays the same).

    Returns False (and does nothing) if either id isn't currently on team_a/team_b
    """
    member_a = next((m for m in session.team_a + session.team_b if m.id == id_a), None)
    member_b = next((m for m in session.team_a + session.team_b if m.id == id_b), None)
    if member_a is None or member_b is None:
        return False

    a_on_team_a = member_a in session.team_a
    b_on_team_a = member_b in session.team_a

    if a_on_team_a != b_on_team_a:
        if a_on_team_a:
            session.team_a.remove(member_a)
            session.team_b.remove(member_b)
            session.team_b.append(member_a)
            session.team_a.append(member_b)
        else:
            session.team_b.remove(member_a)
            session.team_a.remove(member_b)
            session.team_a.append(member_a)
            session.team_b.append(member_b)

    lane_a = session.role_assignments.get(member_a.id)
    lane_b = session.role_assignments.get(member_b.id)
    session.role_assignments[member_a.id] = lane_b
    session.role_assignments[member_b.id] = lane_a

    return True


def jittered_elo(
    player_elo: float,
    avg_elo: float,
    half_width: float = RANK_JITTER,
    pull_scale: float = JITTER_PULL_SCALE,
) -> float:
    """Add a random jitter to a player's elo, biased to pull them toward the lobby average.

    The jitter's total width stays constant, but its center slides based on how far below/above
    average the player is: someone well below average gets a jitter that's entirely upside (never
    randomly pushed even lower), someone well above average gets one that's entirely downside, and
    someone right at the average gets the old symmetric +/- jitter, unbiased either way.
    """
    deviation = avg_elo - player_elo
    pull = max(-1.0, min(1.0, deviation / pull_scale))
    center = pull * half_width
    return player_elo + random.uniform(center - half_width, center + half_width)


async def update_record(
    session: MatchmakingSession,
    winners: list[discord.Member],
    losers: list[discord.Member],
) -> None:
    """Record a win for each player in `winners` and a loss for each player in `losers`
    in profile_stats, for the current session's game."""

    sqlWin = """
            INSERT INTO profile_stats (discordid, game, wins)
            VALUES (%s, %s, 1)
            ON CONFLICT (discordid, game) DO UPDATE SET wins = profile_stats.wins + 1;
        """
    sqlLose = """
            INSERT INTO profile_stats (discordid, game, losses)
            VALUES (%s, %s, 1)
            ON CONFLICT (discordid, game) DO UPDATE SET losses = profile_stats.losses + 1;
    """
    await db.perform_many(
        sqlWin,
        [(w.id, session.game) for w in winners],
    )
    await db.perform_many(
        sqlLose,
        [(m.id, session.game) for m in losers],
    )


async def get_team_elos(game: str, members: list[discord.Member]) -> dict[int, float]:
    """Fetch each player's current elo for a game, seeding+persisting a fresh row
    from their rank if they don't have one yet."""
    ids = [m.id for m in members]

    elo_rows = await db.fetch_all(
        "SELECT discordid, elo FROM profile_elo WHERE discordid = ANY(%s) AND game = %s;",
        (ids, game),
    )
    elo_by_id = {discordid: float(value) for discordid, value in elo_rows}

    missing = [m.id for m in members if m.id not in elo_by_id]
    if missing:
        rank_rows = await db.fetch_all(
            "SELECT discordid, rank_value FROM profile_stats WHERE discordid = ANY(%s) AND game = %s;",
            (missing, game),
        )
        rank_by_id = {discordid: rank_value for discordid, rank_value in rank_rows}

        seeded = []
        for discordid in missing:
            value = elo.seed_elo(game, rank_by_id.get(discordid))
            elo_by_id[discordid] = value
            seeded.append((discordid, game, value))

        await db.perform_many(
            """
            INSERT INTO profile_elo (discordid, game, elo)
            VALUES (%s, %s, %s)
            ON CONFLICT (discordid, game) DO NOTHING
            """,
            seeded,
        )

    return elo_by_id


async def get_team_role_elos(
    game: str, members: list[discord.Member]
) -> dict[int, dict[str, float]]:
    """Per-role analogue of get_team_elos, for games with per_role_ranks.

    Returns {member_id: {role: elo}} covering every rankable role, seeding+persisting
    any (member, role) pair that doesn't have an elo row yet from that player's
    profile_role_ranks (falling back to their other set roles, or the game's default)."""
    ids = [m.id for m in members]
    roles = config.rankable_roles(game)

    elo_rows = await db.fetch_all(
        "SELECT discordid, role, elo FROM profile_role_elo WHERE discordid = ANY(%s) AND game = %s;",
        (ids, game),
    )
    elo_by_id: dict[int, dict[str, float]] = {}
    for discordid, role, value in elo_rows:
        elo_by_id.setdefault(discordid, {})[role] = float(value)

    rank_rows = await db.fetch_all(
        "SELECT discordid, role, rank_value FROM profile_role_ranks WHERE discordid = ANY(%s) AND game = %s;",
        (ids, game),
    )
    rank_by_id: dict[int, dict[str, int]] = {}
    for discordid, role, rank_value in rank_rows:
        rank_by_id.setdefault(discordid, {})[role] = rank_value

    seeded = []
    for discordid in ids:
        have = elo_by_id.setdefault(discordid, {})
        ranks = rank_by_id.get(discordid, {})
        for role in roles:
            if role not in have:
                own = ranks.get(role)
                others = [v for r, v in ranks.items() if r != role]
                value = elo.seed_role_elo(game, own, others)
                have[role] = value
                seeded.append((discordid, game, role, value))

    if seeded:
        await db.perform_many(
            """
            INSERT INTO profile_role_elo (discordid, game, role, elo)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (discordid, game, role) DO NOTHING
            """,
            seeded,
        )

    return elo_by_id


async def get_unranked(
    game: str,
    members: list[discord.Member],
    role_assignments: dict[int, str] | None = None,
) -> list[discord.Member]:
    """Members with no rank set for this game, so their elo is just the default seed.

    For per_role_ranks games, checks the rank of each member's *assigned* role instead
    of "any rank for the game" -- role_assignments should be balance_teams's output."""
    ids = [m.id for m in members]

    if config.is_per_role_ranks(game):
        if not role_assignments:
            return []
        rows = await db.fetch_all(
            "SELECT discordid, role FROM profile_role_ranks WHERE discordid = ANY(%s) AND game = %s AND rank_value IS NOT NULL;",
            (ids, game),
        )
        ranked_pairs = {(discordid, role) for discordid, role in rows}
        return [
            m for m in members if (m.id, role_assignments.get(m.id)) not in ranked_pairs
        ]

    rows = await db.fetch_all(
        "SELECT discordid FROM profile_stats WHERE discordid = ANY(%s) AND game = %s AND rank_value IS NOT NULL;",
        (ids, game),
    )
    ranked = {discordid for (discordid,) in rows}

    return [m for m in members if m.id not in ranked]


async def apply_elo_changes(session: MatchmakingSession, team_a_won: bool) -> None:
    """Update profile_elo (or profile_role_elo, for per_role_ranks games) for every
    player in the match based on the declared winner."""
    if config.is_per_role_ranks(session.game):
        team_a_role_elo = await get_team_role_elos(session.game, session.team_a)
        team_b_role_elo = await get_team_role_elos(session.game, session.team_b)
        team_a_elo = {
            m.id: team_a_role_elo[m.id][session.role_assignments[m.id]]
            for m in session.team_a
        }
        team_b_elo = {
            m.id: team_b_role_elo[m.id][session.role_assignments[m.id]]
            for m in session.team_b
        }

        deltas = elo.compute_elo_deltas(team_a_elo, team_b_elo, team_a_won)

        await db.perform_many(
            """
            UPDATE profile_role_elo
            SET elo = elo + %s, games_played = games_played + 1, updated_at = CURRENT_TIMESTAMP
            WHERE discordid = %s AND game = %s AND role = %s;
            """,
            [
                (delta, discordid, session.game, session.role_assignments[discordid])
                for discordid, delta in deltas.items()
            ],
        )
        return

    team_a_elo = await get_team_elos(session.game, session.team_a)
    team_b_elo = await get_team_elos(session.game, session.team_b)

    deltas = elo.compute_elo_deltas(team_a_elo, team_b_elo, team_a_won)

    await db.perform_many(
        """
        UPDATE profile_elo
        SET elo = elo + %s, games_played = games_played + 1, updated_at = CURRENT_TIMESTAMP
        WHERE discordid = %s AND game = %s;
        """,
        [(delta, discordid, session.game) for discordid, delta in deltas.items()],
    )


async def settle_bets(session: MatchmakingSession, team_a_won: bool) -> dict | None:
    """Pay out or refund every bet, based on which team won. Winners split the losing
    pot proportionally to their stake, same formula as cogs/points.py's Prediction.
    Refunds everyone if only one side bet. Clears session.bets before returning either way.
    """
    if not session.bets:
        return None

    team_a_bets = {
        uid: bet["points"] for uid, bet in session.bets.items() if bet["team"] == "a"
    }
    team_b_bets = {
        uid: bet["points"] for uid, bet in session.bets.items() if bet["team"] == "b"
    }
    # Everything below reads the two local dicts, so clear the book before the first await
    # rather than after it -- a bet booked mid-credit would otherwise be silently dropped.
    session.bets = {}
    winning_bets, losing_bets = (
        (team_a_bets, team_b_bets) if team_a_won else (team_b_bets, team_a_bets)
    )
    winning_pot = sum(winning_bets.values())
    losing_pot = sum(losing_bets.values())

    if not winning_bets or not losing_bets:
        refunds = {**team_a_bets, **team_b_bets}
        await wallet.credit_many([(amount, uid) for uid, amount in refunds.items()])
        return {"refunded": True, "total": sum(refunds.values())}

    multiplier = wallet.payout_multiplier(winning_pot, losing_pot)
    payouts = wallet.distribute_payouts(winning_bets, losing_pot)
    await wallet.credit_many([(amount, uid) for uid, amount in payouts.items()])
    # Multiplier is the same for every winner, so the biggest stake is also
    # the biggest payout and profit.
    richest_id = max(winning_bets, key=lambda uid: winning_bets[uid])
    summary = {
        "refunded": False,
        "multiplier": multiplier,
        "num_winners": len(winning_bets),
        "num_losers": len(losing_bets),
        "richest_bettor_id": richest_id,
        "richest_bettor_stake": winning_bets[richest_id],
        "richest_bettor_payout": payouts[richest_id],
    }
    return summary


async def build_richest_chatter_field(summary: dict | None) -> str | None:
    """Build the "Richest Chatter" embed field value. Returns None if nobody bet,
    or a short refund line if nobody backed the losing side.
    """
    if summary is None:
        return None
    if summary["refunded"]:
        return (
            f"Nobody backed the losing side!\nAll {summary['total']} points refunded."
        )

    richest_id = summary["richest_bettor_id"]
    profit = summary["richest_bettor_payout"] - summary["richest_bettor_stake"]

    tag_row = await db.fetch_one(
        "SELECT tag FROM profiles WHERE discordid = %s;", (richest_id,)
    )
    tag = tag_row[0] if tag_row and tag_row[0] else DEFAULT_TAG.get("Winner")

    winners_word = "big winner" if summary["num_winners"] == 1 else "big winners"
    losers_word = "sore loser" if summary["num_losers"] == 1 else "sore losers"

    return (
        f"{tag} <@{richest_id}>\n"
        f"*{profit} points gained*\n"
        f"**x{summary['multiplier']:.2f} payout**\n"
        f"{summary['num_winners']} {winners_word} - {summary['num_losers']} {losers_word}"
    )


class MatchmakingSession:
    """Tracks the state of one matchmaking lobby for one (channel, game) pair."""

    def __init__(self, game):
        self.game: str = game
        self.joined: list[discord.Member] = []
        self.tags: dict[int, str] = {}  # member.id to tag
        self.team_a: list[discord.Member] = []
        self.team_b: list[discord.Member] = []
        self.team_names: tuple[(str, str)] = random.choice(TEAM_NAMES)
        self.role_assignments: dict[int, str] = {}  # member.id to role
        self.map: str | None = None
        self.message: discord.Message | None = None
        self.admin_panels: dict[int, discord.InteractionMessage] = {}
        self.owner: discord.Member | None = None
        self.key: tuple[int, str] | None = None
        self.bets: dict[int, dict] = {}  # member.id to {"team": "a"|"b", "points": int}
        self.betting_open: bool = False
        self.betting_closes_at: float | None = None
        self.betting_close_task: asyncio.Task | None = None
        self.bet_locks: dict[
            int, asyncio.Lock
        ] = {}  # member.id to a lock serializing that user's bet submissions
        self.betting_epoch: int = (
            0  # bumped whenever the lineup changes, to void bets still in flight
        )
        self.ended: bool = (
            False  # set once a winner's declared or the lobby's cancelled
        )


class Matchmaking(commands.Cog):
    """Cog housing the /matchmaking command group and the active lobby state for all channels."""

    def __init__(self, bot):
        self.bot: discord.Bot = bot
        self.active_sessions: dict[tuple[int, str], MatchmakingSession] = {}

    matchmaking_group = discord.SlashCommandGroup("matchmaking", "matchmaking tools")

    @matchmaking_group.command(name="start", guild_ids=[GUILD_ID])
    async def start(
        self,
        ctx: discord.ApplicationContext,
        game: str = discord.Option(
            description="Game to matchmake for",
            choices=GAME_CHOICES
        ),
        team_a: str = discord.Option(
            description="Team A's name",
            default=None
        ),
        team_b: str = discord.Option(
            description="Team B's name",
            default=None
        ),
        team_a: discord.Option(str, description="Team A's name", default=None),
        team_b: discord.Option(str, description="Team B's name", default=None),
    ) -> None:
        """Start a new matchmaking lobby, or bump an existing one in this channel/game.

        Bumping doesn't reset the lobby, just moves it to the bottom of the channel.
        """

        if not has_privilege(ctx.interaction):
            await ctx.respond(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return

        await ctx.defer()

        key = (ctx.channel.id, game)

        if key in self.active_sessions:
            session = self.active_sessions[key]
            if session.message is not None:
                try:
                    await session.message.delete()
                except discord.NotFound:
                    pass
        else:
            session = MatchmakingSession(game)
            self.active_sessions[key] = session

        session.key = key

        if team_a:
            session.team_names = (team_a, session.team_names[1])
        if team_b:
            session.team_names = (session.team_names[0], team_b)

        view = LobbyView(session)
        embed = generate_embed(session)
        message = await ctx.followup.send(embed=embed, view=view)
        # re-fetch as a normal message so later edits use the bot's token, not the interaction webhook, which expires after 15 min
        session.message = await ctx.channel.fetch_message(message.id)
        if session.owner is None:
            session.owner = ctx.author

        await message.edit(embed=embed, view=view)


class LobbyView(discord.ui.View):
    """Shared, persistent view on the public lobby message: Join / Leave / Settings."""

    def __init__(self, session):
        super().__init__(timeout=None)
        self.session = session
        self.join.disabled = len(session.joined) >= LOBBY_SIZE[session.game]
        self.bet.disabled = not (session.role_assignments and session.betting_open)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Add whoever clicked to the lobby, unless they've already joined or it's full."""
        if any(m.id == interaction.user.id for m in self.session.joined):
            await interaction.response.send_message(
                "You've already joined!", ephemeral=True
            )
            return
        if len(self.session.joined) >= LOBBY_SIZE[self.session.game]:
            await interaction.response.send_message(
                "Lobby already full... :/", ephemeral=True
            )
            return

        # Defer before the tag lookup and the refund: reset_to_lobby is a DB round trip
        # whenever anyone had money on the old lineup, which the 3 second reply deadline
        # doesn't cover any more than shuffle's or swap's does.
        await interaction.response.defer()

        row = await db.fetch_one(
            "SELECT tag FROM profiles WHERE discordid = %s;", (interaction.user.id,)
        )
        self.session.tags[interaction.user.id] = (
            row[0] if row and row[0] else DEFAULT_TAG.get("Lobby")
        )

        self.session.joined.append(interaction.user)
        await reset_to_lobby(self.session)
        # A fresh view, not self: both button states are frozen at construction, and this
        # just closed betting and may have filled the lobby.
        await interaction.edit_original_response(
            embed=generate_embed(self.session), view=LobbyView(self.session)
        )
        await refresh_admin_panels(self.session)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger)
    async def leave(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Remove whoever clicked from the lobby, if they were in it."""
        if not any(m.id == interaction.user.id for m in self.session.joined):
            await interaction.response.send_message(
                "You haven't joined this lobby!", ephemeral=True
            )
            return

        await interaction.response.defer()

        self.session.joined = [
            m for m in self.session.joined if m.id != interaction.user.id
        ]
        self.session.tags.pop(interaction.user.id, None)
        await reset_to_lobby(self.session)
        await interaction.edit_original_response(
            embed=generate_embed(self.session), view=LobbyView(self.session)
        )
        await refresh_admin_panels(self.session)

    @discord.ui.button(label="Settings", style=discord.ButtonStyle.primary)
    async def settings(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Open a private admin panel for gameheads/the lobby owner.

        Deletes the user's previous panels first, so repeated clicks don't make multiple stale ephemeral messages."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return

        old_panel = self.session.admin_panels.get(interaction.user.id)
        if old_panel is not None:
            try:
                await old_panel.delete()
            except discord.NotFound, discord.HTTPException:
                pass

        await interaction.response.send_message(
            embed=generate_embed(self.session),
            view=AdminView(self.session),
            ephemeral=True,
        )
        panel_message = await interaction.original_response()
        self.session.admin_panels[interaction.user.id] = panel_message

    @discord.ui.button(label="Bet", style=discord.ButtonStyle.secondary)
    async def bet(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Open the team-choice view for placing or raising a bet.
        Non-admin game heads get a warning: declaring the winner themselves later
        forfeits this bet. Admins are exempt from both the warning and the forfeit.
        """
        if not self.session.role_assignments:
            await interaction.response.send_message(
                "Betting opens once the lobby's been shuffled!", ephemeral=True
            )
            return
        if not self.session.betting_open:
            await interaction.response.send_message(
                "Betting's closed for this match.", ephemeral=True
            )
            return

        prompt = f"Bet on **{self.session.team_names[0]}** or **{self.session.team_names[1]}**?"
        if must_forfeit_bet_on_declare(interaction):
            prompt += (
                "\n\n⚠️ You're a game head -- if *you* press Winner to declare this match's "
                "result, your bet will be wiped instead of paid out or refunded."
            )

        await interaction.response.send_message(
            prompt,
            view=BetTeamSelectView(self.session, interaction.user),
            ephemeral=True,
        )

def bet_rejection_reason(session: "MatchmakingSession", user_id: int, team: str) -> str | None:
    """Why this user can't back this team, or None if they can."""
    on_team_a = any(m.id == user_id for m in session.team_a)
    on_team_b = any(m.id == user_id for m in session.team_b)
    if (team == "a" and on_team_b) or (team == "b" and on_team_a):
        return "You can only bet on your own team!"

    existing = session.bets.get(user_id)
    if existing is not None and existing["team"] != team:
        return "You've already bet on the other team -- you can't switch sides!"
    return None


class BetTeamSelectView(discord.ui.View):
    """Ephemeral team picker shown after clicking Bet. Players on a team can only
    bet on themselves, and once a bet is placed the opposing team's button disables too.
    """

    def __init__(self, session: MatchmakingSession, user: discord.Member):
        super().__init__(timeout=BETTING_WINDOW_SECONDS)
        self.session = session

        team_a_button = discord.ui.Button(
            label=session.team_names[0], style=discord.ButtonStyle.primary
        )
        team_a_button.disabled = bet_rejection_reason(session, user.id, "a") is not None
        team_a_button.callback = self.make_callback("a")
        self.add_item(team_a_button)

        team_b_button = discord.ui.Button(
            label=session.team_names[1], style=discord.ButtonStyle.primary
        )
        team_b_button.disabled = bet_rejection_reason(session, user.id, "b") is not None
        team_b_button.callback = self.make_callback("b")
        self.add_item(team_b_button)

    def make_callback(self, team: str):
        async def callback(interaction: discord.Interaction) -> None:
            existing = self.session.bets.get(interaction.user.id)
            current_bet = existing["points"] if existing else 0

            # COALESCE because points is nullable, and the `if row else 0` below only
            # guards a missing row, not a row holding a NULL.
            row = await db.fetch_one(
                "SELECT COALESCE(points, 0) FROM users WHERE discordid = %s;",
                (interaction.user.id,),
            )
            balance = row[0] if row else 0

            await interaction.response.send_modal(
                BetModal(self.session, interaction.user, team, current_bet, balance)
            )

        return callback


class BetModal(discord.ui.Modal):
    """Ephemeral wager-amount prompt. The field is the new total stake, not an amount
    to add. current_bet/balance are just a label hint; callback() re-reads the stake, the
    team rules and the epoch under a per-user lock, and deducts with an atomic conditional
    UPDATE so the balance can't go negative even across lobbies.
    """

    def __init__(
        self,
        session: MatchmakingSession,
        user: discord.Member,
        team: str,
        current_bet: int,
        balance: int,
    ):
        super().__init__(title="Place your bet")
        self.session = session
        self.user = user
        self.team = team

        # Labels cap at 45 chars and both numbers are unbounded, so balance goes in the placeholder.
        if current_bet:
            label = f"New total bet (currently {current_bet})"
            placeholder = f"{balance} more available"
        else:
            label = "How many points?"
            placeholder = f"{balance} available"

        self.add_item(
            discord.ui.InputText(
                label=label,
                required=True,
                min_length=1,
                placeholder=placeholder,
            )
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.children[0].value
        try:
            new_total = int(value)
        except ValueError:
            await interaction.response.send_message(
                "You must wager a whole number of points!", ephemeral=True
            )
            return

        lock = self.session.bet_locks.setdefault(self.user.id, asyncio.Lock())
        async with lock:
            existing = self.session.bets.get(self.user.id)
            current_bet = existing["points"] if existing else 0
            epoch = self.session.betting_epoch

            # Before the raise check, so switching sides says so rather than "you can only
            # raise your bet", and before the deduction, so a rejected bet costs nothing.
            rejection = bet_rejection_reason(self.session, self.user.id, self.team)
            if rejection is not None:
                await interaction.response.send_message(rejection, ephemeral=True)
                return

            if new_total <= current_bet:
                message = (
                    "You can only raise your bet, not lower it!"
                    if current_bet
                    else "You must wager more than 0 points!"
                )
                await interaction.response.send_message(message, ephemeral=True)
                return

            delta = new_total - current_bet

            if not self.session.betting_open:
                await interaction.response.send_message(
                    "Betting closed while you were typing -- too slow!", ephemeral=True
                )
                return

            deducted = await wallet.try_deduct(self.user.id, delta)
            if not deducted:
                await interaction.response.send_message(
                    "You don't have enough points for that!", ephemeral=True
                )
                return

            # A re-shuffle mid-UPDATE refunds the book and reopens betting, so betting_open
            # alone can't tell "still valid" from "same window, different teams".
            stale = self.session.betting_epoch != epoch
            if stale or not self.session.betting_open:
                await wallet.credit(self.user.id, delta)
                message = (
                    "The teams changed while you were typing -- bet again on the new lineup."
                    if stale
                    else "Betting closed while you were typing -- too slow!"
                )
                await interaction.response.send_message(message, ephemeral=True)
                return
            self.session.bets[self.user.id] = {"team": self.team, "points": new_total}

        team_name = (
            self.session.team_names[0]
            if self.team == "a"
            else self.session.team_names[1]
        )
        await interaction.response.send_message(
            f"Bet placed: {new_total} points on **{team_name}**.", ephemeral=True
        )

        await refresh_lobby_message(self.session)
        await refresh_admin_panels(self.session)


class LobbyPanelView(discord.ui.View):
    """Base for every view reachable from a lobby's admin panel."""
    def __init__(self, session: "MatchmakingSession"):
        super().__init__(timeout=180)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.session.ended:
            await interaction.response.send_message(
                "This lobby's already over!", ephemeral=True
            )
            return False
        return True


class SwapSelectView(LobbyPanelView):
    def __init__(self, session):
        super().__init__(session)

        options = []
        for member in session.team_a + session.team_b:
            lane = session.role_assignments.get(member.id, "?")
            team = (
                session.team_names[0]
                if member in session.team_a
                else session.team_names[1]
            )
            options.append(
                discord.SelectOption(
                    label=f"{member.display_name} ({lane})",
                    description=team,
                    value=str(member.id),
                )
            )

        self.select = discord.ui.Select(
            placeholder="Pick two players to swap.",
            min_values=2,
            max_values=2,
            options=options,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.success)
        back_button.callback = self.back
        self.add_item(back_button)

    async def on_select(self, interaction: discord.Interaction):
        """Swap the two selected players' team+lane slots and refresh every open view of this lobby."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        # Restarting the window can await a close-timer that's mid-edit, and the refund is a
        # DB round trip -- more than Discord's 3 second reply deadline allows for.
        await interaction.response.defer()

        id_a, id_b = [int(v) for v in self.select.values]
        swap_slots(self.session, id_a, id_b)
        # Different lineup, so bets on the old one go back and the window restarts.
        await start_betting_window(self.session)

        await refresh_lobby_message(self.session)
        await interaction.edit_original_response(
            embed=generate_embed(self.session), view=AdminView(self.session)
        )
        await refresh_admin_panels(self.session)

    async def back(self, interaction: discord.Interaction):
        """Return to the admin panel without swapping anyone."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=generate_embed(self.session), view=AdminView(self.session)
        )


async def declare_winner(
    session: MatchmakingSession, interaction: discord.Interaction, team_a_won: bool
) -> None:
    """Shared implementation for declaring a winner: record the result, settle bets,
    post the postgame embed, end the session. Defers immediately after the privilege
    check, before any DB work, to stay under Discord's interaction-response deadline.
    """
    if not has_privilege(interaction):
        await interaction.response.send_message(
            "You're not a game head! Feel free to apply though...", ephemeral=True
        )
        return
    # Two game heads can hold open pickers at once, and a second declare would re-record
    # the result and re-apply the elo. Claim the session before the first await.
    if session.ended:
        await interaction.response.send_message(
            "This match is already over!", ephemeral=True
        )
        return
    session.ended = True

    await interaction.response.defer()

    winners, losers = (
        (session.team_a, session.team_b)
        if team_a_won
        else (session.team_b, session.team_a)
    )
    winning_team_name = session.team_names[0] if team_a_won else session.team_names[1]

    await update_record(session, winners, losers)
    await apply_elo_changes(session, team_a_won=team_a_won)
    stop_betting_window(session)
    # Forfeit here, not at the prompt: this is the only path that records a result.
    if must_forfeit_bet_on_declare(interaction):
        session.bets.pop(interaction.user.id, None)
    bet_summary = await settle_bets(session, team_a_won=team_a_won)
    richest_chatter = await build_richest_chatter_field(bet_summary)

    # The elo and the payouts are committed by now, so a failed edit gets surfaced
    # rather than swallowed.
    rendered = await edit_lobby_message(
        session,
        generate_postgame_embed(session, winning_team_name, winners, richest_chatter),
        PostgameView(session),
    )
    if not rendered:
        await interaction.followup.send(
            "Result recorded, but I couldn't update the lobby embed.", ephemeral=True
        )

    cog = interaction.client.get_cog("Matchmaking")
    cog.active_sessions.pop(session.key, None)
    await interaction.delete_original_response()
    await close_admin_panels(session)


class MapSelectView(LobbyPanelView):
    def __init__(self, session):
        super().__init__(session)

        options = [
            discord.SelectOption(label=m, default=(m == session.map))
            for m in MAPS[session.game]
        ]
        # a Select caps out at 25 options (Overwatch has 32 maps), so split across as many selects as needed
        chunks = [options[i : i + 25] for i in range(0, len(options), 25)]
        for i, chunk in enumerate(chunks):
            placeholder = (
                "Pick a map."
                if len(chunks) == 1
                else f"Pick a map ({i + 1}/{len(chunks)})."
            )
            select = discord.ui.Select(placeholder=placeholder, options=chunk)
            select.callback = self.on_select
            self.add_item(select)

        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.success)
        back_button.callback = self.back
        self.add_item(back_button)

    async def on_select(self, interaction: discord.Interaction):
        """Set the session's map and refresh every open view of this lobby."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        self.session.map = interaction.data["values"][0]

        await refresh_lobby_message(self.session)
        await interaction.response.edit_message(
            embed=generate_embed(self.session), view=AdminView(self.session)
        )
        await refresh_admin_panels(self.session)

    async def back(self, interaction: discord.Interaction):
        """Return to the admin panel without changing the map."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=generate_embed(self.session), view=AdminView(self.session)
        )


class WinnerSelectView(LobbyPanelView):
    """Ephemeral team picker for declaring a winner.

    Uses manually-constructed buttons so their labels can show the session's actual team names instead of static text."""

    def __init__(self, session):
        super().__init__(session)

        team_a_button = discord.ui.Button(
            label=session.team_names[0], style=discord.ButtonStyle.primary
        )
        team_a_button.callback = self.team_a
        self.add_item(team_a_button)

        team_b_button = discord.ui.Button(
            label=session.team_names[1], style=discord.ButtonStyle.primary
        )
        team_b_button.callback = self.team_b
        self.add_item(team_b_button)

        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.success)
        back_button.callback = self.back
        self.add_item(back_button)

    async def team_a(self, interaction: discord.Interaction) -> None:
        """Declare team_a the winner."""
        await declare_winner(self.session, interaction, team_a_won=True)

    async def team_b(self, interaction: discord.Interaction) -> None:
        """Declare team_b the winner."""
        await declare_winner(self.session, interaction, team_a_won=False)

    async def back(self, interaction: discord.Interaction) -> None:
        """Return to the admin panel without declaring a winner."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content=None,
            embed=generate_embed(self.session),
            view=AdminView(self.session),
        )


class SelfBetForfeitWarningView(LobbyPanelView):
    """Shown instead of WinnerSelectView when a non-admin game head with an active bet
    presses Winner. Requires confirmation first since declaring forfeits the bet outright.
    Uses buttons instead of a dropdown, since Select option text can get cut off on Discord's client.
    """

    def __init__(self, session: MatchmakingSession, stake: int):
        super().__init__(session)

        continue_button = discord.ui.Button(
            label=f"Continue (forfeit my {stake}-point bet)",
            style=discord.ButtonStyle.danger,
        )
        continue_button.callback = self.confirm
        self.add_item(continue_button)

        cancel_button = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary
        )
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

    async def confirm(self, interaction: discord.Interaction) -> None:
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        # Only opens the picker -- declare_winner does the forfeiting, so backing out is free.
        await interaction.response.edit_message(
            content=None,
            embed=generate_embed(self.session),
            view=WinnerSelectView(self.session),
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        await interaction.response.edit_message(
            content=None,
            embed=generate_embed(self.session),
            view=AdminView(self.session),
        )


class PostgameView(discord.ui.View):
    """Post-game view that allows for rematching."""

    def __init__(self, session):
        super().__init__(timeout=180)
        self.session = session


class AdminView(LobbyPanelView):
    """Ephemeral admin panel: Shuffle / Swap / Map / Winner / Delete. Gated to game heads."""

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.primary)
    async def shuffle(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Fetch each player's rank/role data and re-balance the lobby into two teams."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return

        if not self.session.joined:
            await interaction.response.send_message(
                "Nobody's in the lobby yet!", ephemeral=True
            )
            return

        if (len(self.session.joined) % 2) != 0:
            await interaction.response.send_message(
                "You need an even amount of players to shuffle!", ephemeral=True
            )
            return

        # Same as declare_winner: defer before any DB or API work. The shuffle query, the
        # refund and two message edits all stack up well past the 3 second deadline.
        await interaction.response.defer()

        elo_by_id, roles_by_id = await get_game_shuffle_data(
            self.session.joined, self.session.game
        )
        team_a, team_b, assignments = balance_teams(
            self.session.game, self.session.joined, elo_by_id, roles_by_id
        )
        self.session.team_a = team_a
        self.session.team_b = team_b
        self.session.role_assignments = assignments
        if MAPS.get(self.session.game):
            self.session.map = random.choice(MAPS[self.session.game])
        await start_betting_window(self.session)

        await refresh_lobby_message(self.session)
        await interaction.edit_original_response(
            embed=generate_embed(self.session), view=self
        )
        await refresh_admin_panels(self.session)

        unranked = await get_unranked(
            self.session.game, self.session.joined, self.session.role_assignments
        )
        if unranked:
            names = ", ".join(f"<@{m.id}>" for m in unranked)
            await interaction.followup.send(
                f"⚠️ Warning: no rank set for {names}. They're seeded at the default, "
                f"tell them to run `/profile rank` for a better shuffle.",
                ephemeral=True,
            )

    @discord.ui.button(label="Swap", style=discord.ButtonStyle.secondary)
    async def swap(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Open the two-player swap select menu. Requires a shuffle to have happened first."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        if not self.session.role_assignments:
            await interaction.response.send_message(
                "Shuffle first before trying to swap!", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            embed=generate_embed(self.session), view=SwapSelectView(self.session)
        )

    @discord.ui.button(label="Map", style=discord.ButtonStyle.secondary)
    async def map(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Open the map picker. Only for games with a maps list configured."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        if not MAPS.get(self.session.game):
            await interaction.response.send_message(
                f"{self.session.game.title()} doesn't have maps configured!",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            embed=generate_embed(self.session), view=MapSelectView(self.session)
        )

    @discord.ui.button(label="Winner", style=discord.ButtonStyle.success)
    async def winner(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Open the team picker to declare a winner. Requires a shuffle to have happened first.
        If the clicker is a non-admin game head with an active bet, shows a confirm
        warning first instead of opening the team picker."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return
        if not self.session.role_assignments:
            await interaction.response.send_message(
                "Shuffle first before deciding a winner!", ephemeral=True
            )
            return

        bet = self.session.bets.get(interaction.user.id)
        if bet is not None and must_forfeit_bet_on_declare(interaction):
            await interaction.response.edit_message(
                content=(
                    "⚠️ **You have an active bet on this match.** Declaring the winner "
                    "yourself will **forfeit it immediately** -- no payout, no refund."
                ),
                embed=generate_embed(self.session),
                view=SelfBetForfeitWarningView(self.session, bet["points"]),
            )
            return

        await interaction.response.edit_message(
            embed=generate_embed(self.session), view=WinnerSelectView(self.session)
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        """Cancel a game."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            embed=generate_embed(self.session), view=CancelConfirmView(self.session)
        )


class CancelConfirmView(LobbyPanelView):
    """Ephemeral confirmation step before actually cancelling a lobby.

    Uses a dropdown rather than buttons, so a misclick doesn't instantly end the game.
    """

    def __init__(self, session):
        super().__init__(session)

        options = [
            discord.SelectOption(
                label="Yes, cancel this game", value="confirm", emoji="🗑️"
            ),
            discord.SelectOption(label="No, go back", value="back", emoji="↩️"),
        ]
        self.select = discord.ui.Select(
            placeholder="Are you sure you want to cancel this game?", options=options
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        """Cancel the lobby if confirmed, otherwise return to the admin panel."""
        if not has_privilege(interaction):
            await interaction.response.send_message(
                "You're not a game head! Feel free to apply though...", ephemeral=True
            )
            return

        if self.select.values[0] == "back":
            await interaction.response.edit_message(
                embed=generate_embed(self.session), view=AdminView(self.session)
            )
            return

        # Claim the lobby before the first await, then defer: same as shuffle and
        # declare_winner, the refund and the edit outlast the 3 second reply deadline.
        self.session.ended = True
        await interaction.response.defer()

        stop_betting_window(self.session)
        await refund_bets(self.session)
        await edit_lobby_message(
            self.session, generate_cancelled_embed(self.session), None
        )

        cog = interaction.client.get_cog("Matchmaking")
        cog.active_sessions.pop(self.session.key, None)

        await interaction.delete_original_response()
        await close_admin_panels(self.session)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(Matchmaking(bot))
