import asyncio

import pytest

from cogs import matchmaking
from tests.conftest import FakeInteraction, FakeMessage

class FakeMember:
    def __init__(self, id):
        self.id = id
        self.display_name = f"player{id}"   # SwapSelectView labels its options with this

@pytest.fixture
def balance_setup(monkeypatch):
    """Fake 2-role game, no elo jitter, no shuffle, fully determinisitc"""
    monkeypatch.setattr(matchmaking.config, "game_data", {
        "fakegame": {"per_role_ranks": False}
    })
    monkeypatch.setitem(matchmaking.ROLE_REQUIREMENTS, "fakegame", {"Tank": 1, "Support": 1})
    monkeypatch.setattr(matchmaking.random, "uniform", lambda a, b:0)
    monkeypatch.setattr(matchmaking.random, "shuffle", lambda seq: None)

def test_balance_teams_empty_lobby_returns_empty(balance_setup):
    team_a, team_b, assignments = matchmaking.balance_teams("fakegame", [], {}, {})
    assert team_a == []
    assert team_b == []
    assert assignments == {}

def test_balance_teams_splits_one_of_each_role_per_team(balance_setup):
    players = [FakeMember(i) for i in (1, 2, 3, 4)]
    roles_by_id = {1: ["Tank"], 2: ["Support"], 3: ["Tank"], 4: ["Support"]}
    elo_by_id = {1: 1000, 2: 1000, 3: 1000, 4: 1000}

    team_a, team_b, assignments = matchmaking.balance_teams(
        "fakegame", players, elo_by_id, roles_by_id
    )

    assert len(team_a) == 2
    assert len(team_b) == 2
    assert assignments[1] == "Tank"
    assert assignments[3] == "Tank"
    assert assignments[2] == "Support"
    assert assignments[4] == "Support"


def test_balance_teams_assigns_every_player_exactly_once(balance_setup):
    players = [FakeMember(i) for i in (1, 2, 3, 4, 5, 6)]
    roles_by_id = {
        1: ["Tank"], 2: ["Support"], 3: ["Tank"],
        4: ["Support"], 5: ["Flex"], 6: ["Flex"],
    }
    elo_by_id = {i: 1000 for i in (1, 2, 3, 4, 5, 6)}

    team_a, team_b, assignments = matchmaking.balance_teams(
        "fakegame", players, elo_by_id, roles_by_id
    )

    all_ids = {m.id for m in team_a} | {m.id for m in team_b}
    assert all_ids == {1, 2, 3, 4, 5, 6}
    assert len(team_a) == len(team_b) == 3
    assert set(assignments.keys()) == all_ids


def test_swap_slots_different_teams_swaps_team_and_lane():
    session = matchmaking.MatchmakingSession("fakegame")
    a, b = FakeMember(1), FakeMember(2)
    session.team_a = [a]
    session.team_b = [b]
    session.role_assignments = {1: "Tank", 2: "Support"}

    assert matchmaking.swap_slots(session, 1, 2) is True
    assert a in session.team_b
    assert b in session.team_a
    assert session.role_assignments[1] == "Support"
    assert session.role_assignments[2] == "Tank"


def test_swap_slots_same_team_only_swaps_lanes():
    session = matchmaking.MatchmakingSession("fakegame")
    a, b = FakeMember(1), FakeMember(2)
    session.team_a = [a, b]
    session.team_b = []
    session.role_assignments = {1: "Tank", 2: "Support"}

    assert matchmaking.swap_slots(session, 1, 2) is True
    assert session.team_a == [a, b]
    assert session.role_assignments[1] == "Support"
    assert session.role_assignments[2] == "Tank"


def test_swap_slots_unknown_id_returns_false_and_does_nothing():
    session = matchmaking.MatchmakingSession("fakegame")
    a = FakeMember(1)
    session.team_a = [a]
    session.team_b = []
    session.role_assignments = {1: "Tank"}

    assert matchmaking.swap_slots(session, 1, 999) is False
    assert session.team_a == [a]
    assert session.role_assignments == {1: "Tank"}


# --- has_privilege ---

class FakeGuildPermissions:
    def __init__(self, administrator=False):
        self.administrator = administrator


class FakeRole:
    def __init__(self, name, id=0):
        self.name = name
        self.id = id


class FakeUser:
    def __init__(self, roles=None, administrator=False, id=0):
        self.id = id
        self.roles = roles or []
        self.guild_permissions = FakeGuildPermissions(administrator)


@pytest.fixture
def gamehead_roles(monkeypatch):
    monkeypatch.setattr(matchmaking.config, "config", {"roles": {"gameheads": {"valorant": 111}}})


GAMEHEAD_ROLE = FakeRole("Valorant Game Head", id=111)
MEMBER_ROLE = FakeRole("Member", id=222)


def gamehead(id=7):
    """A non-admin game head -- privileged, but subject to the self-officiating rules."""
    return FakeUser(roles=[GAMEHEAD_ROLE], id=id)


def admin(id=7):
    """A server admin, who is trusted past the self-officiating rules."""
    return FakeUser(roles=[GAMEHEAD_ROLE], administrator=True, id=id)


def member(id=99):
    return FakeUser(roles=[MEMBER_ROLE], id=id)


def test_has_privilege_true_for_admin():
    interaction = FakeInteraction(FakeUser(administrator=True))
    assert matchmaking.has_privilege(interaction) is True


def test_has_privilege_true_for_game_head_role(gamehead_roles):
    interaction = FakeInteraction(gamehead())
    assert matchmaking.has_privilege(interaction) is True


def test_has_privilege_false_otherwise(gamehead_roles):
    interaction = FakeInteraction(member())
    assert matchmaking.has_privilege(interaction) is False


# --- must_forfeit_bet_on_declare ---

def test_admins_are_exempt_from_the_forfeit_rule(gamehead_roles):
    """Admins keep their bet when they declare. Same trust call has_privilege makes."""
    assert matchmaking.must_forfeit_bet_on_declare(FakeInteraction(admin())) is False


def test_non_admin_game_heads_must_forfeit(gamehead_roles):
    user = gamehead()
    assert matchmaking.must_forfeit_bet_on_declare(FakeInteraction(user)) is True


def test_plain_members_have_nothing_to_forfeit(gamehead_roles):
    user = member()
    assert matchmaking.must_forfeit_bet_on_declare(FakeInteraction(user)) is False


# --- betting fixtures ---

class FakeCog:
    def __init__(self):
        self.active_sessions = {}


class FakeClient:
    def __init__(self, cog):
        self._cog = cog

    def get_cog(self, name):
        return self._cog


@pytest.fixture
def fake_db(monkeypatch):
    """Records db calls instead of running them. matchmaking does `from utils import db`,
    so patching attributes on that module object covers every call site in the cog."""
    class Recorder:
        def __init__(self):
            self.perform_many_calls = []
            self.perform_one_calls = []
            self.rowcount = 1        # what perform_one reports back to the caller
            self.fetch_one_result = None

    rec = Recorder()

    async def perform_many(sql, parameters):
        rec.perform_many_calls.append((sql, list(parameters)))

    async def perform_one(sql, parameters=None):
        rec.perform_one_calls.append((sql, parameters))
        return rec.rowcount

    async def fetch_one(sql, parameters=None):
        return rec.fetch_one_result

    monkeypatch.setattr(matchmaking.db, "perform_many", perform_many)
    monkeypatch.setattr(matchmaking.db, "perform_one", perform_one)
    monkeypatch.setattr(matchmaking.db, "fetch_one", fetch_one)
    return rec


@pytest.fixture
def declaring(betting_session):
    """A live session registered with a cog, plus a factory for the interaction that
    declares its winner. declare_winner reaches through interaction.client to pop the
    session, so the two have to be wired together."""
    cog = FakeCog()
    cog.active_sessions[betting_session.key] = betting_session

    def make(user=None):
        return FakeInteraction(user or gamehead(5), client=FakeClient(cog))

    make.cog = cog
    return make


