from types import SimpleNamespace

import pytest
import yaml
from conftest import FakeApplicationContext as BaseContext

from cogs import config_editor
from utils import config, config_edit, config_schema

SAMPLE = """---
roles:
  # pinged when something needs a human
  staff_role: 1
  bot_devs:
    users:
      - 10 # alex
    role: 2
  gameroom_staff:
    users:
      - 10
    role: 3
  gameheads:
    valorant: 4 # val
gameheads:
  smash:
    # N/A....
  valorant:
    someone: "someone@u.northwestern.edu"
antiscam:
  # 28 is Discord's maximum
  timeout_days: 28
  exempt_staff: true
"""


class FakeMember:
    def __init__(self, id=10, roles=(), administrator=False, name="lilac"):
        self.id = id
        self.name = name
        self.roles = [SimpleNamespace(id=role_id) for role_id in roles]
        self.guild_permissions = SimpleNamespace(administrator=administrator)


@pytest.fixture
def live_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(SAMPLE, encoding="utf-8", newline="\n")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "config", yaml.safe_load(SAMPLE))
    return path


def text(path):
    return path.read_text(encoding="utf-8")


# --- the schema ---------------------------------------------------------------


# free-form or testing-only, so deliberately out of /config's reach
UNREGISTERED = ("fun.special_users", "antiscam.test_account_ages")


def test_every_example_setting_is_registered():
    with open("config.example.yaml", encoding="utf-8") as f:
        example = yaml.safe_load(f)

    missing = []

    def walk(node, prefix):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if config_schema.find(path) or path in UNREGISTERED:
                continue
            if isinstance(value, dict):
                walk(value, path)
            else:
                missing.append(path)

    walk(example, "")
    assert missing == []


def test_secrets_are_unreachable():
    assert config_schema.find("discord.token") is None
    assert config_schema.find("database.password") is None


def test_a_wildcard_only_takes_plain_keys():
    assert (
        config_schema.find("gameheads.attendants").kind == config_schema.Kind.EMAIL_MAP
    )
    assert config_schema.find("gameheads.Bad Key") is None


def test_problems_catch_bad_values_but_allow_unset():
    data = yaml.safe_load(SAMPLE)
    assert config_schema.problems(data) == []

    data["antiscam"]["timeout_days"] = 99
    data["roles"]["bot_devs"]["users"] = ["alex"]
    data["gameheads"]["valorant"]["someone"] = "not an email"
    assert config_schema.problems(data) == [
        "roles.bot_devs.users must be a list of user ids",
        "gameheads.valorant someone needs an email",
        "antiscam.timeout_days must be at most 28",
    ]


def test_a_flag_is_not_an_id():
    setting = config_schema.find("reservations.channel")
    assert config_schema.check(setting, True) == "must be an id"


# --- saving an edit -----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_edit_changes_only_its_own_line(live_config):
    await config_edit.edit(FakeMember(), "antiscam.timeout_days", lambda _: 7)
    assert text(live_config) == SAMPLE.replace("timeout_days: 28", "timeout_days: 7")


@pytest.mark.asyncio
async def test_an_edit_goes_live_in_the_same_dict(live_config):
    live = config.config
    saved = await config_edit.edit(FakeMember(), "roles.staff_role", lambda _: 55)

    assert (saved.old, saved.new) == (1, 55)
    assert live is config.config
    assert live["roles"]["staff_role"] == 55


@pytest.mark.asyncio
async def test_the_previous_file_is_kept(live_config):
    await config_edit.edit(FakeMember(), "roles.staff_role", lambda _: 55)
    assert text(live_config.with_name("config.yaml.prev")) == SAMPLE


@pytest.mark.asyncio
async def test_an_out_of_range_value_is_refused_and_nothing_written(live_config):
    with pytest.raises(config_edit.EditRefused, match="at most 28"):
        await config_edit.edit(FakeMember(), "antiscam.timeout_days", lambda _: 99)
    assert text(live_config) == SAMPLE
    assert config.config["antiscam"]["timeout_days"] == 28


