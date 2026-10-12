import re
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import embed, error_embed

MAX_TIMEOUT = timedelta(days=28)  # Discord's hard limit
DURATION_RE = re.compile(r"(\d+)([smhdw])")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
NO_REASON = "No reason provided"


class NotModerator(commands.CheckFailure):
    pass


# ---------------------------------------------------------------- helpers

def parse_duration(text: str) -> timedelta | None:
    """'30m', '2h', '1d12h', '1w' -> timedelta. None if it isn't valid."""
    text = text.strip().lower().replace(" ", "")
    parts = DURATION_RE.findall(text)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != text:
        return None
    return timedelta(seconds=sum(int(n) * UNITS[u] for n, u in parts))


def fmt_td(td: timedelta) -> str:
    secs, out = int(td.total_seconds()), []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        q, secs = divmod(secs, size)
        if q:
            out.append(f"{q}{name}")
    return " ".join(out) or "0s"


def audit_reason(ctx: commands.Context, reason: str) -> str:
    return f"{ctx.author} ({ctx.author.id}): {reason}"[:512]


async def guild_check(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        raise commands.NoPrivateMessage("This command only works in servers.")
    return True


async def is_mod(ctx: commands.Context) -> bool:
    """Allowed: server owner, anyone with Administrator, or anyone holding the
    server's configured moderator role."""
    await guild_check(ctx)
    member = ctx.author
    if member.id == ctx.guild.owner_id or member.guild_permissions.administrator:
        return True
    role_id = await ctx.bot.db.get_mod_role(ctx.guild.id)
    if role_id and any(r.id == role_id for r in member.roles):
        return True
    if role_id is None:
        raise NotModerator(
            "No moderator role is set for this server. An admin can set one with `/modrole set`."
        )
    raise NotModerator("You need the moderator role to use this command.")


def mod_only(func):
    """Server-only command restricted to moderators. Apply BELOW the command decorator."""
    func = commands.check(is_mod)(func)
    func = app_commands.allowed_installs(guilds=True, users=False)(func)
    func = app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)(func)
    return func


def guild_only_cmd(func):
    func = commands.check(guild_check)(func)
    func = app_commands.allowed_installs(guilds=True, users=False)(func)
    func = app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)(func)
    return func


def hierarchy_error(ctx: commands.Context, target: discord.Member, *, allow_self: bool = False) -> str | None:
    """Returns a reason the action must be refused, or None if it's allowed."""
    guild, author, me = ctx.guild, ctx.author, ctx.guild.me
    if target.id == me.id:
        return "I'm not going to do that to myself."
    if target.id == guild.owner_id:
        return "You can't do that to the server owner."
    if target.id == author.id:
        if not allow_self:
            return "You can't do that to yourself."
    elif author.id != guild.owner_id:
        if target.guild_permissions.administrator and not author.guild_permissions.administrator:
            return "You can't moderate an administrator."
        if target.top_role >= author.top_role:
            return "That user's highest role is the same as or higher than yours."
    if target.top_role >= me.top_role:
        return "That user's highest role is above mine. Move my role higher in Server Settings → Roles."
    return None


async def get_member(ctx: commands.Context, user: discord.abc.User) -> discord.Member | None:
    """Resolve a user to a guild Member (None if they aren't in the server).
    Falls back to an API call because we don't use the privileged Members intent."""
    if isinstance(user, discord.Member):
        return user
    member = ctx.guild.get_member(user.id)
    if member:
        return member
    try:
        return await ctx.guild.fetch_member(user.id)
    except discord.NotFound:
        return None


async def fail(ctx: commands.Context, message: str):
    await ctx.send(embed=error_embed(message), ephemeral=True)


async def try_dm(user: discord.abc.User, title: str, guild: discord.Guild, reason: str, extra: str = ""):
    """Best-effort DM; users with DMs closed are silently skipped."""
    try:
        await user.send(embed=embed(title, f"**Server:** {guild.name}\n**Reason:** {reason}{extra}"))
    except discord.HTTPException:
        pass