@pytest.fixture
def betting_session(monkeypatch):
    """A shuffled 4-player lobby with fixed team names, ready to take bets.
    LOBBY_SIZE is 10 so the Chatters cap lands at 5 rows."""
    monkeypatch.setitem(matchmaking.LOBBY_SIZE, "fakegame", 10)
    monkeypatch.setitem(matchmaking.ROLE_REQUIREMENTS, "fakegame", {"Tank": 1, "Support": 1})

    session = matchmaking.MatchmakingSession("fakegame")
    session.team_names = ("Purple", "Gold")
    session.team_a = [FakeMember(1), FakeMember(2)]
    session.team_b = [FakeMember(3), FakeMember(4)]
    session.role_assignments = {1: "Tank", 2: "Support", 3: "Tank", 4: "Support"}
    session.joined = session.team_a + session.team_b
    session.message = FakeMessage()
    session.key = (555, "fakegame")
    return session


# --- generate_chatters_field ---

def test_chatters_field_says_so_when_nobody_has_bet(betting_session):
    assert matchmaking.generate_chatters_field(betting_session) == "No bets yet"


def test_chatters_field_orients_each_row_toward_its_team_column(betting_session):
    betting_session.bets = {
        7: {"team": "a", "points": 100},
        8: {"team": "b", "points": 50},
    }

    rows = matchmaking.generate_chatters_field(betting_session).split("\n")

    # The mention sits on the side of the team backed: A toward the left column, B the right
    assert rows[0] == "<@7> - 100 points"
    assert rows[1] == "50 points - <@8>"


def test_chatters_field_sorts_by_stake_descending(betting_session):
    betting_session.bets = {
        7: {"team": "a", "points": 10},
        8: {"team": "a", "points": 900},
        9: {"team": "a", "points": 250},
    }

    rows = matchmaking.generate_chatters_field(betting_session).split("\n")

    assert rows == ["<@8> - 900 points", "<@9> - 250 points", "<@7> - 10 points"]


def test_chatters_field_truncates_to_one_teams_worth_of_rows(betting_session):
    """Cap is LOBBY_SIZE//2 == 5, so 7 bettors become 4 rows plus a count of the rest."""
    betting_session.bets = {
        uid: {"team": "a", "points": uid * 10} for uid in range(1, 8)
    }

    rows = matchmaking.generate_chatters_field(betting_session).split("\n")

    assert len(rows) == 5
    assert rows[-1] == "...and 3 more"
    # The highest stakes survive the cut, since the list is sorted before truncating
    assert rows[0] == "<@7> - 70 points"


def test_chatters_field_shows_a_countdown_while_betting_is_open(betting_session):
    betting_session.bets = {7: {"team": "a", "points": 5}}
    betting_session.betting_open = True
    betting_session.betting_closes_at = 1700000000.9

    value = matchmaking.generate_chatters_field(betting_session)

    assert value.endswith("*Betting closes <t:1700000000:R>*")


def test_chatters_field_shows_closed_once_the_window_has_passed(betting_session):
    betting_session.bets = {7: {"team": "a", "points": 5}}
    betting_session.betting_open = False
    betting_session.betting_closes_at = 1700000000.0

    assert matchmaking.generate_chatters_field(betting_session).endswith("*Betting closed*")


def test_chatters_field_has_no_status_line_before_the_first_shuffle(betting_session):
    assert matchmaking.generate_chatters_field(betting_session) == "No bets yet"


# --- generate_postgame_embed ---

def test_postgame_embed_omits_the_richest_chatter_block_when_nobody_bet(betting_session):
    embed = matchmaking.generate_postgame_embed(betting_session, "Purple", betting_session.team_a)

    assert [f.name for f in embed.fields] == ["Players"]


def test_postgame_embed_spacer_field_is_never_empty(betting_session):
    """Discord rejects an embed field with an empty name or value outright, which
    400'd every win embed that had a bet on it."""
    embed = matchmaking.generate_postgame_embed(
        betting_session, "Purple", betting_session.team_a, richest_chatter="someone rich"
    )

    assert len(embed.fields) == 3
    assert embed.fields[2].name == "Richest Chatter"
    for field in embed.fields:
        assert field.name
        assert field.value


def test_postgame_embed_mentions_winners_rather_than_naming_them(betting_session):
    embed = matchmaking.generate_postgame_embed(betting_session, "Purple", betting_session.team_a)

    assert "<@1>" in embed.fields[0].value
    assert "<@2>" in embed.fields[0].value


# --- BetTeamSelectView ---

@pytest.mark.asyncio
async def test_players_can_only_bet_on_their_own_team(betting_session):
    view = matchmaking.BetTeamSelectView(betting_session, FakeMember(1))  # on team A

    team_a_button, team_b_button = view.children
    assert team_a_button.disabled is False
    assert team_b_button.disabled is True


@pytest.mark.asyncio
async def test_team_b_players_cannot_back_team_a(betting_session):
    view = matchmaking.BetTeamSelectView(betting_session, FakeMember(3))  # on team B

    team_a_button, team_b_button = view.children
    assert team_a_button.disabled is True
    assert team_b_button.disabled is False


@pytest.mark.asyncio
async def test_spectators_can_back_either_team(betting_session):
    view = matchmaking.BetTeamSelectView(betting_session, FakeMember(99))

    assert all(button.disabled is False for button in view.children)


@pytest.mark.asyncio
async def test_an_existing_bet_locks_out_the_other_side(betting_session):
    """Raising is allowed, switching sides is not."""
    betting_session.bets = {99: {"team": "a", "points": 25}}

    view = matchmaking.BetTeamSelectView(betting_session, FakeMember(99))

    team_a_button, team_b_button = view.children
    assert team_a_button.disabled is False
    assert team_b_button.disabled is True


# --- settle_bets ---

@pytest.mark.asyncio
async def test_settle_bets_returns_none_and_touches_nothing_when_nobody_bet(betting_session, fake_db):
    assert await matchmaking.settle_bets(betting_session, team_a_won=True) is None
    assert fake_db.perform_many_calls == []


@pytest.mark.asyncio
async def test_settle_bets_splits_the_losing_pot_proportionally(betting_session, fake_db):
    betting_session.bets = {
        7: {"team": "a", "points": 100},
        8: {"team": "a", "points": 50},
        9: {"team": "b", "points": 300},
    }

    summary = await matchmaking.settle_bets(betting_session, team_a_won=True)

    # 150 backing A against 300 backing B -> 1 + 300/150 == 3x
    assert summary["multiplier"] == 3.0
    _, payout_rows = fake_db.perform_many_calls[0]
    assert sorted(payout_rows) == [(150, 8), (300, 7)]
    assert summary["num_winners"] == 2
    assert summary["num_losers"] == 1
    assert betting_session.bets == {}


@pytest.mark.asyncio
async def test_settle_bets_credits_the_full_payout_not_just_the_profit(betting_session, fake_db):
    """Stakes were deducted when the bet was placed, so settlement pays the whole amount back."""
    betting_session.bets = {
        7: {"team": "a", "points": 100},
        9: {"team": "b", "points": 100},
    }

    await matchmaking.settle_bets(betting_session, team_a_won=True)

    _, payout_rows = fake_db.perform_many_calls[0]
    assert payout_rows == [(200, 7)]


@pytest.mark.asyncio
async def test_settle_bets_names_the_biggest_winner_as_richest(betting_session, fake_db):
    betting_session.bets = {
        7: {"team": "b", "points": 100},
        8: {"team": "b", "points": 400},
        9: {"team": "a", "points": 500},
    }

    summary = await matchmaking.settle_bets(betting_session, team_a_won=False)

    assert summary["richest_bettor_id"] == 8
    assert summary["richest_bettor_stake"] == 400
    assert summary["richest_bettor_payout"] == 800  # 400 * (1 + 500/500)


