import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import yaml
from conftest import FakeApplicationContext as BaseContext
from conftest import FakeInteraction, FakeMessage

from cogs import config_editor
from utils import config, config_edit, config_history, config_schema

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


class FakeHistory:
    """utils.config_history kept in a list, so the command tests need no database."""

    def __init__(self):
        self.revisions = []

    async def record(
        self, action, after_text, before_text=None, path=None, member=None
    ):
        revision = config_history.Revision(
            len(self.revisions) + 1,
            datetime.now(UTC),
            action,
            path,
            member.name if member else None,
            before_text,
            after_text,
            None,
        )
        self.revisions.append(revision)
        return revision.id

    async def get(self, revision_id):
        return next((r for r in self.revisions if r.id == revision_id), None)

    async def recent(self, limit=10, path=None):
        matching = [r for r in reversed(self.revisions) if path in (None, r.path)]
        return matching[:limit]

    async def latest_text(self):
        return self.revisions[-1].after_text if self.revisions else None

    async def mark_undone(self, revision_id, undone_by):
        index = revision_id - 1
        self.revisions[index] = dataclasses.replace(
            self.revisions[index], undone_by=undone_by
        )


@pytest.fixture(autouse=True)
def history(monkeypatch):
    fake = FakeHistory()
    for name in ("record", "get", "recent", "latest_text", "mark_undone"):
        monkeypatch.setattr(config_history, name, getattr(fake, name))
    return fake


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
    assert (await config_edit.reload()).problems == []
    assert config.config["roles"]["staff_role"] == 7


@pytest.mark.asyncio
async def test_reload_keeps_the_running_config_when_the_file_is_broken(live_config):
    live_config.write_text("roles: [unclosed")
    with pytest.raises(config_edit.EditRefused):
        await config_edit.reload()
    assert config.config["roles"]["staff_role"] == 1


# --- the commands -------------------------------------------------------------


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return FakeMessage()


class FakeBot:
    def __init__(self, log_channel=None):
        self.dispatched = []
        self.log_channel = log_channel

    def dispatch(self, event, *args):
        self.dispatched.append(event)

    def get_channel(self, channel_id):
        return self.log_channel


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

    await config_editor.ConfigEditor.view.callback(cog, ctx, "roles.staff_role")

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
        "✅ Saved **antiscam.timeout_days** · #1\n```ansi\n"
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


# --- reverting ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_edit_that_changes_nothing_is_refused(live_config):
    with pytest.raises(config_edit.EditRefused, match="already set"):
        await config_edit.edit(FakeMember(), "antiscam.timeout_days", lambda _: 28)


@pytest.mark.asyncio
async def test_reverting_puts_back_only_that_setting(live_config):
    first = await config_edit.edit(FakeMember(), "antiscam.timeout_days", lambda _: 7)
    await config_edit.edit(FakeMember(), "roles.staff_role", lambda _: 55)

    await config_edit.revert(FakeMember(), "antiscam.timeout_days", first.before_text)

    assert config.config["antiscam"]["timeout_days"] == 28
    assert config.config["roles"]["staff_role"] == 55


@pytest.mark.asyncio
async def test_reverting_a_new_roster_takes_it_back_out(live_config):
    created = await config_edit.edit(
        FakeMember(), "gameheads.attendants", lambda _: {"a": "a@b.c"}
    )

    await config_edit.revert(FakeMember(), "gameheads.attendants", created.before_text)

    assert "attendants" not in config.config["gameheads"]
    assert text(live_config) == SAMPLE


def test_a_huge_snippet_is_cut_to_one_message():
    snippet = "```ansi\n" + "x\n" * 3000 + "```"
    content = config_editor.fit("header", snippet)
    assert len(content) <= config_editor.DISCORD_LIMIT
    assert content.endswith("\n...\n```")


# --- history, the log and undo ------------------------------------------------


def editor(log_channel=None):
    return config_editor.ConfigEditor(FakeBot(log_channel))


@pytest.mark.asyncio
async def test_a_change_is_recorded_and_posted_with_undo(live_config, history):
    # in the file, since a save reloads the live config from it
    live_config.write_text(SAMPLE + "config_log:\n  channel: 5\n", encoding="utf-8")
    channel = FakeChannel()
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.edit_number.callback(
        editor(channel), ctx, "antiscam.timeout_days", "7"
    )

    assert [(r.action, r.path) for r in history.revisions] == [
        ("edit", "antiscam.timeout_days")
    ]
    post = channel.sent[0]
    assert post["content"].startswith(
        "📝 **lilac** changed **antiscam.timeout_days** · #1"
    )
    assert post["view"].children[0].custom_id == "config-undo:1"