async def discord_error(ctx: commands.Context, e: discord.HTTPException):
    if isinstance(e, discord.Forbidden):
        await fail(ctx, "I don't have permission to do that. Check my permissions and that my role is high enough.")
    else:
        await fail(ctx, f"Discord rejected that: {e.text or e}")


# ---------------------------------------------------------------- cog

class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    # ===== moderator role config =====

    @commands.hybrid_group(
        name="modrole",
        description="Manage the role that can use moderation commands",
        invoke_without_command=True,
    )
    @guild_only_cmd
    async def modrole(self, ctx: commands.Context):
        await self._show_modrole(ctx)

    async def _show_modrole(self, ctx: commands.Context):
        role_id = await self.db.get_mod_role(ctx.guild.id)
        if role_id and ctx.guild.get_role(role_id):
            text = f"Moderator role: <@&{role_id}>\nAdministrators and the server owner can always use moderation commands."
        elif role_id:
            text = "The saved moderator role no longer exists. Set a new one with `/modrole set`."
        else:
            text = "No moderator role is set. Only administrators can use moderation commands.\nSet one with `/modrole set`."
        await ctx.send(embed=embed("Moderator role", text))

    @modrole.command(name="show", description="Show the current moderator role")
    @guild_only_cmd
    async def modrole_show(self, ctx: commands.Context):
        await self._show_modrole(ctx)

    @modrole.command(name="set", description="Set the role allowed to use moderation commands")
    @guild_only_cmd
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(role="Members with this role can use moderation commands")
    async def modrole_set(self, ctx: commands.Context, role: discord.Role):
        if role.is_default():
            return await fail(ctx, "You can't use @everyone as the moderator role.")
        if role.managed:
            return await fail(ctx, "That role is managed by an integration. Pick a normal role.")
        await self.db.set_mod_role(ctx.guild.id, role.id)
        await ctx.send(
            embed=embed("Moderator role set", f"Members with {role.mention} can now use moderation commands.")
        )

    @modrole.command(name="clear", description="Remove the moderator role (only admins can moderate)")
    @guild_only_cmd
    @commands.has_permissions(manage_guild=True)
    async def modrole_clear(self, ctx: commands.Context):
        await self.db.set_mod_role(ctx.guild.id, None)
        await ctx.send(embed=embed("Moderator role cleared", "Only administrators can use moderation commands now."))

    # ===== ban / unban / kick =====

    @commands.hybrid_command(name="ban", description="Ban a user from the server")
    @mod_only
    @app_commands.describe(user="Who to ban (you can paste an ID for users who aren't in the server)", reason="Why")
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, user: discord.User, *, reason: str = NO_REASON):
        member = await get_member(ctx, user)
        if member:
            if err := hierarchy_error(ctx, member):
                return await fail(ctx, err)
        elif user.id == ctx.author.id or user.id == ctx.bot.user.id:
            return await fail(ctx, "You can't ban that user.")
        try:
            if member:
                await try_dm(user, "You were banned", ctx.guild, reason)
            await ctx.guild.ban(user, reason=audit_reason(ctx, reason), delete_message_seconds=0)
        except discord.HTTPException as e:
            return await discord_error(ctx, e)
        await ctx.send(embed=embed("🔨 User banned", f"**{user}** (`{user.id}`) was banned.\n**Reason:** {reason}"))

    @commands.hybrid_command(name="unban", description="Unban a user (by ID or exact username)")
    @mod_only
    @app_commands.describe(user="User ID or exact username of the banned user", reason="Why")
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user: str, *, reason: str = NO_REASON):
        target = None
        try:
            if user.isdigit():
                target = (await ctx.guild.fetch_ban(discord.Object(id=int(user)))).user
            else:
                wanted = user.lower().lstrip("@")
                async for entry in ctx.guild.bans(limit=None):
                    if entry.user.name.lower() == wanted:
                        target = entry.user
                        break
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            return await discord_error(ctx, e)
        if target is None:
            return await fail(ctx, f"No banned user matches `{user}`. Try their user ID.")
        try:
            await ctx.guild.unban(target, reason=audit_reason(ctx, reason))
        except discord.HTTPException as e:
            return await discord_error(ctx, e)
        await ctx.send(embed=embed("✅ User unbanned", f"**{target}** (`{target.id}`) was unbanned."))

    @commands.hybrid_command(name="kick", description="Kick a member from the server")
    @mod_only
    @app_commands.describe(member="Who to kick", reason="Why")
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = NO_REASON):
        if err := hierarchy_error(ctx, member):
            return await fail(ctx, err)
        try:
            await try_dm(member, "You were kicked", ctx.guild, reason)
            await member.kick(reason=audit_reason(ctx, reason))
        except discord.HTTPException as e:
            return await discord_error(ctx, e)
        await ctx.send(embed=embed("👢 Member kicked", f"**{member}** was kicked.\n**Reason:** {reason}"))

    # ===== mute (timeout) / unmute =====

    @commands.hybrid_command(name="mute", aliases=["timeout"], description="Mute (time out) a member")
    @mod_only
    @app_commands.describe(member="Who to mute", duration="e.g. 30m, 2h, 1d12h (max 28d)", reason="Why")
    @commands.bot_has_permissions(moderate_members=True)
    async def mute(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = NO_REASON):
        td = parse_duration(duration)
        if td is None or td < timedelta(seconds=1) or td > MAX_TIMEOUT:
            return await fail(ctx, "Invalid duration. Use something like `30m`, `2h` or `1d12h` (max 28 days).")
        if err := hierarchy_error(ctx, member):
            return await fail(ctx, err)
        until = discord.utils.utcnow() + td
        try:
            await member.timeout(td, reason=audit_reason(ctx, reason))
        except discord.HTTPException as e:
            return await discord_error(ctx, e)
        await try_dm(member, "You were muted", ctx.guild, reason, f"\n**Duration:** {fmt_td(td)}")
        await ctx.send(
            embed=embed(
                "🔇 Member muted",
                f"{member.mention} is muted for **{fmt_td(td)}** (ends {discord.utils.format_dt(until, 'R')}).\n**Reason:** {reason}",
            )
        )

    @commands.hybrid_command(name="unmute", aliases=["untimeout"], description="Remove a member's mute")
    @mod_only
    @app_commands.describe(member="Who to unmute", reason="Why")
    @commands.bot_has_permissions(moderate_members=True)
    async def unmute(self, ctx: commands.Context, member: discord.Member, *, reason: str = NO_REASON):
        if not member.is_timed_out():
            return await fail(ctx, f"{member.mention} isn't muted.")
        if err := hierarchy_error(ctx, member, allow_self=True):
            return await fail(ctx, err)
        try:
            await member.timeout(None, reason=audit_reason(ctx, reason))
        except discord.HTTPException as e:
            return await discord_error(ctx, e)
        await ctx.send(embed=embed("🔊 Member unmuted", f"{member.mention} can talk again."))

    # ===== nickname =====

    @commands.hybrid_command(name="nick", aliases=["nickname", "setnick"], description="Change or reset a member's nickname")
    @mod_only
    @app_commands.describe(member="Whose nickname to change", nickname="New nickname (leave empty to reset)")
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nick(self, ctx: commands.Context, member: discord.Member, *, nickname: Optional[str] = None):
        if nickname and len(nickname) > 32:
            return await fail(ctx, "Nicknames can be at most 32 characters.")
        if err := hierarchy_error(ctx, member, allow_self=True):
            return await fail(ctx, err)
        try:
            await member.edit(nick=nickname or None, reason=audit_reason(ctx, "Nickname change"))
        except discord.HTTPException as e:
            return await discord_error(ctx, e)
        text = f"{member.mention}'s nickname is now **{nickname}**." if nickname else f"{member.mention}'s nickname was reset."
        await ctx.send(embed=embed("✏️ Nickname updated", text))

    # ===== purge / slowmode =====

    @commands.hybrid_command(name="purge", aliases=["clear"], description="Delete recent messages in this channel")
    @mod_only
    @app_commands.describe(amount="How many recent messages to scan (1-100)", member="Only delete this member's messages")
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(
        self, ctx: commands.Context, amount: app_commands.Range[int, 1, 100], member: Optional[discord.Member] = None
    ):
        await ctx.defer(ephemeral=True)
        if ctx.interaction is None:  # prefix: remove the command message itself first
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
        check = (lambda m: m.author.id == member.id) if member else None
        try:
            deleted = await ctx.channel.purge(limit=amount, check=check)
        except discord.HTTPException as e:
            return await discord_error(ctx, e)
        await ctx.send(
            embed=embed("🧹 Messages deleted", f"Deleted **{len(deleted)}** message{'s' if len(deleted) != 1 else ''}."),
            ephemeral=True,
            delete_after=5 if ctx.interaction is None else None,
        )

    @commands.hybrid_command(name="slowmode", description="Set this channel's slowmode (0 turns it off)")
    @mod_only
    @app_commands.describe(seconds="Seconds between messages (0-21600)")
    @commands.bot_has_permissions(manage_channels=True)
    async def slowmode(self, ctx: commands.Context, seconds: app_commands.Range[int, 0, 21600]):
        try:
            await ctx.channel.edit(slowmode_delay=seconds, reason=audit_reason(ctx, "Slowmode change"))
        except (discord.HTTPException, TypeError) as e:
            if isinstance(e, discord.HTTPException):
                return await discord_error(ctx, e)
            return await fail(ctx, "Slowmode can't be set in this kind of channel.")
        text = f"Slowmode set to **{seconds}s**." if seconds else "Slowmode turned off."
        await ctx.send(embed=embed("🐢 Slowmode", text))

    # ===== warnings =====

    @commands.hybrid_command(name="warn", description="Warn a member (saved to their record)")
    @mod_only
    @app_commands.describe(member="Who to warn", reason="Why")
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        if err := hierarchy_error(ctx, member):
            return await fail(ctx, err)
        reason = reason[:500]
        warn_id, total = await self.db.add_warning(ctx.guild.id, member.id, ctx.author.id, reason)
        await try_dm(member, "You received a warning", ctx.guild, reason)
        await ctx.send(
            embed=embed(
                "⚠️ Member warned",
                f"{member.mention} was warned (warning `#{warn_id}`, **{total}** total).\n**Reason:** {reason}",
            )
        )

    @commands.hybrid_command(name="warnings", aliases=["warns"], description="Show a user's warnings")
    @mod_only
    @app_commands.describe(user="Whose warnings to show")
    async def warnings(self, ctx: commands.Context, user: discord.User):
        rows, total = await self.db.list_warnings(ctx.guild.id, user.id, 10)
        if not rows:
            return await ctx.send(embed=embed("Warnings", f"**{user}** has no warnings."))
        lines = [
            f"`#{r['id']}` {discord.utils.format_dt(r['created_at'], 'd')} by <@{r['mod_id']}>: {r['reason']}"
            for r in rows
        ]
        e = embed(f"Warnings for {user}", "\n".join(lines))
        e.set_footer(text=f"Showing {len(rows)} of {total}")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="delwarn", aliases=["unwarn"], description="Delete a warning by its ID")
    @mod_only
    @app_commands.describe(warning_id="The number shown next to the warning, e.g. 12")
    async def delwarn(self, ctx: commands.Context, warning_id: app_commands.Range[int, 1, 2**62]):
        if not await self.db.delete_warning(ctx.guild.id, warning_id):
            return await fail(ctx, f"No warning `#{warning_id}` found in this server.")
        await ctx.send(embed=embed("Warning deleted", f"Warning `#{warning_id}` was removed."))


async def setup(bot):
    await bot.add_cog(Moderation(bot))
