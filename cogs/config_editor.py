import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import discord
import psycopg
from discord.ext import commands
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from utils import config, config_edit, config_history, config_schema, db
from utils.config_schema import Kind

GUILD_ID = config.secrets["discord"]["guild_id"]

# mentions render as names in the reply without pinging anyone
QUIET = discord.AllowedMentions.none()

# answered by on_interaction rather than a registered view, so it outlives restarts
UNDO_PREFIX = "config-undo:"
DISCORD_LIMIT = 2000


def fit(header: str, snippet: str) -> str:
    """header + snippet, cut short to fit one message if a hand edit made it huge."""
    content = f"{header}\n{snippet}"
    if len(content) <= DISCORD_LIMIT:
        return content
    room = DISCORD_LIMIT - len(header) - len("\n\n...\n```")
    return f"{header}\n{snippet[:room]}\n...\n```"


def undo_view(revision_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Undo",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{UNDO_PREFIX}{revision_id}",
            disabled=disabled,
        )
    )
    return view


async def undoable_autocomplete(
    ctx: discord.AutocompleteContext,
) -> list[discord.OptionChoice]:
    typed = str(ctx.value or "").lstrip("#")
    revisions = [r for r in await config_history.recent(25) if r.undoable]
    return [
        discord.OptionChoice(f"#{r.id} {r.user_name} {r.action} {r.path}"[:100], r.id)
        for r in revisions
        if typed in str(r.id)
    ]


# straight and the curly ones phone keyboards swap in
QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’"}


def unquote(text: str) -> str:
    """What someone meant, minus any quotes they wrapped it in -- the setting's kind
    decides the type, and the yaml writer adds quotes wherever the file needs them."""
    text = text.strip()
    while len(text) >= 2 and QUOTE_PAIRS.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    return text


def path_autocomplete(
    *kinds: Kind,
) -> Callable[[discord.AutocompleteContext], Coroutine[Any, Any, list[str]]]:
    """Offer every live path of the given kinds, wildcards expanded to what exists."""

    async def complete(ctx: discord.AutocompleteContext) -> list[str]:
        typed = (ctx.value or "").lower()
        paths = []
        for setting in config_schema.SETTINGS:
            if kinds and setting.kind not in kinds:
                continue
            if "*" in setting.path:
                paths += config_schema.concrete_paths(config.config, setting)
            else:
                paths.append(setting.path)
        return [path for path in paths if typed in path.lower()][:25]

    return complete


async def roster_autocomplete(ctx: discord.AutocompleteContext) -> list[str]:
    """Usernames already on the chosen roster, since they may have left the server."""
    roster = config_schema.lookup(config.config, ctx.options.get("path") or "")
    typed = (ctx.value or "").lower()
    names = list(roster) if isinstance(roster, dict) else []
    return [name for name in names if typed in name.lower()][:25]


def show(setting: config_schema.Setting, value: Any) -> str:
    """A value as people read it: mentions instead of bare ids."""
    if value is None or value == [] or value == {}:
        return "*unset*"
    kind = setting.kind
    if kind == Kind.ROLE:
        ids = value if isinstance(value, list) else [value]
        return ", ".join(f"<@&{role_id}>" for role_id in ids)
    if kind == Kind.CHANNEL:
        return f"<#{value}>"
    if kind == Kind.USER:
        return f"<@{value}>"
    if kind == Kind.USER_LIST:
        return ", ".join(f"<@{user_id}>" for user_id in value)
    if kind == Kind.EMAIL_MAP:
        return "\n".join(f"`{name}`: {email}" for name, email in value.items())
    return f"`{str(value).lower() if kind == Kind.BOOL else value}`"


def _as_list(current: Any) -> CommentedSeq:
    return current if isinstance(current, CommentedSeq) else CommentedSeq()


def _as_map(current: Any) -> CommentedMap:
    return current if isinstance(current, CommentedMap) else CommentedMap()