@pytest.mark.asyncio
async def test_settle_bets_refunds_everyone_when_only_one_side_backed_a_team(betting_session, fake_db):
    betting_session.bets = {
        7: {"team": "a", "points": 100},
        8: {"team": "a", "points": 25},
    }

    summary = await matchmaking.settle_bets(betting_session, team_a_won=True)

    assert summary == {"refunded": True, "total": 125}
    _, refund_rows = fake_db.perform_many_calls[0]
    assert sorted(refund_rows) == [(25, 8), (100, 7)]
    assert betting_session.bets == {}


@pytest.mark.asyncio
async def test_settle_bets_refunds_the_losing_side_too_when_the_winners_had_no_backers(betting_session, fake_db):
    """Nobody backed A, so the B backers get their stakes back rather than losing them."""
    betting_session.bets = {9: {"team": "b", "points": 300}}

    summary = await matchmaking.settle_bets(betting_session, team_a_won=True)

    assert summary == {"refunded": True, "total": 300}
    _, refund_rows = fake_db.perform_many_calls[0]
    assert refund_rows == [(300, 9)]


@pytest.mark.asyncio
async def test_settle_bets_rounds_payouts_to_whole_points(betting_session, fake_db):
    betting_session.bets = {
        7: {"team": "a", "points": 3},
        9: {"team": "b", "points": 10},
    }

    await matchmaking.settle_bets(betting_session, team_a_won=True)

    _, payout_rows = fake_db.perform_many_calls[0]
    assert payout_rows == [(13, 7)]  # round(3 * (1 + 10/3))


# --- refund_bets ---

@pytest.mark.asyncio
async def test_refund_bets_returns_every_stake_and_clears_the_book(betting_session, fake_db):
    betting_session.bets = {
        7: {"team": "a", "points": 100},
        9: {"team": "b", "points": 20},
    }

    await matchmaking.refund_bets(betting_session)

    _, rows = fake_db.perform_many_calls[0]
    assert sorted(rows) == [(20, 9), (100, 7)]
    assert betting_session.bets == {}


@pytest.mark.asyncio
async def test_refund_bets_is_a_no_op_with_no_bets(betting_session, fake_db):
    await matchmaking.refund_bets(betting_session)
    assert fake_db.perform_many_calls == []


# --- build_richest_chatter_field ---

@pytest.mark.asyncio
async def test_richest_chatter_field_is_absent_when_nobody_bet():
    assert await matchmaking.build_richest_chatter_field(None) is None


@pytest.mark.asyncio
async def test_richest_chatter_field_explains_a_refund():
    field = await matchmaking.build_richest_chatter_field({"refunded": True, "total": 500})

    assert "refunded" in field.lower()
    assert "500" in field


@pytest.mark.asyncio
async def test_richest_chatter_field_reports_profit_not_payout(fake_db):
    fake_db.fetch_one_result = ("👑",)
    summary = {
        "refunded": False,
        "multiplier": 3.0,
        "num_winners": 2,
        "num_losers": 3,
        "richest_bettor_id": 7,
        "richest_bettor_stake": 100,
        "richest_bettor_payout": 300,
    }

    field = await matchmaking.build_richest_chatter_field(summary)

    assert field.startswith("👑 <@7>")
    assert "*200 points gained*" in field   # payout minus the stake they'd already given up
    assert "**x3.00 payout**" in field
    assert "2 big winners - 3 sore losers" in field


@pytest.mark.asyncio
async def test_richest_chatter_field_uses_singular_wording_for_one_of_each(fake_db):
    fake_db.fetch_one_result = ("👑",)
    summary = {
        "refunded": False,
        "multiplier": 2.0,
        "num_winners": 1,
        "num_losers": 1,
        "richest_bettor_id": 7,
        "richest_bettor_stake": 50,
        "richest_bettor_payout": 100,
    }

    field = await matchmaking.build_richest_chatter_field(summary)

    assert "1 big winner - 1 sore loser" in field


@pytest.mark.asyncio
async def test_richest_chatter_field_falls_back_to_the_default_tag(fake_db):
    fake_db.fetch_one_result = None  # no profile row at all
    summary = {
        "refunded": False,
        "multiplier": 2.0,
        "num_winners": 1,
        "num_losers": 1,
        "richest_bettor_id": 7,
        "richest_bettor_stake": 50,
        "richest_bettor_payout": 100,
    }

    field = await matchmaking.build_richest_chatter_field(summary)

    assert field.startswith(f"{matchmaking.DEFAULT_TAG['Winner']} <@7>")


@pytest.mark.asyncio
async def test_richest_chatter_field_falls_back_when_the_tag_is_null(fake_db):
    fake_db.fetch_one_result = (None,)
    summary = {
        "refunded": False,
        "multiplier": 2.0,
        "num_winners": 1,
        "num_losers": 1,
        "richest_bettor_id": 7,
        "richest_bettor_stake": 50,
        "richest_bettor_payout": 100,
    }

    field = await matchmaking.build_richest_chatter_field(summary)

    assert field.startswith(f"{matchmaking.DEFAULT_TAG['Winner']} <@7>")


# --- betting window lifecycle ---

@pytest.fixture
def instant_window(monkeypatch):
    """Collapses the 2-minute window so the close timer fires within the test."""
    monkeypatch.setattr(matchmaking, "BETTING_WINDOW_SECONDS", 0)


@pytest.mark.asyncio
async def test_start_betting_window_opens_betting_and_sets_a_deadline(betting_session, fake_db):
    await matchmaking.start_betting_window(betting_session)
    try:
        assert betting_session.betting_open is True
        assert betting_session.betting_closes_at > 0
        assert betting_session.betting_close_task is not None
    finally:
        matchmaking.stop_betting_window(betting_session)


@pytest.mark.asyncio
async def test_reshuffling_refunds_the_previous_round_rather_than_pocketing_it(betting_session, fake_db):
    """Stakes are deducted up front, so wiping the book on reshuffle would confiscate them."""
    betting_session.bets = {7: {"team": "a", "points": 100}}

    await matchmaking.start_betting_window(betting_session)
    try:
        _, rows = fake_db.perform_many_calls[0]
        assert rows == [(100, 7)]
        assert betting_session.bets == {}
    finally:
        matchmaking.stop_betting_window(betting_session)


@pytest.mark.asyncio
async def test_reshuffling_cancels_the_previous_close_timer(betting_session, fake_db):
    await matchmaking.start_betting_window(betting_session)
    first_task = betting_session.betting_close_task

    await matchmaking.start_betting_window(betting_session)
    try:
        await asyncio.sleep(0)
        assert first_task.cancelled()
        assert betting_session.betting_close_task is not first_task
    finally:
        matchmaking.stop_betting_window(betting_session)


@pytest.mark.asyncio
async def test_the_window_closes_itself_and_refreshes_the_lobby(betting_session, fake_db, instant_window):
    await matchmaking.start_betting_window(betting_session)
    await betting_session.betting_close_task

    assert betting_session.betting_open is False
    assert betting_session.betting_close_task is None
    assert len(betting_session.message.edit_calls) == 1


@pytest.mark.asyncio
async def test_stop_betting_window_closes_betting_without_reopening_it(betting_session, fake_db):
    await matchmaking.start_betting_window(betting_session)
    task = betting_session.betting_close_task

    matchmaking.stop_betting_window(betting_session)

    assert betting_session.betting_open is False
    assert betting_session.betting_close_task is None
    await asyncio.sleep(0)
    assert task.cancelled()


def test_stop_betting_window_is_safe_with_no_timer_running(betting_session):
    matchmaking.stop_betting_window(betting_session)
    assert betting_session.betting_open is False


# --- BetModal ---

def make_bet_modal(session, user, team="a", value="100", current_bet=0, balance=1000):
    modal = matchmaking.BetModal(session, user, team, current_bet, balance)
    modal.children[0].value = value
    return modal


@pytest.mark.asyncio
async def test_bet_modal_rejects_a_non_numeric_wager(betting_session, fake_db):
    betting_session.betting_open = True
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="all of it")
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert "whole number" in interaction.response.messages[0]["content"]
    assert fake_db.perform_one_calls == []
    assert betting_session.bets == {}


