import pytest
from cogs import matchmaking

class FakeMember:
    def __init__(self, id):
        self.id = id

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
    def __init__(self, name):
        self.name = name


class FakeUser:
    def __init__(self, roles=None, administrator=False):
        self.roles = roles or []
        self.guild_permissions = FakeGuildPermissions(administrator)


class FakeInteraction:
    def __init__(self, user):
        self.user = user


def test_has_privilege_true_for_admin():
    interaction = FakeInteraction(FakeUser(administrator=True))
    assert matchmaking.has_privilege(interaction) is True


def test_has_privilege_true_for_game_head_role():
    interaction = FakeInteraction(FakeUser(roles=[FakeRole("Valorant Game Head")]))
    assert matchmaking.has_privilege(interaction) is True


def test_has_privilege_false_otherwise():
    interaction = FakeInteraction(FakeUser(roles=[FakeRole("Member")]))
    assert matchmaking.has_privilege(interaction) is False