class ConfigEditor(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot: discord.Bot = bot
        # on_ready fires again on every reconnect, the snapshot only needs the first
        self.snapshotted = False

    config_group = discord.SlashCommandGroup(
        "config", "View and change the bot's config", guild_ids=[GUILD_ID]
    )
    add_group = config_group.create_subgroup(
        "add", "Add to a user list or roster", guild_ids=[GUILD_ID]
    )
    remove_group = config_group.create_subgroup(
        "remove", "Take something off a user list or roster", guild_ids=[GUILD_ID]
    )
    edit_group = config_group.create_subgroup(
        "edit", "Change a single setting", guild_ids=[GUILD_ID]
    )

    async def allowed(self, ctx: discord.ApplicationContext) -> bool:
        if config.is_bot_dev(ctx.author):
            return True
        await ctx.respond("❌ Only bot devs can use /config.", ephemeral=True)
        return False

    async def apply(
        self,
        ctx: discord.ApplicationContext,
        path: str,
        kinds: tuple[Kind, ...],
        change: Callable[[Any], Any],
    ) -> None:
        """Run one edit end to end and report it back as old -> new."""
        if not await self.allowed(ctx):
            return
        setting = config_schema.find(path)
        if setting is None or setting.kind not in kinds:
            wanted = " or ".join(kinds)
            await ctx.respond(f"❌ `{path}` isn't a {wanted} setting.", ephemeral=True)
            return
        try:
            saved = await config_edit.edit(ctx.author, path, change)
        except config_edit.EditRefused as refused:
            await ctx.respond(f"❌ Not saved: {refused}", ephemeral=True)
            return
        except OSError as error:
            # prod's config mount has to be writable for this to work at all
            await ctx.respond(f"❌ Couldn't write the config: {error}", ephemeral=True)
            return
        print(
            f"[config] {ctx.author.name} changed {path}: {saved.old!r} -> {saved.new!r}"
        )
        revision = await self.committed(ctx.author, "edit", path, saved)
        await ctx.respond(
            fit(f"✅ Saved **{path}**{revision_tag(revision)}", saved.snippet),
            ephemeral=True,
        )

    async def committed(
        self,
        member: discord.abc.User,
        action: str,
        path: str,
        saved: config_edit.Saved,
        note: str = "changed",
    ) -> int | None:
        """Everything after a save: tell the cogs, keep the revision, post the log."""
        self.bot.dispatch("config_changed")
        try:
            revision = await config_history.record(
                action, saved.after_text, saved.before_text, path, member
            )
        except psycopg.Error as error:
            # the save already landed, losing its history entry shouldn't undo that
            print(f"[config] couldn't record {path} in history: {error}")
            return None
        await self.log(
            fit(f"📝 **{member.name}** {note} **{path}** · #{revision}", saved.snippet),
            undo_view(revision),
        )
        return revision

    async def log(self, content: str, view: discord.ui.View | None = None) -> None:
        channel_id = (config.config.get("config_log") or {}).get("channel")
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        if view is None:
            await channel.send(content, allowed_mentions=QUIET)
        else:
            await channel.send(content, view=view, allowed_mentions=QUIET)

    async def undo(
        self, member: discord.Member, revision_id: int
    ) -> tuple[config_edit.Saved, int | None]:
        """Put one edit's setting back how it was before, as a new revision."""
        revision = await config_history.get(revision_id)
        if revision is None:
            raise config_edit.EditRefused(f"there's no revision #{revision_id}")
        if revision.undone_by is not None:
            raise config_edit.EditRefused(
                f"#{revision_id} was already undone by #{revision.undone_by}"
            )
        if not revision.undoable or not revision.path or revision.before_text is None:
            raise config_edit.EditRefused(
                f"#{revision_id} was a {revision.action}, only /config changes undo"
            )
        saved = await config_edit.revert(member, revision.path, revision.before_text)
        new_revision = await self.committed(
            member, "undo", revision.path, saved, note=f"undid #{revision_id} on"
        )
        if new_revision is not None:
            await config_history.mark_undone(revision_id, new_revision)
        return saved, new_revision

    @config_group.command(name="get", description="Show a setting's current value")
    async def get(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path", description="Setting", autocomplete=path_autocomplete()
        ),
    ) -> None:
        if not await self.allowed(ctx):
            return
        setting = config_schema.find(path)
        if setting is None:
            await ctx.respond(f"❌ `{path}` isn't an editable setting.", ephemeral=True)
            return
        value = config_schema.lookup(config.config, path)
        await ctx.respond(
            f"**{path}** ({setting.kind}): {setting.description}\n{show(setting, value)}",
            ephemeral=True,
            allowed_mentions=QUIET,
        )

    @edit_group.command(name="role", description="Point a setting at a role")
    async def edit_role(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="Setting",
            autocomplete=path_autocomplete(Kind.ROLE),
        ),
        role: discord.Role = discord.Option(discord.Role, name="role"),
    ) -> None:
        await self.apply(ctx, path, (Kind.ROLE,), lambda _: role.id)

    @edit_group.command(name="channel", description="Point a setting at a channel")
    async def edit_channel(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="Setting",
            autocomplete=path_autocomplete(Kind.CHANNEL),
        ),
        channel: discord.TextChannel = discord.Option(
            discord.TextChannel, name="channel"
        ),
    ) -> None:
        await self.apply(ctx, path, (Kind.CHANNEL,), lambda _: channel.id)

    @edit_group.command(name="user", description="Point a setting at a user")
    async def edit_user(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="Setting",
            autocomplete=path_autocomplete(Kind.USER),
        ),
        user: discord.User = discord.Option(discord.User, name="user"),
    ) -> None:
        await self.apply(ctx, path, (Kind.USER,), lambda _: user.id)

    @edit_group.command(name="number", description="Set a number or id")
    async def edit_number(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="Setting",
            autocomplete=path_autocomplete(Kind.NUMBER),
        ),
        # text, since discord's integer option tops out below snowflake ids
        value: str = discord.Option(name="value", description="A whole number"),
    ) -> None:
        try:
            number = int(unquote(value))
        except ValueError:
            await ctx.respond(f"❌ `{value}` isn't a whole number.", ephemeral=True)
            return
        await self.apply(ctx, path, (Kind.NUMBER,), lambda _: number)

    @edit_group.command(name="bool", description="Turn a setting on or off")
    async def edit_bool(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="Setting",
            autocomplete=path_autocomplete(Kind.BOOL),
        ),
        value: bool = discord.Option(bool, name="value"),
    ) -> None:
        await self.apply(ctx, path, (Kind.BOOL,), lambda _: value)

    @add_group.command(name="user", description="Add a user to a user list")
    async def add_user(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="User list",
            autocomplete=path_autocomplete(Kind.USER_LIST),
        ),
        user: discord.User = discord.Option(discord.User, name="user"),
    ) -> None:
        def change(current: Any) -> CommentedSeq:
            users = _as_list(current)
            if user.id not in users:
                users.append(user.id)
            return users

        await self.apply(ctx, path, (Kind.USER_LIST,), change)

    @remove_group.command(name="user", description="Take a user off a user list")
    async def remove_user(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="User list",
            autocomplete=path_autocomplete(Kind.USER_LIST),
        ),
        user: discord.User = discord.Option(discord.User, name="user"),
    ) -> None:
        def change(current: Any) -> CommentedSeq:
            users = _as_list(current)
            if user.id not in users:
                raise config_edit.EditRefused(f"{user.name} isn't on `{path}`")
            users.remove(user.id)
            return users

        await self.apply(ctx, path, (Kind.USER_LIST,), change)

    @add_group.command(
        name="email", description="Add someone and their email to a roster"
    )
    async def add_email(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="Roster, or a new one like gameheads.attendants",
            autocomplete=path_autocomplete(Kind.EMAIL_MAP),
        ),
        user: discord.User = discord.Option(discord.User, name="user"),
        email: str = discord.Option(name="email"),
    ) -> None:
        email = unquote(email)
        if "@" not in email:
            await ctx.respond(f"❌ `{email}` isn't an email.", ephemeral=True)
            return

        def change(current: Any) -> CommentedMap:
            roster = _as_map(current)
            if user.name in roster:
                raise config_edit.EditRefused(
                    f"{user.name} is already on `{path}`, use /config edit email"
                )
            # keyed by username since that's what reservations look up, quoted to match
            roster[user.name] = DoubleQuotedScalarString(email)
            return roster

        await self.apply(ctx, path, (Kind.EMAIL_MAP,), change)

    @remove_group.command(name="email", description="Take someone off a roster")
    async def remove_email(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="Roster",
            autocomplete=path_autocomplete(Kind.EMAIL_MAP),
        ),
        username: str = discord.Option(
            name="username", autocomplete=roster_autocomplete
        ),
    ) -> None:
        username = unquote(username)

        def change(current: Any) -> CommentedMap:
            roster = _as_map(current)
            if username not in roster:
                raise config_edit.EditRefused(f"{username} isn't on `{path}`")
            del roster[username]
            return roster

        await self.apply(ctx, path, (Kind.EMAIL_MAP,), change)

    @edit_group.command(name="email", description="Change someone's email on a roster")
    async def edit_email(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="Roster",
            autocomplete=path_autocomplete(Kind.EMAIL_MAP),
        ),
        username: str = discord.Option(
            name="username", autocomplete=roster_autocomplete
        ),
        email: str = discord.Option(name="email"),
    ) -> None:
        username, email = unquote(username), unquote(email)
        if "@" not in email:
            await ctx.respond(f"❌ `{email}` isn't an email.", ephemeral=True)
            return

        def change(current: Any) -> CommentedMap:
            roster = _as_map(current)
            if username not in roster:
                raise config_edit.EditRefused(
                    f"{username} isn't on `{path}`, use /config add email"
                )
            roster[username] = DoubleQuotedScalarString(email)
            return roster

        await self.apply(ctx, path, (Kind.EMAIL_MAP,), change)

    @config_group.command(
        name="reload", description="Pick up edits made to the file directly"
    )
    async def reload(self, ctx: discord.ApplicationContext) -> None:
        if not await self.allowed(ctx):
            return
        try:
            reloaded = await config_edit.reload()
        except config_edit.EditRefused as refused:
            await ctx.respond(f"❌ Kept the running config: {refused}", ephemeral=True)
            return
        self.bot.dispatch("config_changed")
        await self.snapshot("reload", reloaded.text, ctx.author)
        if reloaded.problems:
            listed = "\n".join(f"- {problem}" for problem in reloaded.problems)
            await ctx.respond(f"⚠️ Reloaded, but:\n{listed}", ephemeral=True)
            return
        await ctx.respond("✅ Reloaded.", ephemeral=True)

    @config_group.command(name="undo", description="Take back a /config change")
    async def undo_command(
        self,
        ctx: discord.ApplicationContext,
        revision: int = discord.Option(
            int,
            name="revision",
            description="The change's number, from /config history",
            autocomplete=undoable_autocomplete,
        ),
    ) -> None:
        if not await self.allowed(ctx):
            return
        try:
            saved, new_revision = await self.undo(ctx.author, revision)
        except config_edit.EditRefused as refused:
            await ctx.respond(f"❌ Not undone: {refused}", ephemeral=True)
            return
        except OSError as error:
            await ctx.respond(f"❌ Couldn't write the config: {error}", ephemeral=True)
            return
        await ctx.respond(
            fit(f"↩️ Undid #{revision}{revision_tag(new_revision)}", saved.snippet),
            ephemeral=True,
        )

    @config_group.command(name="history", description="Recent config changes")
    async def history(
        self,
        ctx: discord.ApplicationContext,
        path: str = discord.Option(
            name="path",
            description="Only this setting",
            autocomplete=path_autocomplete(),
            default=None,
        ),
    ) -> None:
        if not await self.allowed(ctx):
            return
        revisions = await config_history.recent(15, path)
        if not revisions:
            await ctx.respond("No changes recorded yet.", ephemeral=True)
            return
        await ctx.respond(
            "\n".join(describe(revision) for revision in revisions), ephemeral=True
        )

    async def snapshot(
        self, action: str, text: str, member: discord.abc.User | None = None
    ) -> None:
        """Keep text as a revision if it isn't what history already ends on."""
        latest = await config_history.latest_text()
        if text == latest:
            return
        revision = await config_history.record(action, text, latest, member=member)
        # the very first snapshot is just the baseline, nothing to compare it with
        if latest is not None:
            who = f"**{member.name}** reloaded" if member else "Changed while offline:"
            await self.log(
                fit(
                    f"📄 {who} config.yaml · #{revision}",
                    config_edit.snippet(latest, text),
                )
            )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.snapshotted:
            return
        self.snapshotted = True
        # bot.py opens the pool in its own on_ready, which may not have run yet
        while db.pool.closed:
            await asyncio.sleep(0.5)
        await self.snapshot("startup", config.CONFIG_PATH.read_text(encoding="utf-8"))
        if config.config_fallback_reason:
            await self.log(
                f"🚨 config.yaml wouldn't load ({config.config_fallback_reason}), so "
                "the bot started on the last saved copy. The broken one is "
                "config.yaml.broken."
            )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith(UNDO_PREFIX):
            return
        if not config.is_bot_dev(interaction.user):
            await interaction.response.send_message(
                "❌ Only bot devs can use /config.", ephemeral=True
            )
            return
        revision = int(custom_id.removeprefix(UNDO_PREFIX))
        try:
            saved, new_revision = await self.undo(interaction.user, revision)
        except (config_edit.EditRefused, OSError) as refused:
            await interaction.response.send_message(
                f"❌ Not undone: {refused}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            fit(f"↩️ Undid #{revision}{revision_tag(new_revision)}", saved.snippet),
            ephemeral=True,
        )
        if interaction.message is not None:
            await interaction.message.edit(view=undo_view(revision, disabled=True))


def revision_tag(revision: int | None) -> str:
    return f" · #{revision}" if revision is not None else " (not in history)"


def describe(revision: config_history.Revision) -> str:
    """One line of /config history."""
    when = f"<t:{int(revision.changed_at.timestamp())}:R>"
    who = revision.user_name or "the bot"
    what = f"{revision.action} `{revision.path}`" if revision.path else revision.action
    undone = f" · undone by #{revision.undone_by}" if revision.undone_by else ""
    return f"`#{revision.id}` {when} **{who}** {what}{undone}"


def setup(bot: discord.Bot) -> None:
    bot.add_cog(ConfigEditor(bot))