@pytest.mark.asyncio
async def test_undo_puts_it_back_as_a_new_revision(live_config, history):
    cog = editor()
    ctx = FakeApplicationContext(FakeMember())
    await config_editor.ConfigEditor.edit_number.callback(
        cog, ctx, "antiscam.timeout_days", "7"
    )

    await config_editor.ConfigEditor.undo_command.callback(cog, ctx, 1)

    assert config.config["antiscam"]["timeout_days"] == 28
    assert ctx.replies[-1].startswith("↩️ Undid #1 · #2")
    assert history.revisions[0].undone_by == 2


@pytest.mark.asyncio
async def test_the_same_change_cant_be_undone_twice(live_config, history):
    cog = editor()
    ctx = FakeApplicationContext(FakeMember())
    await config_editor.ConfigEditor.edit_number.callback(
        cog, ctx, "antiscam.timeout_days", "7"
    )
    await config_editor.ConfigEditor.undo_command.callback(cog, ctx, 1)

    await config_editor.ConfigEditor.undo_command.callback(cog, ctx, 1)

    assert "already undone by #2" in ctx.replies[-1]


@pytest.mark.asyncio
async def test_a_snapshot_cant_be_undone(live_config, history):
    await history.record("startup", SAMPLE)
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.undo_command.callback(editor(), ctx, 1)

    assert "only /config changes undo" in ctx.replies[-1]


def undo_click(user, revision_id):
    interaction = FakeInteraction(user)
    interaction.data = {"custom_id": f"config-undo:{revision_id}"}
    interaction.message = FakeMessage()
    return interaction


@pytest.mark.asyncio
async def test_the_undo_button_works_and_retires_itself(live_config, history):
    cog = editor()
    ctx = FakeApplicationContext(FakeMember())
    await config_editor.ConfigEditor.edit_number.callback(
        cog, ctx, "antiscam.timeout_days", "7"
    )
    click = undo_click(FakeMember(), 1)

    await cog.on_interaction(click)

    assert config.config["antiscam"]["timeout_days"] == 28
    assert click.response.messages[0]["content"].startswith("↩️ Undid #1")
    assert click.message.edit_calls[0]["view"].children[0].disabled


@pytest.mark.asyncio
async def test_the_undo_button_is_bot_devs_only(live_config, history):
    cog = editor()
    ctx = FakeApplicationContext(FakeMember())
    await config_editor.ConfigEditor.edit_number.callback(
        cog, ctx, "antiscam.timeout_days", "7"
    )
    click = undo_click(FakeMember(id=5), 1)

    await cog.on_interaction(click)

    assert config.config["antiscam"]["timeout_days"] == 7
    assert "Only bot devs" in click.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_other_buttons_are_left_alone(live_config):
    click = FakeInteraction(FakeMember())
    click.data = {"custom_id": "something-else"}

    await editor().on_interaction(click)

    assert click.response.messages == []


@pytest.mark.asyncio
async def test_history_lists_newest_first(live_config, history):
    cog = editor()
    ctx = FakeApplicationContext(FakeMember())
    await history.record("startup", SAMPLE)
    await config_editor.ConfigEditor.edit_number.callback(
        cog, ctx, "antiscam.timeout_days", "7"
    )

    await config_editor.ConfigEditor.history.callback(cog, ctx, None)

    lines = ctx.replies[-1].splitlines()
    assert lines[0].startswith("`#2`")
    assert "**lilac** edit `antiscam.timeout_days`" in lines[0]
    assert "**the bot** startup" in lines[1]


# --- snapshots ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_snapshot_is_a_quiet_baseline(live_config, history):
    config.config["config_log"] = {"channel": 5}
    channel = FakeChannel()

    await editor(channel).snapshot("startup", SAMPLE)
    await editor(channel).snapshot("startup", SAMPLE)

    assert [r.action for r in history.revisions] == ["startup"]
    assert channel.sent == []


@pytest.mark.asyncio
async def test_offline_hand_edits_are_posted(live_config, history):
    config.config["config_log"] = {"channel": 5}
    channel = FakeChannel()
    await history.record("startup", SAMPLE)

    await editor(channel).snapshot(
        "startup", SAMPLE.replace("staff_role: 1", "staff_role: 9")
    )

    assert channel.sent[0]["content"].startswith(
        "📄 Changed while offline: config.yaml · #2"
    )