@pytest.mark.asyncio
async def test_bet_modal_accepts_non_ascii_digits(betting_session, fake_db):
    """int() reads Arabic-Indic and fullwidth digits as numbers, so these are real
    wagers rather than crashes. The balance guard covers them like any other."""
    betting_session.betting_open = True
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="١٢٣")

    await modal.callback(FakeInteraction(user))

    assert betting_session.bets == {7: {"team": "a", "points": 123}}


@pytest.mark.asyncio
async def test_bet_modal_rejects_zero(betting_session, fake_db):
    betting_session.betting_open = True
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="0")
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert "more than 0" in interaction.response.messages[0]["content"]
    assert fake_db.perform_one_calls == []


@pytest.mark.asyncio
async def test_bet_modal_refuses_to_lower_an_existing_bet(betting_session, fake_db):
    betting_session.betting_open = True
    betting_session.bets = {7: {"team": "a", "points": 500}}
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="200", current_bet=500)
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert "only raise" in interaction.response.messages[0]["content"]
    assert betting_session.bets[7]["points"] == 500
    assert fake_db.perform_one_calls == []


@pytest.mark.asyncio
async def test_bet_modal_rejects_a_wager_placed_after_the_window_shut(betting_session, fake_db):
    betting_session.betting_open = False
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="100")
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert "closed while you were typing" in interaction.response.messages[0]["content"]
    assert fake_db.perform_one_calls == []
    assert betting_session.bets == {}


@pytest.mark.asyncio
async def test_bet_modal_records_the_bet_and_deducts_the_stake(betting_session, fake_db):
    betting_session.betting_open = True
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, team="b", value="100")
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    sql, params = fake_db.perform_one_calls[0]
    assert "points >= %s" in sql          # the guard is in the statement, not a prior SELECT
    assert params == (100, 7, 100)
    assert betting_session.bets == {7: {"team": "b", "points": 100}}
    assert "Gold" in interaction.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_raising_a_bet_only_charges_the_difference(betting_session, fake_db):
    betting_session.betting_open = True
    betting_session.bets = {7: {"team": "a", "points": 100}}
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="250", current_bet=100)
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    _, params = fake_db.perform_one_calls[0]
    assert params == (150, 7, 150)        # the delta, not the new total
    assert betting_session.bets[7]["points"] == 250


@pytest.mark.asyncio
async def test_bet_modal_leaves_the_book_untouched_when_the_deduction_is_refused(betting_session, fake_db):
    """rowcount 0 means the WHERE points >= guard rejected it. Recording the bet
    anyway would hand out a free stake."""
    betting_session.betting_open = True
    fake_db.rowcount = 0
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="100")
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert "don't have enough points" in interaction.response.messages[0]["content"]
    assert betting_session.bets == {}


@pytest.mark.asyncio
async def test_bet_modal_refreshes_the_lobby_and_any_open_admin_panels(betting_session, fake_db):
    betting_session.betting_open = True
    panel = FakeMessage()
    betting_session.admin_panels = {42: panel}
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="100")

    await modal.callback(FakeInteraction(user))

    assert len(betting_session.message.edit_calls) == 1
    assert len(panel.edit_calls) == 1


# --- LobbyView.bet ---

@pytest.mark.asyncio
async def test_bet_button_waits_for_a_shuffle(betting_session, gamehead_roles):
    betting_session.role_assignments = {}
    view = matchmaking.LobbyView(betting_session)
    interaction = FakeInteraction(FakeUser(id=7))

    await view.bet.callback(interaction)

    assert "once the lobby's been shuffled" in interaction.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_bet_button_refuses_once_the_window_is_shut(betting_session, gamehead_roles):
    betting_session.betting_open = False
    view = matchmaking.LobbyView(betting_session)
    interaction = FakeInteraction(FakeUser(id=7))

    await view.bet.callback(interaction)

    assert "Betting's closed" in interaction.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_bet_button_warns_game_heads_about_the_forfeit_rule(betting_session, gamehead_roles):
    betting_session.betting_open = True
    view = matchmaking.LobbyView(betting_session)
    user = gamehead(7)

    interaction = FakeInteraction(user)
    await view.bet.callback(interaction)

    assert "your bet will be wiped" in interaction.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_bet_button_does_not_warn_admins(betting_session, gamehead_roles):
    betting_session.betting_open = True
    view = matchmaking.LobbyView(betting_session)
    user = admin(7)

    interaction = FakeInteraction(user)
    await view.bet.callback(interaction)

    assert "your bet will be wiped" not in interaction.response.messages[0]["content"]


# --- AdminView.winner routing ---

@pytest.mark.asyncio
async def test_winner_button_is_gated_to_game_heads(betting_session, gamehead_roles):
    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(member(7))

    await view.winner.callback(interaction)

    assert "not a game head" in interaction.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_winner_button_requires_a_shuffle_first(betting_session, gamehead_roles):
    betting_session.role_assignments = {}
    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(gamehead(7))

    await view.winner.callback(interaction)

    assert "Shuffle first" in interaction.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_a_betting_game_head_is_warned_before_declaring(betting_session, gamehead_roles):
    betting_session.bets = {7: {"team": "a", "points": 250}}
    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(gamehead(7))

    await view.winner.callback(interaction)

    edit = interaction.response.edits[0]
    assert "forfeit it immediately" in edit["content"]
    assert isinstance(edit["view"], matchmaking.SelfBetForfeitWarningView)


@pytest.mark.asyncio
async def test_an_admin_with_a_bet_goes_straight_to_the_team_picker(betting_session, gamehead_roles):
    betting_session.bets = {7: {"team": "a", "points": 250}}
    view = matchmaking.AdminView(betting_session)
    user = admin(7)

    interaction = FakeInteraction(user)
    await view.winner.callback(interaction)

    assert isinstance(interaction.response.edits[0]["view"], matchmaking.WinnerSelectView)


@pytest.mark.asyncio
async def test_a_game_head_without_a_bet_skips_the_warning(betting_session, gamehead_roles):
    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(gamehead(7))

    await view.winner.callback(interaction)

    assert isinstance(interaction.response.edits[0]["view"], matchmaking.WinnerSelectView)


# --- SelfBetForfeitWarningView ---

@pytest.mark.asyncio
async def test_confirming_the_forfeit_only_opens_the_picker(betting_session, gamehead_roles):
    """The stake survives confirmation. declare_winner is what actually forfeits it,
    so abandoning the picker here costs nothing."""
    betting_session.bets = {7: {"team": "a", "points": 250}}
    view = matchmaking.SelfBetForfeitWarningView(betting_session, 250)
    interaction = FakeInteraction(gamehead(7))

    await view.confirm(interaction)

    assert betting_session.bets[7]["points"] == 250
    assert isinstance(interaction.response.edits[0]["view"], matchmaking.WinnerSelectView)


@pytest.mark.asyncio
async def test_backing_out_after_confirming_costs_nothing(betting_session, gamehead_roles):
    betting_session.bets = {7: {"team": "a", "points": 250}}
    user = gamehead(7)

    await matchmaking.SelfBetForfeitWarningView(betting_session, 250).confirm(FakeInteraction(user))
    await matchmaking.WinnerSelectView(betting_session).back(FakeInteraction(user))

    assert betting_session.bets[7]["points"] == 250


@pytest.mark.asyncio
async def test_cancelling_the_forfeit_keeps_the_bet(betting_session, gamehead_roles):
    betting_session.bets = {7: {"team": "a", "points": 250}}
    view = matchmaking.SelfBetForfeitWarningView(betting_session, 250)
    interaction = FakeInteraction(gamehead(7))

    await view.cancel(interaction)

    assert betting_session.bets[7]["points"] == 250
    assert isinstance(interaction.response.edits[0]["view"], matchmaking.AdminView)


