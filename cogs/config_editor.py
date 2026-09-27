from collections.abc import Callable, Coroutine
from typing import Any

import discord
from discord.ext import commands
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from utils import config, config_edit, config_schema
from utils.config_schema import Kind

GUILD_ID = config.secrets["discord"]["guild_id"]

# mentions render as names in the reply without pinging anyone
QUIET = discord.AllowedMentions.none()

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
        self.bot.dispatch("config_changed")
        print(
            f"[config] {ctx.author.name} changed {path}: {saved.old!r} -> {saved.new!r}"
        )
        await ctx.respond(f"✅ Saved **{path}**\n{saved.snippet}", ephemeral=True)

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
            problems = await config_edit.reload()
        except config_edit.EditRefused as refused:
            await ctx.respond(f"❌ Kept the running config: {refused}", ephemeral=True)
            return
        self.bot.dispatch("config_changed")
        if problems:
            listed = "\n".join(f"- {problem}" for problem in problems)
            await ctx.respond(f"⚠️ Reloaded, but:\n{listed}", ephemeral=True)
            return
        await ctx.respond("✅ Reloaded.", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(ConfigEditor(bot))