# --- falling back at startup ----------------------------------------------------


def test_a_good_file_never_asks_the_database(live_config, monkeypatch):
    def unreachable():
        raise AssertionError("a good file shouldn't need the fallback")

    monkeypatch.setattr(config_history, "latest_text_before_startup", unreachable)
    data, reason = config.load_config_or_fallback()
    assert data["roles"]["staff_role"] == 1
    assert reason is None


def test_a_broken_file_falls_back_and_is_kept_aside(live_config, monkeypatch):
    live_config.write_text("roles: [unclosed", encoding="utf-8")
    monkeypatch.setattr(config_history, "latest_text_before_startup", lambda: SAMPLE)

    data, reason = config.load_config_or_fallback()

    assert data["roles"]["staff_role"] == 1
    assert reason
    assert text(live_config) == SAMPLE
    assert text(live_config.with_name("config.yaml.broken")) == "roles: [unclosed"


def test_a_missing_file_falls_back_too(live_config, monkeypatch):
    live_config.unlink()
    monkeypatch.setattr(config_history, "latest_text_before_startup", lambda: SAMPLE)

    data, _ = config.load_config_or_fallback()

    assert data["roles"]["staff_role"] == 1
    assert text(live_config) == SAMPLE


def test_nothing_to_fall_back_on_fails_loudly(live_config, monkeypatch):
    live_config.write_text("roles: [unclosed", encoding="utf-8")
    monkeypatch.setattr(config_history, "latest_text_before_startup", lambda: None)

    with pytest.raises(RuntimeError, match="none is saved"):
        config.load_config_or_fallback()


# --- what the snippet shows around a change -----------------------------------


def lines(*parts):
    return "```ansi\n" + "\n".join(parts) + "\n```"


def bold(line):
    return f"{config_edit.BOLD}{line}{config_edit.RESET}"


def test_a_new_section_shows_only_itself():
    old = "antiscam:\n  purge_window_minutes: 60\n  exempt_staff: true\n"
    new = old + "config_log:\n  channel: 5\n"
    assert config_edit.snippet(old, new) == lines(
        bold("config_log:"), bold("  channel: 5")
    )


def test_a_change_under_another_section_ignores_it():
    old = "antiscam:\n  exempt_staff: true\nconfig_log:\n  channel: 1\n"
    new = old.replace("channel: 1", "channel: 2")
    assert config_edit.snippet(old, new) == lines("config_log:", bold("  channel: 2"))


def test_a_roster_addition_stays_inside_its_roster():
    old = "gameheads:\n  valorant:\n    a: x\n  smash:\n    b: y\n"
    new = old.replace("    a: x\n", "    a: x\n    c: z\n")
    assert config_edit.snippet(old, new) == lines(
        "  valorant:", "    a: x", bold("    c: z")
    )


# --- viewing ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_view_shows_one_roster(live_config):
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.view.callback(editor(), ctx, "gameheads.valorant")

    assert ctx.replies[-1].endswith("`someone`: someone@u.northwestern.edu")


@pytest.mark.asyncio
async def test_view_shows_a_whole_section(live_config):
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.view.callback(editor(), ctx, "gameheads")

    assert ctx.replies[-1].splitlines() == [
        "**gameheads.smash**: *unset*",
        "**gameheads.valorant**: `someone`: someone@u.northwestern.edu",
    ]


@pytest.mark.asyncio
async def test_view_of_something_unknown_says_so(live_config):
    ctx = FakeApplicationContext(FakeMember())

    await config_editor.ConfigEditor.view.callback(editor(), ctx, "discord")

    assert "Nothing to show under `discord`" in ctx.replies[-1]


@pytest.mark.asyncio
async def test_view_offers_sections_too(live_config):
    ctx = SimpleNamespace(value="gameheads", options={})

    offered = await config_editor.view_autocomplete(ctx)

    assert offered[:3] == ["gameheads", "gameheads.smash", "gameheads.valorant"]
    assert "roles.gameheads" in offered


def test_a_long_view_is_clipped():
    content = config_editor.clip("x" * 5000)
    assert len(content) <= config_editor.DISCORD_LIMIT
    assert content.endswith("narrow the path to see the rest")