@pytest.mark.asyncio
async def test_forfeiting_the_only_backer_of_a_side_refunds_the_rest(
    betting_session, fake_db, gamehead_roles, no_record_keeping, declaring
):
    """The forfeit leaves one side unbacked, which collapses settlement into the
    refund-everyone branch rather than paying a 1x 'win'."""
    betting_session.bets = {7: {"team": "a", "points": 250}, 8: {"team": "b", "points": 10}}
    interaction = declaring(gamehead(7))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=False)

    _, rows = fake_db.perform_many_calls[0]
    assert rows == [(10, 8)]   # the surviving bettor refunded, the forfeiter paid nothing


@pytest.mark.asyncio
async def test_declaring_forfeits_the_declarers_own_bet(
    betting_session, fake_db, gamehead_roles, no_record_keeping, declaring
):
    """Closes the self-officiating hole: reaching declare_winner through a picker that
    was opened before the bet existed still forfeits, since AdminView never saw it."""
    betting_session.bets = {7: {"team": "a", "points": 250}, 8: {"team": "b", "points": 100}}
    interaction = declaring(gamehead(7))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=True)

    _, rows = fake_db.perform_many_calls[0]
    assert rows == [(100, 8)]   # only the other side, refunded; no payout to the declarer


@pytest.mark.asyncio
async def test_an_admin_declaring_still_gets_paid(
    betting_session, fake_db, gamehead_roles, no_record_keeping, declaring
):
    betting_session.bets = {7: {"team": "a", "points": 100}, 8: {"team": "b", "points": 100}}
    interaction = declaring(admin(7))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=True)

    _, rows = fake_db.perform_many_calls[0]
    assert rows == [(200, 7)]


# --- declare_winner ---

@pytest.fixture
def no_record_keeping(monkeypatch):
    """Stubs out the elo/record writes, which this PR didn't touch, so the test is
    about the betting settlement rather than the scoreboard."""
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(matchmaking, "update_record", noop)
    monkeypatch.setattr(matchmaking, "apply_elo_changes", noop)


@pytest.mark.asyncio
async def test_declare_winner_is_gated_to_game_heads(betting_session, gamehead_roles, no_record_keeping):
    interaction = FakeInteraction(member(7))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=True)

    assert "not a game head" in interaction.response.messages[0]["content"]
    assert interaction.response.deferred is False


@pytest.mark.asyncio
async def test_declare_winner_defers_before_doing_any_work(betting_session, fake_db, gamehead_roles, no_record_keeping, declaring):
    """Discord kills an interaction that isn't answered in 3 seconds, and settlement
    plus elo writes take longer than that."""
    interaction = declaring(gamehead(7))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=True)

    assert interaction.response.deferred is True


@pytest.mark.asyncio
async def test_declare_winner_settles_bets_into_the_postgame_embed(betting_session, fake_db, gamehead_roles, no_record_keeping, declaring):
    fake_db.fetch_one_result = ("👑",)
    betting_session.bets = {
        7: {"team": "a", "points": 100},
        9: {"team": "b", "points": 100},
    }
    interaction = declaring(gamehead(5))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=True)

    embed = betting_session.message.edit_calls[0]["embed"]
    assert embed.title == "Purple Win!"
    assert embed.fields[2].name == "Richest Chatter"
    assert "<@7>" in embed.fields[2].value
    assert betting_session.bets == {}


@pytest.mark.asyncio
async def test_declare_winner_stops_the_betting_timer(betting_session, fake_db, gamehead_roles, no_record_keeping, declaring):
    await matchmaking.start_betting_window(betting_session)
    interaction = declaring(gamehead(5))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=True)

    assert betting_session.betting_open is False
    assert betting_session.betting_close_task is None


@pytest.mark.asyncio
async def test_declare_winner_ends_the_session(betting_session, fake_db, gamehead_roles, no_record_keeping, declaring):
    interaction = declaring(gamehead(5))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=True)

    assert declaring.cog.active_sessions == {}
    assert interaction.original_response_deleted is True


@pytest.mark.asyncio
async def test_declare_winner_surfaces_a_failed_embed_edit(betting_session, fake_db, gamehead_roles, no_record_keeping, declaring):
    """The result is already written by this point, so a silent failure would leave
    the declarer thinking nothing happened."""
    import discord

    async def boom(**kwargs):
        raise discord.HTTPException(_FakeResponse(), "nope")

    betting_session.message.edit = boom
    interaction = declaring(gamehead(5))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=True)

    assert "couldn't update the lobby embed" in interaction.followup.send_calls[0]["content"]
    assert declaring.cog.active_sessions == {}


@pytest.mark.asyncio
async def test_declare_winner_survives_a_lobby_that_never_got_its_message(
    betting_session, fake_db, gamehead_roles, no_record_keeping, declaring
):
    """session.message stays None if the post-send fetch_message never lands, and the
    elo and payouts are already committed by the time we'd reach for it."""
    betting_session.message = None
    interaction = declaring(gamehead(5))

    await matchmaking.declare_winner(betting_session, interaction, team_a_won=True)

    assert "couldn't update the lobby embed" in interaction.followup.send_calls[0]["content"]
    assert declaring.cog.active_sessions == {}   # still torn down rather than left stuck


class _FakeResponse:
    """Minimal stand-in for the aiohttp response discord.HTTPException wants."""
    status = 500
    reason = "Internal Server Error"


# --- CancelConfirmView ---

@pytest.mark.asyncio
async def test_cancelling_a_lobby_refunds_every_outstanding_bet(betting_session, fake_db, gamehead_roles):
    betting_session.bets = {7: {"team": "a", "points": 100}}
    await matchmaking.start_betting_window(betting_session)
    betting_session.bets = {7: {"team": "a", "points": 100}}
    fake_db.perform_many_calls.clear()

    cog = FakeCog()
    cog.active_sessions[betting_session.key] = betting_session
    view = matchmaking.CancelConfirmView(betting_session)
    view.select._selected_values = ["confirm"]
    view.select._interaction = object()   # values property short-circuits to None until set
    interaction = FakeInteraction(
        gamehead(5), client=FakeClient(cog)
    )

    await view.on_select(interaction)

    _, rows = fake_db.perform_many_calls[0]
    assert rows == [(100, 7)]
    assert betting_session.betting_open is False
    assert betting_session.betting_close_task is None
    assert cog.active_sessions == {}


@pytest.mark.asyncio
async def test_backing_out_of_the_cancel_leaves_the_bets_alone(betting_session, fake_db, gamehead_roles):
    betting_session.bets = {7: {"team": "a", "points": 100}}
    view = matchmaking.CancelConfirmView(betting_session)
    view.select._selected_values = ["back"]
    view.select._interaction = object()   # values property short-circuits to None until set
    interaction = FakeInteraction(gamehead(5))

    await view.on_select(interaction)

    assert fake_db.perform_many_calls == []
    assert betting_session.bets == {7: {"team": "a", "points": 100}}
    assert isinstance(interaction.response.edits[0]["view"], matchmaking.AdminView)

# --- BetTeamSelectView.make_callback ---

@pytest.mark.asyncio
async def test_picking_a_team_opens_a_modal_seeded_with_the_live_balance(betting_session, fake_db):
    fake_db.fetch_one_result = (250,)
    view = matchmaking.BetTeamSelectView(betting_session, FakeMember(7))

    interaction = FakeInteraction(FakeMember(7))
    await view.make_callback("a")(interaction)

    modal = interaction.response.modals[0]
    assert isinstance(modal, matchmaking.BetModal)
    assert modal.team == "a"
    assert "250 available" in modal.children[0].placeholder


@pytest.mark.asyncio
async def test_picking_a_team_treats_a_missing_user_row_as_zero(betting_session, fake_db):
    fake_db.fetch_one_result = None
    view = matchmaking.BetTeamSelectView(betting_session, FakeMember(7))

    interaction = FakeInteraction(FakeMember(7))
    await view.make_callback("b")(interaction)

    assert "0 available" in interaction.response.modals[0].children[0].placeholder