@pytest.mark.asyncio
async def test_an_unregistered_path_is_refused(live_config):
    with pytest.raises(config_edit.EditRefused):
        await config_edit.edit(FakeMember(), "discord.token", lambda _: "x")


@pytest.mark.asyncio
async def test_you_cant_remove_your_own_access(live_config):
    with pytest.raises(config_edit.EditRefused, match="your own access"):
        await config_edit.edit(FakeMember(), "roles.bot_devs.users", lambda _: [])
    assert text(live_config) == SAMPLE


@pytest.mark.asyncio
async def test_an_admin_can_clear_the_list(live_config):
    admin = FakeMember(id=99, administrator=True)
    await config_edit.edit(admin, "roles.bot_devs.users", lambda _: [])
    assert config.config["roles"]["bot_devs"]["users"] == []


@pytest.mark.asyncio
async def test_a_new_roster_can_be_started(live_config):
    await config_edit.edit(
        FakeMember(),
        "gameheads.attendants",
        lambda _: {"liilac__": "alex@u.northwestern.edu"},
    )
    assert config.gamehead_email("liilac__") == "alex@u.northwestern.edu"
    assert "# N/A...." in text(live_config)


@pytest.mark.asyncio
async def test_an_existing_bad_value_doesnt_block_other_fixes(live_config):
    live_config.write_text(
        SAMPLE.replace("timeout_days: 28", "timeout_days: 99"), encoding="utf-8"
    )
    await config_edit.edit(FakeMember(), "roles.staff_role", lambda _: 55)
    assert config.config["roles"]["staff_role"] == 55


@pytest.mark.asyncio
async def test_reload_picks_up_hand_edits(live_config):
    live_config.write_text(SAMPLE.replace("staff_role: 1", "staff_role: 7"))
    assert await config_edit.reload() == []
    assert config.config["roles"]["staff_role"] == 7


@pytest.mark.asyncio
async def test_reload_keeps_the_running_config_when_the_file_is_broken(live_config):
    live_config.write_text("roles: [unclosed")
    with pytest.raises(config_edit.EditRefused):
        await config_edit.reload()
    assert config.config["roles"]["staff_role"] == 1


# --- the commands -------------------------------------------------------------


class FakeBot:
    def __init__(self):
        self.dispatched = []

    def dispatch(self, event, *args):
        self.dispatched.append(event)


class FakeApplicationContext(BaseContext):
    """Keeps the reply text too, which the shared fake drops."""

    def __init__(self, author):
        super().__init__(author)
        self.replies = []

    async def respond(self, content=None, **kwargs):
        self.replies.append(content)


@pytest.mark.asyncio
async def test_only_bot_devs_get_in(live_config):
    ctx = FakeApplicationContext(FakeMember(id=5))
    cog = config_editor.ConfigEditor(FakeBot())

    await config_editor.ConfigEditor.get.callback(cog, ctx, "roles.staff_role")

    assert "Only bot devs" in ctx.replies[-1]


@pytest.mark.asyncio
async def test_set_number_saves_and_tells_the_cogs(live_config):
    bot = FakeBot()
    cog = config_editor.ConfigEditor(bot)
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.edit_number.callback(
        cog, ctx, "antiscam.timeout_days", "14"
    )

    assert config.config["antiscam"]["timeout_days"] == 14
    assert bot.dispatched == ["config_changed"]
    assert ctx.replies[-1] == (
        "✅ Saved **antiscam.timeout_days**\n```ansi\n"
        "antiscam:\n"
        "  # 28 is Discord's maximum\n"
        f"{config_edit.BOLD}  timeout_days: 14{config_edit.RESET}\n"
        "  exempt_staff: true\n```"
    )


