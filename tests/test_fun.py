import pytest

from cogs import fun


class FakeSticker:
    def __init__(self, id):
        self.id = id


class FakeGuild:
    def __init__(self, stickers=None):
        self.stickers = stickers or []


class FakeMessage:
    def __init__(self, content="", mention_everyone=False, guild=None):
        self.content = content
        self.mention_everyone = mention_everyone
        self.guild = guild
        self.reply_calls = []

    async def reply(self, *args, **kwargs):
        self.reply_calls.append((args, kwargs))


class FakeClientUser:
    def __init__(self, mentioned=False):
        self._mentioned = mentioned

    def mentioned_in(self, message):
        return self._mentioned


class FakeBot:
    def __init__(self, user):
        self.user = user


class FakeCog:
    def __init__(self, mentioned=False):
        self.bot = FakeBot(FakeClientUser(mentioned))


@pytest.fixture
def fun_config(monkeypatch):
    """Fake chess_emojis/special_users config, no randomness by default (tests
    override fun.random.randint/choice per case as needed)."""
    fake = {
        "fun": {
            "chess_emojis": {"pawn": 111, "rook": 222},
            "special_users": {42: ["hi 42"], 99: ["hi 99a", "hi 99b"]},
        }
    }
    monkeypatch.setattr(fun.config, "config", fake)


# --- i_love_osu ---

def test_i_love_osu_matches_case_insensitive():
    assert fun.i_love_osu(FakeMessage(content="I LOVE OSU today")) == "Osu 😻"


def test_i_love_osu_no_match_returns_none():
    assert fun.i_love_osu(FakeMessage(content="i love valorant")) is None


# --- oh_lord ---

def test_oh_lord_hits_when_roll_succeeds_and_phrase_present(monkeypatch):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 1)
    result = fun.oh_lord(FakeMessage(content="oh lord here we go"))
    assert result == "https://www.youtube.com/watch?v=YsoP6bjADic"


def test_oh_lord_misses_when_phrase_absent(monkeypatch):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 1)
    assert fun.oh_lord(FakeMessage(content="oh no")) is None


def test_oh_lord_misses_when_roll_fails(monkeypatch):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 50)
    assert fun.oh_lord(FakeMessage(content="oh lord")) is None


# --- special_interactions ---

def test_special_interactions_hits_for_known_user(monkeypatch, fun_config):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 1)
    monkeypatch.setattr(fun.random, "choice", lambda seq: seq[0])
    message = FakeMessage()
    message.author = type("A", (), {"id": 42})()
    assert fun.special_interactions(message) == ["hi 42"]


def test_special_interactions_none_for_unknown_user(monkeypatch, fun_config):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 1)
    message = FakeMessage()
    message.author = type("A", (), {"id": 7})()
    assert fun.special_interactions(message) is None


def test_special_interactions_none_when_roll_fails(monkeypatch, fun_config):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 50)
    message = FakeMessage()
    message.author = type("A", (), {"id": 42})()
    assert fun.special_interactions(message) is None


def test_special_interactions_none_when_special_users_empty(monkeypatch):
    monkeypatch.setattr(fun.config, "config", {"fun": {"special_users": {}}})
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 1)
    message = FakeMessage()
    message.author = type("A", (), {"id": 42})()
    assert fun.special_interactions(message) is None


# --- chess ---

def test_chess_reacts_when_mentioned(monkeypatch, fun_config):
    monkeypatch.setattr(fun.random, "choice", lambda seq: seq[0])
    cog = FakeCog(mentioned=True)
    assert fun.chess(cog, FakeMessage()) == "<:pawn:111>"


def test_chess_none_when_mention_everyone(fun_config):
    cog = FakeCog(mentioned=True)
    assert fun.chess(cog, FakeMessage(mention_everyone=True)) is None


def test_chess_none_when_not_mentioned(fun_config):
    cog = FakeCog(mentioned=False)
    assert fun.chess(cog, FakeMessage()) is None


# --- ty_stan ---

@pytest.mark.asyncio
async def test_ty_stan_replies_with_sticker_and_returns_none(monkeypatch):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 1)
    sticker = FakeSticker(fun.TYST_STICKER_ID)
    message = FakeMessage(content="thank you shannon tan", guild=FakeGuild([sticker]))

    result = await fun.ty_stan(message)

    assert result is None
    assert len(message.reply_calls) == 1
    args, kwargs = message.reply_calls[0]
    assert kwargs["stickers"] == [sticker]


@pytest.mark.asyncio
async def test_ty_stan_returns_string_when_no_sticker_found(monkeypatch):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 1)
    message = FakeMessage(content="tyst", guild=FakeGuild([]))

    result = await fun.ty_stan(message)

    assert result == "THANK YOU SHANNON TAN"
    assert message.reply_calls == []


@pytest.mark.asyncio
async def test_ty_stan_false_when_phrase_absent(monkeypatch):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 1)
    message = FakeMessage(content="hello", guild=FakeGuild([]))
    assert await fun.ty_stan(message) is False


@pytest.mark.asyncio
async def test_ty_stan_false_when_roll_fails(monkeypatch):
    monkeypatch.setattr(fun.random, "randint", lambda a, b: 50)
    message = FakeMessage(content="tyst", guild=FakeGuild([]))
    assert await fun.ty_stan(message) is False