@pytest.mark.asyncio
async def test_the_modal_label_stays_within_discords_limit(betting_session, fake_db):
    """Labels cap at 45 characters and both numbers are user-driven, so a large
    balance must not push the label over and break the modal before it opens."""
    fake_db.fetch_one_result = (999999999,)
    betting_session.bets = {7: {"team": "a", "points": 999999999}}
    view = matchmaking.BetTeamSelectView(betting_session, FakeMember(7))

    interaction = FakeInteraction(FakeMember(7))
    await view.make_callback("a")(interaction)

    assert len(interaction.response.modals[0].children[0].label) <= 45


# --- embed mentions ---

def test_lobby_roster_mentions_players_rather_than_naming_them(betting_session):
    betting_session.role_assignments = {}   # pre-shuffle waiting room

    embed = matchmaking.generate_embed(betting_session)

    assert "<@1>" in embed.fields[0].value
    assert "<@2>" in embed.fields[1].value


def test_shuffled_embed_mentions_players_and_carries_a_chatters_column(betting_session):
    embed = matchmaking.generate_embed(betting_session)   # dispatches to generate_match_embed

    assert [f.name for f in embed.fields] == ["Purple", "Gold", "Chatters"]
    assert "<@1>" in embed.fields[0].value
    assert embed.fields[2].value == "No bets yet"


# --- AdminView.shuffle opens betting ---

@pytest.mark.asyncio
async def test_shuffling_opens_the_betting_window(betting_session, fake_db, gamehead_roles, monkeypatch):
    """The entry point for the whole feature: no shuffle, no betting."""
    async def fake_shuffle_data(joined, game):
        return {}, {}

    async def no_unranked(game, joined, assignments):
        return []

    monkeypatch.setattr(matchmaking, "get_game_shuffle_data", fake_shuffle_data)
    monkeypatch.setattr(matchmaking, "get_unranked", no_unranked)
    monkeypatch.setattr(
        matchmaking, "balance_teams",
        lambda game, joined, elo, roles: (betting_session.team_a, betting_session.team_b, betting_session.role_assignments),
    )

    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(gamehead(5))

    await view.shuffle.callback(interaction)
    try:
        assert betting_session.betting_open is True
        assert betting_session.betting_close_task is not None
    finally:
        matchmaking.stop_betting_window(betting_session)


@pytest.mark.asyncio
async def test_reshuffling_refunds_before_reopening(betting_session, fake_db, gamehead_roles, monkeypatch):
    async def fake_shuffle_data(joined, game):
        return {}, {}

    async def no_unranked(game, joined, assignments):
        return []

    monkeypatch.setattr(matchmaking, "get_game_shuffle_data", fake_shuffle_data)
    monkeypatch.setattr(matchmaking, "get_unranked", no_unranked)
    monkeypatch.setattr(
        matchmaking, "balance_teams",
        lambda game, joined, elo, roles: (betting_session.team_a, betting_session.team_b, betting_session.role_assignments),
    )
    betting_session.bets = {7: {"team": "a", "points": 100}}

    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(gamehead(5))

    await view.shuffle.callback(interaction)
    try:
        _, rows = fake_db.perform_many_calls[0]
        assert rows == [(100, 7)]
        assert betting_session.bets == {}
    finally:
        matchmaking.stop_betting_window(betting_session)


@pytest.mark.asyncio
async def test_shuffling_defers_before_doing_any_work(betting_session, fake_db, gamehead_roles, monkeypatch):
    """The shuffle query, the refund and two message edits stack up past Discord's 3
    second deadline, which kills the interaction even though the state already changed."""
    deferred_at = []

    async def fake_shuffle_data(joined, game):
        deferred_at.append(interaction.response.deferred)
        return {}, {}

    async def no_unranked(game, joined, assignments):
        return []

    monkeypatch.setattr(matchmaking, "get_game_shuffle_data", fake_shuffle_data)
    monkeypatch.setattr(matchmaking, "get_unranked", no_unranked)
    monkeypatch.setattr(
        matchmaking, "balance_teams",
        lambda game, joined, elo, roles: (betting_session.team_a, betting_session.team_b, betting_session.role_assignments),
    )

    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(gamehead(5))

    await view.shuffle.callback(interaction)
    try:
        assert deferred_at == [True]   # deferred before even the first query
        assert isinstance(interaction.original_response_edits[0]["view"], matchmaking.AdminView)
        assert interaction.response.edits == []   # edit_message is unavailable post-defer
    finally:
        matchmaking.stop_betting_window(betting_session)


@pytest.mark.asyncio
async def test_shuffling_answers_a_non_game_head_without_deferring(betting_session, fake_db, gamehead_roles):
    """The refusal is the whole response, so it has to stay a plain reply."""
    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(member(99))

    await view.shuffle.callback(interaction)

    assert "not a game head" in interaction.response.messages[0]["content"]
    assert interaction.response.deferred is False
    assert betting_session.betting_open is False


@pytest.mark.asyncio
async def test_swapping_defers_before_restarting_the_window(betting_session, fake_db, gamehead_roles):
    """start_betting_window can await a close-timer that's mid-edit, then refund -- two
    round trips before the panel would otherwise get its reply."""
    view = matchmaking.SwapSelectView(betting_session)
    view.select._selected_values = ["1", "3"]
    view.select._interaction = object()
    interaction = FakeInteraction(gamehead(5))

    try:
        await view.on_select(interaction)

        assert interaction.response.deferred is True
        assert isinstance(interaction.original_response_edits[0]["view"], matchmaking.AdminView)
        assert interaction.response.edits == []
    finally:
        matchmaking.stop_betting_window(betting_session)


# --- races closed by review ---

@pytest.mark.asyncio
async def test_a_bet_landing_after_settlement_is_rolled_back(betting_session, monkeypatch):
    """declare_winner can settle and clear the book while the deduction is in flight.
    Booking the bet anyway would take the points with nothing left to pay them from."""
    betting_session.betting_open = True
    calls = []

    async def perform_one(sql, parameters=None):
        calls.append((sql, parameters))
        betting_session.betting_open = False   # a winner gets declared mid-UPDATE
        return 1

    monkeypatch.setattr(matchmaking.db, "perform_one", perform_one)
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="100")
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert betting_session.bets == {}
    assert "closed while you were typing" in interaction.response.messages[0]["content"]
    assert calls[1][1] == (100, 7)   # stake handed straight back


@pytest.mark.asyncio
async def test_a_superseded_close_timer_leaves_the_new_window_alone(betting_session, instant_window):
    """A re-shuffle in the same tick as the deadline can't cancel the old timer in
    time, so it has to notice it is no longer the live one."""
    betting_session.betting_open = True
    betting_session.betting_close_task = None   # some other task owns the window now

    await matchmaking.close_betting_after_delay(betting_session)

    assert betting_session.betting_open is True
    assert betting_session.message.edit_calls == []


@pytest.mark.asyncio
async def test_reshuffling_waits_for_the_old_timer_to_die(betting_session, fake_db, instant_window):
    await matchmaking.start_betting_window(betting_session)
    first_task = betting_session.betting_close_task

    await matchmaking.start_betting_window(betting_session)
    try:
        assert first_task.done()
        assert betting_session.betting_open is True
    finally:
        matchmaking.stop_betting_window(betting_session)


# --- lineup changes refund outstanding bets ---

@pytest.mark.asyncio
async def test_joining_refunds_bets_placed_on_the_old_lineup(betting_session, fake_db):
    """Join wipes the teams, so every stake was placed on a match that no longer exists."""
    await matchmaking.start_betting_window(betting_session)
    betting_session.bets = {7: {"team": "a", "points": 100}}
    fake_db.perform_many_calls.clear()

    view = matchmaking.LobbyView(betting_session)
    await view.join.callback(FakeInteraction(FakeUser(id=99)))

    _, rows = fake_db.perform_many_calls[0]
    assert rows == [(100, 7)]
    assert betting_session.bets == {}
    assert betting_session.betting_open is False
    assert betting_session.betting_close_task is None