@pytest.mark.asyncio
async def test_a_path_of_the_wrong_kind_is_refused(live_config):
    bot = FakeBot()
    cog = config_editor.ConfigEditor(bot)
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.edit_number.callback(
        cog, ctx, "roles.staff_role", "14"
    )

    assert config.config["roles"]["staff_role"] == 1
    assert bot.dispatched == []


@pytest.mark.asyncio
async def test_removing_someone_not_on_the_roster_is_refused(live_config):
    bot = FakeBot()
    cog = config_editor.ConfigEditor(bot)
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.remove_email.callback(
        cog, ctx, "gameheads.valorant", "nobody"
    )

    assert text(live_config) == SAMPLE
    assert bot.dispatched == []
    assert "nobody isn't on" in ctx.replies[-1]


def test_a_removal_shows_the_line_that_went():
    old = "a:\n  b: 1\n  c: 2\n  d: 3\n"
    new = "a:\n  b: 1\n  d: 3\n"
    red, reset = config_edit.RED, config_edit.RESET
    assert config_edit.snippet(old, new) == (
        f"```ansi\na:\n  b: 1\n{red}  c: 2{reset}\n  d: 3\n```"
    )


@pytest.mark.parametrize(
    "typed",
    ["14", " 14 ", '"14"', "'14'", "“14”", "‘14’", '" 14 "'],
)
def test_quotes_are_never_needed(typed):
    assert config_editor.unquote(typed) == "14"


def test_quotes_inside_a_value_are_kept():
    assert config_editor.unquote('o"brien') == 'o"brien'


@pytest.mark.asyncio
async def test_a_quoted_number_still_saves(live_config):
    cog = config_editor.ConfigEditor(FakeBot())
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.edit_number.callback(
        cog, ctx, "antiscam.timeout_days", "“14”"
    )

    assert config.config["antiscam"]["timeout_days"] == 14


@pytest.mark.asyncio
async def test_a_new_email_is_written_like_the_others(live_config):
    cog = config_editor.ConfigEditor(FakeBot())
    ctx = FakeApplicationContext(FakeMember())
    user = FakeMember(id=20, name="banllana")

    await config_editor.ConfigEditor.add_email.callback(
        cog, ctx, "gameheads.valorant", user, '"hannah@u.northwestern.edu"'
    )

    assert config.gamehead_email("banllana") == "hannah@u.northwestern.edu"
    assert '    banllana: "hannah@u.northwestern.edu"\n' in text(live_config)


@pytest.mark.asyncio
async def test_add_refuses_someone_already_on_the_roster(live_config):
    cog = config_editor.ConfigEditor(FakeBot())
    ctx = FakeApplicationContext(FakeMember())
    user = FakeMember(id=20, name="someone")

    await config_editor.ConfigEditor.add_email.callback(
        cog, ctx, "gameheads.valorant", user, "new@u.northwestern.edu"
    )

    assert "use /config edit email" in ctx.replies[-1]
    assert text(live_config) == SAMPLE


@pytest.mark.asyncio
async def test_edit_changes_an_existing_email(live_config):
    cog = config_editor.ConfigEditor(FakeBot())
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.edit_email.callback(
        cog, ctx, "gameheads.valorant", "someone", "new@u.northwestern.edu"
    )

    assert config.gamehead_email("someone") == "new@u.northwestern.edu"
    assert '    someone: "new@u.northwestern.edu"\n' in text(live_config)


@pytest.mark.asyncio
async def test_edit_refuses_someone_not_on_the_roster(live_config):
    cog = config_editor.ConfigEditor(FakeBot())
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.edit_email.callback(
        cog, ctx, "gameheads.valorant", "nobody", "new@u.northwestern.edu"
    )

    assert "use /config add email" in ctx.replies[-1]


def test_show_renders_mentions():
    role = config_schema.find("roles.staff_role")
    roster = config_schema.find("gameheads.valorant")
    assert config_editor.show(role, 5) == "<@&5>"
    assert config_editor.show(role, None) == "*unset*"
    assert config_editor.show(roster, {"a": "a@b.c"}) == "`a`: a@b.c"