@pytest.mark.asyncio
async def test_leaving_refunds_bets_placed_on_the_old_lineup(betting_session, fake_db):
    await matchmaking.start_betting_window(betting_session)
    betting_session.bets = {7: {"team": "a", "points": 100}}
    fake_db.perform_many_calls.clear()

    view = matchmaking.LobbyView(betting_session)
    await view.leave.callback(FakeInteraction(betting_session.joined[0]))

    _, rows = fake_db.perform_many_calls[0]
    assert rows == [(100, 7)]
    assert betting_session.bets == {}
    assert betting_session.betting_open is False


@pytest.mark.asyncio
async def test_joining_sends_back_a_view_that_reflects_the_new_state(betting_session, fake_db, monkeypatch):
    """Both button states are frozen in __init__, so re-sending the same instance left an
    enabled Bet button on a lobby whose betting had just closed."""
    monkeypatch.setitem(matchmaking.LOBBY_SIZE, "fakegame", 5)
    await matchmaking.start_betting_window(betting_session)

    view = matchmaking.LobbyView(betting_session)
    assert view.bet.disabled is False   # open before the join
    interaction = FakeInteraction(FakeUser(id=99))

    await view.join.callback(interaction)

    sent_back = interaction.response.edits[0]["view"]
    assert sent_back is not view
    assert sent_back.bet.disabled is True    # betting closed with the teams
    assert sent_back.join.disabled is True   # and that fifth player filled the lobby


@pytest.mark.asyncio
async def test_swapping_refunds_bets_placed_on_the_old_lineup(betting_session, fake_db, gamehead_roles):
    await matchmaking.start_betting_window(betting_session)
    betting_session.bets = {7: {"team": "a", "points": 100}}
    fake_db.perform_many_calls.clear()
    first_task = betting_session.betting_close_task

    view = matchmaking.SwapSelectView(betting_session)
    view.select._selected_values = ["1", "3"]
    view.select._interaction = object()
    try:
        await view.on_select(FakeInteraction(gamehead(5)))

        _, rows = fake_db.perform_many_calls[0]
        assert rows == [(100, 7)]
        assert betting_session.bets == {}
        # Swapping restarts the window, so bettors get a full one on the new lineup
        assert betting_session.betting_open is True
        assert first_task.done()
        assert betting_session.betting_close_task is not first_task
    finally:
        matchmaking.stop_betting_window(betting_session)


# --- select callbacks gate the same way their Back buttons do ---

@pytest.mark.asyncio
async def test_swapping_is_gated_to_game_heads(betting_session, fake_db, gamehead_roles):
    view = matchmaking.SwapSelectView(betting_session)
    view.select._selected_values = ["1", "3"]
    view.select._interaction = object()
    interaction = FakeInteraction(member(99))

    await view.on_select(interaction)

    assert "not a game head" in interaction.response.messages[0]["content"]
    assert betting_session.team_a == [betting_session.joined[0], betting_session.joined[1]]
    assert fake_db.perform_many_calls == []


@pytest.mark.asyncio
async def test_picking_a_map_is_gated_to_game_heads(betting_session, gamehead_roles, monkeypatch):
    monkeypatch.setitem(matchmaking.MAPS, "fakegame", ["Ascent", "Bind"])
    view = matchmaking.MapSelectView(betting_session)
    interaction = FakeInteraction(member(99))

    await view.on_select(interaction)

    assert "not a game head" in interaction.response.messages[0]["content"]
    assert betting_session.map is None


# --- lineup changes void bets still in flight ---

@pytest.mark.asyncio
async def test_a_bet_is_rolled_back_when_a_reshuffle_lands_mid_deduction(betting_session, monkeypatch):
    """A re-shuffle refunds the book and reopens betting, so betting_open is True again
    by the time the modal resumes. Only the epoch tells the two apart."""
    betting_session.betting_open = True
    calls = []

    async def perform_one(sql, parameters=None):
        calls.append((sql, parameters))
        betting_session.betting_epoch += 1   # a re-shuffle lands mid-UPDATE
        return 1

    async def perform_many(sql, parameters):
        pass

    monkeypatch.setattr(matchmaking.db, "perform_one", perform_one)
    monkeypatch.setattr(matchmaking.db, "perform_many", perform_many)
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="100")
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert betting_session.bets == {}
    assert "teams changed" in interaction.response.messages[0]["content"]
    assert calls[1][1] == (100, 7)   # stake handed straight back


@pytest.mark.asyncio
async def test_a_raise_in_flight_during_a_refund_cannot_outrun_its_stake(betting_session, monkeypatch):
    """Raising deducts only the delta. If the book is refunded mid-UPDATE, booking the
    new total would leave the user staked 250 having paid 150."""
    betting_session.betting_open = True
    betting_session.bets = {7: {"team": "a", "points": 100}}
    calls = []

    async def perform_one(sql, parameters=None):
        calls.append((sql, parameters))
        if len(calls) == 1:
            betting_session.bets = {}            # the refund clears the book
            betting_session.betting_epoch += 1
        return 1

    async def perform_many(sql, parameters):
        pass

    monkeypatch.setattr(matchmaking.db, "perform_one", perform_one)
    monkeypatch.setattr(matchmaking.db, "perform_many", perform_many)
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, value="250", current_bet=100)
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert betting_session.bets == {}
    assert calls[0][1] == (150, 7, 150)   # only the delta was ever deducted
    assert calls[1][1] == (150, 7)        # and only the delta comes back


@pytest.mark.asyncio
async def test_refund_bets_bumps_the_epoch_even_with_an_empty_book(betting_session, fake_db):
    """A first-time bet mid-submission isn't in the book yet, so an empty-book early
    return would let it through onto the new lineup."""
    epoch_before = betting_session.betting_epoch

    await matchmaking.refund_bets(betting_session)

    assert betting_session.betting_epoch == epoch_before + 1


@pytest.mark.asyncio
async def test_refund_bets_empties_the_book_before_it_credits(betting_session, monkeypatch):
    """Clearing after the credit would discard anything booked while it was in flight --
    on the reshuffle path betting_open is still True, so a bet really can land there."""
    betting_session.bets = {7: {"team": "a", "points": 100}}
    seen = []

    async def perform_many(sql, parameters):
        seen.append(dict(betting_session.bets))
        betting_session.bets[9] = {"team": "b", "points": 50}   # a bet lands mid-credit

    monkeypatch.setattr(matchmaking.db, "perform_many", perform_many)

    await matchmaking.refund_bets(betting_session)

    assert seen == [{}]                                          # already cleared
    assert betting_session.bets == {9: {"team": "b", "points": 50}}   # and it survived


@pytest.mark.asyncio
async def test_settle_bets_empties_the_book_before_it_credits(betting_session, monkeypatch):
    betting_session.bets = {7: {"team": "a", "points": 100}, 8: {"team": "b", "points": 100}}
    seen = []

    async def perform_many(sql, parameters):
        seen.append(dict(betting_session.bets))

    monkeypatch.setattr(matchmaking.db, "perform_many", perform_many)

    summary = await matchmaking.settle_bets(betting_session, team_a_won=True)

    assert seen == [{}]
    assert summary["richest_bettor_payout"] == 200   # the locals still drove the payout


# --- team rules survive a stale ephemeral picker ---

def test_a_spectator_can_back_either_team(betting_session):
    assert matchmaking.bet_rejection_reason(betting_session, 99, "a") is None
    assert matchmaking.bet_rejection_reason(betting_session, 99, "b") is None


def test_a_player_cannot_back_the_team_theyre_playing_against(betting_session):
    """FakeMember(1) is on team_a in the fixture."""
    assert matchmaking.bet_rejection_reason(betting_session, 1, "a") is None
    assert "your own team" in matchmaking.bet_rejection_reason(betting_session, 1, "b")


@pytest.mark.asyncio
async def test_a_second_picker_cannot_switch_a_bettors_side(betting_session, fake_db):
    """Two Bet views opened before betting both render with the sides enabled. Backing one
    team then the other used to overwrite the first bet's team and charge only the delta."""
    betting_session.betting_open = True
    betting_session.bets = {7: {"team": "a", "points": 100}}
    user = FakeMember(7)
    modal = make_bet_modal(betting_session, user, team="b", value="200")
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert "switch sides" in interaction.response.messages[0]["content"]
    assert betting_session.bets == {7: {"team": "a", "points": 100}}
    assert fake_db.perform_one_calls == []   # rejected before anything was deducted


@pytest.mark.asyncio
async def test_a_swapped_player_cannot_bet_against_their_new_team(betting_session, fake_db):
    """A swap restarts the window but can't reach back into an already-open ephemeral
    picker, so the modal is the only place left to catch this."""
    betting_session.betting_open = True
    user = betting_session.team_a[0]
    matchmaking.swap_slots(betting_session, user.id, betting_session.team_b[0].id)
    modal = make_bet_modal(betting_session, user, team="a", value="100")
    interaction = FakeInteraction(user)

    await modal.callback(interaction)

    assert "your own team" in interaction.response.messages[0]["content"]
    assert betting_session.bets == {}
    assert fake_db.perform_one_calls == []


@pytest.mark.asyncio
async def test_the_picker_greys_out_the_buttons_the_modal_would_reject(betting_session):
    """Same rule driving both, so the disable and the enforcement can't drift apart."""
    betting_session.bets = {7: {"team": "a", "points": 100}}
    view = matchmaking.BetTeamSelectView(betting_session, FakeMember(7))

    assert view.children[0].disabled is False   # their own side stays open to raises
    assert view.children[1].disabled is True


def test_chatters_field_still_shows_a_row_in_a_one_versus_one_lobby(betting_session, monkeypatch):
    """team_size of 1 used to slice rows[:0] and print nothing but the overflow line."""
    monkeypatch.setitem(matchmaking.LOBBY_SIZE, "fakegame", 2)
    betting_session.bets = {
        7: {"team": "a", "points": 500},
        8: {"team": "b", "points": 10},
    }

    rows = matchmaking.generate_chatters_field(betting_session).split("\n")

    assert rows[0] == "<@7> - 500 points"   # the top stake survives
    assert rows[1] == "...and 1 more"


# --- edit_lobby_message ---

@pytest.mark.asyncio
async def test_editing_the_lobby_message_reports_a_lobby_that_never_got_one(betting_session):
    """session.message stays None until the post-send fetch lands, and every caller
    used to have to remember that."""
    betting_session.message = None

    assert await matchmaking.refresh_lobby_message(betting_session) is False


@pytest.mark.asyncio
async def test_editing_the_lobby_message_swallows_a_deleted_message(betting_session):
    import discord

    async def boom(**kwargs):
        raise discord.NotFound(_FakeResponse(), "gone")

    betting_session.message.edit = boom

    assert await matchmaking.refresh_lobby_message(betting_session) is False


@pytest.mark.asyncio
async def test_swapping_survives_a_lobby_that_never_got_its_message(betting_session, fake_db, gamehead_roles):
    """The swap path reached straight through session.message, so a lobby whose
    post-send fetch never landed raised instead of swapping."""
    betting_session.message = None
    view = matchmaking.SwapSelectView(betting_session)
    view.select._selected_values = ["1", "3"]
    view.select._interaction = object()

    await view.on_select(FakeInteraction(gamehead(5)))

    assert betting_session.team_a[-1].id == 3


@pytest.mark.asyncio
async def test_cancelling_survives_a_lobby_that_never_got_its_message(betting_session, fake_db, gamehead_roles):
    betting_session.message = None
    cog = FakeCog()
    cog.active_sessions[betting_session.key] = betting_session
    view = matchmaking.CancelConfirmView(betting_session)
    view.select._selected_values = ["confirm"]
    view.select._interaction = object()

    await view.on_select(FakeInteraction(gamehead(5), client=FakeClient(cog)))

    assert cog.active_sessions == {}


@pytest.mark.asyncio
async def test_cancelling_defers_before_the_refund_and_the_edit(betting_session, gamehead_roles, monkeypatch):
    """A DB round trip plus an API call don't fit inside Discord's 3 second deadline."""
    betting_session.bets = {7: {"team": "a", "points": 100}}
    deferred_at_refund = []

    async def perform_many(sql, parameters):
        deferred_at_refund.append(interaction.response.deferred)

    monkeypatch.setattr(matchmaking.db, "perform_many", perform_many)

    cog = FakeCog()
    cog.active_sessions[betting_session.key] = betting_session
    view = matchmaking.CancelConfirmView(betting_session)
    view.select._selected_values = ["confirm"]
    view.select._interaction = object()
    interaction = FakeInteraction(gamehead(5), client=FakeClient(cog))

    await view.on_select(interaction)

    assert deferred_at_refund == [True]


# --- ended lobbies ---

@pytest.mark.asyncio
async def test_declare_winner_refuses_a_second_declaration(
    betting_session, fake_db, gamehead_roles, no_record_keeping, declaring
):
    """Two game heads can hold open pickers at once. A second declare would re-record
    the result and re-apply the elo on a match that's already settled."""
    await matchmaking.declare_winner(betting_session, declaring(gamehead(5)), team_a_won=True)
    second = declaring(gamehead(6))

    await matchmaking.declare_winner(betting_session, second, team_a_won=False)

    assert "already over" in second.response.messages[0]["content"]
    assert second.response.deferred is False


@pytest.mark.asyncio
async def test_declare_winner_takes_the_admin_panels_down_with_it(
    betting_session, fake_db, gamehead_roles, no_record_keeping, declaring
):
    panel = FakeMessage()
    betting_session.admin_panels = {6: panel}

    await matchmaking.declare_winner(betting_session, declaring(gamehead(5)), team_a_won=True)

    assert panel.deleted is True
    assert betting_session.admin_panels == {}


@pytest.mark.asyncio
async def test_cancelling_takes_the_admin_panels_down_with_it(betting_session, fake_db, gamehead_roles):
    panel = FakeMessage()
    betting_session.admin_panels = {6: panel}
    cog = FakeCog()
    cog.active_sessions[betting_session.key] = betting_session
    view = matchmaking.CancelConfirmView(betting_session)
    view.select._selected_values = ["confirm"]
    view.select._interaction = object()

    await view.on_select(FakeInteraction(gamehead(5), client=FakeClient(cog)))

    assert betting_session.ended is True
    assert panel.deleted is True


@pytest.mark.asyncio
async def test_admin_panel_buttons_go_dead_once_the_lobby_ends(betting_session, gamehead_roles):
    """A click already in flight lands after close_admin_panels has run, and Shuffle
    would reopen betting on a match nobody can win."""
    betting_session.ended = True
    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(gamehead(5))

    allowed = await view.interaction_check(interaction)

    assert allowed is False
    assert "already over" in interaction.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_admin_panel_buttons_work_while_the_lobby_is_live(betting_session, gamehead_roles):
    view = matchmaking.AdminView(betting_session)

    assert await view.interaction_check(FakeInteraction(gamehead(5))) is True


@pytest.mark.asyncio
async def test_shuffle_checks_privilege_before_it_reports_the_lobby_state(betting_session, gamehead_roles):
    """Every other handler rejects outsiders first; shuffle was telling them how many
    players were in the lobby on the way past."""
    betting_session.joined = []
    view = matchmaking.AdminView(betting_session)
    interaction = FakeInteraction(member(99))

    await view.shuffle.callback(interaction)

    assert "not a game head" in interaction.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_the_bet_picker_expires_with_the_window_it_belongs_to(betting_session):
    """A bare 120 shadowing BETTING_WINDOW_SECONDS would drift the moment the window
    length changed."""
    view = matchmaking.BetTeamSelectView(betting_session, FakeMember(9))

    assert view.timeout == matchmaking.BETTING_WINDOW_SECONDS
