"""XP leveling.

  - XP from chatting (60s cooldown) and from voice time (per minute, not deafened,
    not in the AFK channel, and only when 2+ people are in the channel)
  - /rank, /levels (leaderboard)
  - /levelrole add | remove | list   -> role rewards at certain levels
  - /setlevel                        -> admin tool to set someone's level
  - Level-up messages go to the levels channel made by /setup, or to the channel
    the member was chatting in if /setup hasn't been run.
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

from rankcard import render_rank_card

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS levels (
    guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, xp BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id));
CREATE INDEX IF NOT EXISTS levels_rank_idx ON levels (guild_id, xp DESC);
CREATE TABLE IF NOT EXISTS level_roles (
    guild_id BIGINT NOT NULL, level INT NOT NULL, role_id BIGINT NOT NULL,
    PRIMARY KEY (guild_id, level));
CREATE TABLE IF NOT EXISTS guild_channels (
    guild_id BIGINT PRIMARY KEY, levels_channel_id BIGINT, modlog_channel_id BIGINT);
"""

MSG_XP = (15, 25)      # random XP per message
MSG_COOLDOWN = 60      # seconds between XP-earning messages
VOICE_XP = 10          # XP per minute in voice
INSTALLS = app_commands.AppInstallationType(guild=True, user=False)


def guild_only_cmd(f):
    f = app_commands.allowed_installs(guilds=True, users=False)(f)
    return app_commands.guild_only()(f)


def xp_for_next(level: int) -> int:
    """XP needed to go from `level` to `level + 1`."""
    return 5 * level**2 + 50 * level + 100


def level_from_xp(xp: int):
    """Returns (level, xp earned inside the current level)."""
    level = 0
    while xp >= xp_for_next(level):
        xp -= xp_for_next(level)
        level += 1
    return level, xp


def total_xp_for_level(level: int) -> int:
    return sum(xp_for_next(l) for l in range(level))


STAT_FILTER = "guild_id=$1 AND user_id=$2 AND hour >= now() - interval '14 days'"
MSG_SUM = (
    "SELECT COALESCE(SUM(count) FILTER (WHERE hour >= now() - interval '1 day'), 0) a, "
    "COALESCE(SUM(count) FILTER (WHERE hour >= now() - interval '7 days'), 0) b, "
    f"COALESCE(SUM(count), 0) c FROM stat_messages WHERE {STAT_FILTER}"
)
VOICE_SUM = (
    "SELECT COALESCE(SUM(seconds) FILTER (WHERE hour >= now() - interval '1 day'), 0) a, "
    "COALESCE(SUM(seconds) FILTER (WHERE hour >= now() - interval '7 days'), 0) b, "
    f"COALESCE(SUM(seconds), 0) c FROM stat_voice WHERE {STAT_FILTER} AND state <> 'afk'"
)
TOP_MSG = f"SELECT channel_id, SUM(count) v FROM stat_messages WHERE {STAT_FILTER} GROUP BY 1 ORDER BY v DESC LIMIT 2"
TOP_VOICE = (f"SELECT channel_id, SUM(seconds) v FROM stat_voice WHERE {STAT_FILTER} AND state <> 'afk' "
             "GROUP BY 1 ORDER BY v DESC LIMIT 1")
DAY_MSG = f"SELECT (hour AT TIME ZONE 'UTC')::date d, SUM(count) v FROM stat_messages WHERE {STAT_FILTER} GROUP BY 1"
DAY_VOICE = (f"SELECT (hour AT TIME ZONE 'UTC')::date d, SUM(seconds) / 3600.0 v FROM stat_voice "
             f"WHERE {STAT_FILTER} AND state <> 'afk' GROUP BY 1")


def fmt_date(dt) -> str:
    return f"{dt:%B} {dt.day}, {dt.year}" if dt else "Unknown"


class RankView(discord.ui.View):
    """Refresh button under the rank card (only the person who ran it can use it)."""

    def __init__(self, cog, guild, member, owner_id):
        super().__init__(timeout=180)
        self.cog, self.guild, self.member, self.owner_id = cog, guild, member, owner_id
        self.message = None

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, itx: discord.Interaction, button: discord.ui.Button):
        if itx.user.id != self.owner_id:
            return await itx.response.send_message("Only the person who ran this can refresh it.", ephemeral=True)
        await itx.response.defer()
        buf = await self.cog.rank_png(self.guild, self.member)
        await itx.edit_original_response(attachments=[discord.File(buf, "rank.png")])

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pool = None
        self.cooldowns = {}   # (guild, user) -> monotonic time of last XP message
        self.chan_cache = {}  # guild -> levels channel id (or None)

    async def cog_load(self):
        self.pool = self.bot.db.pool
        async with self.pool.acquire() as c:
            await c.execute(SCHEMA)
        self.voice_loop.start()

    async def cog_unload(self):
        self.voice_loop.cancel()

    # ------------------------------------------------------------------ helpers
    async def _member(self, guild, uid):
        m = guild.get_member(uid)
        if m:
            return m
        try:
            return await guild.fetch_member(uid)
        except discord.HTTPException:
            return None

    async def _announce_channel(self, guild):
        if guild.id not in self.chan_cache:
            self.chan_cache[guild.id] = await self.pool.fetchval(
                "SELECT levels_channel_id FROM guild_channels WHERE guild_id=$1", guild.id)
        cid = self.chan_cache[guild.id]
        ch = guild.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _grant_roles(self, guild, member, level):
        granted = []
        rows = await self.pool.fetch(
            "SELECT role_id FROM level_roles WHERE guild_id=$1 AND level <= $2", guild.id, level)
        for r in rows:
            role = guild.get_role(r["role_id"])
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Reached level {level}")
                    granted.append(role)
                except discord.HTTPException:
                    log.warning("couldn't give role %s in guild %s", role.id, guild.id)
        return granted

    async def add_xp(self, guild, uid, amount, member=None, fallback=None):
        new_xp = await self.pool.fetchval(
            "INSERT INTO levels (guild_id, user_id, xp) VALUES ($1,$2,$3) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = levels.xp + $3 RETURNING xp",
            guild.id, uid, amount,
        )
        old_level = level_from_xp(new_xp - amount)[0]
        new_level = level_from_xp(new_xp)[0]
        if new_level <= old_level:
            return
        member = member or await self._member(guild, uid)
        granted = await self._grant_roles(guild, member, new_level) if member else []
        channel = await self._announce_channel(guild) or fallback
        if not channel:
            return
        e = discord.Embed(title="🎉 Level up!", color=0x57F287,
                          description=f"<@{uid}> reached **level {new_level}**!")
        if granted:
            e.add_field(name="New role", value=", ".join(r.mention for r in granted))
        try:
            await channel.send(content=f"<@{uid}>", embed=e,
                               allowed_mentions=discord.AllowedMentions(users=[discord.Object(uid)]))
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ XP sources
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self.cooldowns.get(key, -MSG_COOLDOWN) < MSG_COOLDOWN:
            return
        self.cooldowns[key] = now
        try:
            await self.add_xp(message.guild, message.author.id, random.randint(*MSG_XP),
                              member=message.author, fallback=message.channel)
        except Exception:
            log.exception("message XP failed")

    @tasks.loop(seconds=60)
    async def voice_loop(self):
        cutoff = time.monotonic() - MSG_COOLDOWN
        self.cooldowns = {k: v for k, v in self.cooldowns.items() if v > cutoff}
        for guild in self.bot.guilds:
            afk = guild.afk_channel.id if guild.afk_channel else None
            for ch in [*guild.voice_channels, *guild.stage_channels]:
                if ch.id == afk or len(ch.voice_states) < 2:
                    continue
                for uid, vs in list(ch.voice_states.items()):
                    if vs.deaf or vs.self_deaf:
                        continue
                    m = guild.get_member(uid)
                    if m and m.bot:
                        continue
                    try:
                        await self.add_xp(guild, uid, VOICE_XP, member=m)
                    except Exception:
                        log.exception("voice XP failed")

    @voice_loop.before_loop
    async def _before_voice(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ commands
    async def rank_png(self, guild: discord.Guild, member: discord.Member):
        gid, uid = guild.id, member.id
        xp = await self.pool.fetchval("SELECT xp FROM levels WHERE guild_id=$1 AND user_id=$2", gid, uid) or 0
        level, into = level_from_xp(xp)
        pos = await self.pool.fetchval("SELECT COUNT(*) + 1 FROM levels WHERE guild_id=$1 AND xp > $2", gid, xp)

        msgs, voice = [0, 0, 0], [0.0, 0.0, 0.0]
        top_m, top_v, day_m, day_v = [], [], {}, {}
        try:
            r = await self.pool.fetchrow(MSG_SUM, gid, uid)
            msgs = [int(r["a"]), int(r["b"]), int(r["c"])]
            r = await self.pool.fetchrow(VOICE_SUM, gid, uid)
            voice = [int(r["a"]) / 3600, int(r["b"]) / 3600, int(r["c"]) / 3600]
            top_m = await self.pool.fetch(TOP_MSG, gid, uid)
            top_v = await self.pool.fetch(TOP_VOICE, gid, uid)
            day_m = {r["d"]: float(r["v"]) for r in await self.pool.fetch(DAY_MSG, gid, uid)}
            day_v = {r["d"]: float(r["v"]) for r in await self.pool.fetch(DAY_VOICE, gid, uid)}
        except asyncpg.PostgresError:
            log.warning("stat tables unavailable; rank card will show zeros (is cogs.stats loaded?)")

        def chan(cid):
            ch = guild.get_channel(cid)
            return ch.name if ch else "deleted-channel"

        top = []
        best = int(top_m[0]["v"]) if top_m else 1
        for r in top_m:
            top.append({"kind": "#", "label": chan(r["channel_id"]),
                        "value": f"{int(r['v']):,} messages", "frac": int(r["v"]) / best})
        while len(top) < 2:
            top.append(None)
        top.append({"kind": "VC", "label": chan(top_v[0]["channel_id"]),
                    "value": f"{int(top_v[0]['v']) / 3600:.1f} hours", "frac": 1.0} if top_v else None)

        today = datetime.now(timezone.utc).date()
        days = [today - timedelta(days=13 - i) for i in range(14)]
        try:
            avatar = await member.display_avatar.replace(size=128, format="png").read()
        except discord.HTTPException:
            avatar = None

        data = {
            "name": member.display_name, "server": guild.name, "avatar": avatar,
            "created": fmt_date(member.created_at), "joined": fmt_date(member.joined_at),
            "level": level, "rank": pos, "xp_into": into, "xp_need": xp_for_next(level),
            "msgs": msgs, "voice": voice, "top": top,
            "chart_m": [day_m.get(d, 0.0) for d in days], "chart_v": [day_v.get(d, 0.0) for d in days],
            "bot": self.bot.user.name if self.bot.user else "Bot",
        }
        return await asyncio.to_thread(render_rank_card, data)

    @app_commands.command(name="rank", description="Show a member's level card")
    @guild_only_cmd
    async def rank(self, itx: discord.Interaction, member: Optional[discord.Member] = None):
        member = member or itx.user
        await itx.response.defer()
        buf = await self.rank_png(itx.guild, member)
        view = RankView(self, itx.guild, member, itx.user.id)
        view.message = await itx.followup.send(file=discord.File(buf, "rank.png"), view=view, wait=True)

    @app_commands.command(name="levels", description="Server XP leaderboard")
    @guild_only_cmd
    async def levels(self, itx: discord.Interaction):
        rows = await self.pool.fetch(
            "SELECT user_id, xp FROM levels WHERE guild_id=$1 ORDER BY xp DESC LIMIT 10", itx.guild_id)
        if not rows:
            return await itx.response.send_message("Nobody has XP yet. Start chatting!", ephemeral=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — level {level_from_xp(r['xp'])[0]} "
            f"({r['xp']:,} XP)" for i, r in enumerate(rows)
        ]
        await itx.response.send_message(embed=discord.Embed(
            title="🏆 Leaderboard", description="\n".join(lines), color=0x5865F2))

    @app_commands.command(name="setlevel", description="Set a member's level (admin)")
    @app_commands.default_permissions(manage_guild=True)
    @guild_only_cmd
    async def setlevel(self, itx: discord.Interaction, member: discord.Member,
                       level: app_commands.Range[int, 0, 500]):
        if not itx.user.guild_permissions.manage_guild:
            return await itx.response.send_message("You need **Manage Server** for this.", ephemeral=True)
        await self.pool.execute(
            "INSERT INTO levels (guild_id, user_id, xp) VALUES ($1,$2,$3) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = EXCLUDED.xp",
            itx.guild_id, member.id, total_xp_for_level(level))
        await self._grant_roles(itx.guild, member, level)
        await itx.response.send_message(f"Set {member.mention} to **level {level}**.", ephemeral=True)

    levelrole = app_commands.Group(
        name="levelrole", description="Role rewards for levels", guild_only=True,
        allowed_installs=INSTALLS, default_permissions=discord.Permissions(manage_roles=True),
    )

    @levelrole.command(name="add", description="Give a role when members reach a level")
    async def levelrole_add(self, itx: discord.Interaction,
                            level: app_commands.Range[int, 1, 500], role: discord.Role):
        if role.is_default() or role.managed or role >= itx.guild.me.top_role:
            return await itx.response.send_message(
                "I can't manage that role. Pick a normal role that sits **below** my highest role.",
                ephemeral=True)
        await self.pool.execute(
            "INSERT INTO level_roles (guild_id, level, role_id) VALUES ($1,$2,$3) "
            "ON CONFLICT (guild_id, level) DO UPDATE SET role_id = EXCLUDED.role_id",
            itx.guild_id, level, role.id)
        await itx.response.send_message(
            f"{role.mention} will be given at **level {level}** (on each member's next level-up).",
            ephemeral=True)

    @levelrole.command(name="remove", description="Remove the reward for a level")
    async def levelrole_remove(self, itx: discord.Interaction, level: app_commands.Range[int, 1, 500]):
        res = await self.pool.execute(
            "DELETE FROM level_roles WHERE guild_id=$1 AND level=$2", itx.guild_id, level)
        await itx.response.send_message(
            "Removed." if res.endswith("1") else "No reward set for that level.", ephemeral=True)

    @levelrole.command(name="list", description="Show level role rewards")
    async def levelrole_list(self, itx: discord.Interaction):
        rows = await self.pool.fetch(
            "SELECT level, role_id FROM level_roles WHERE guild_id=$1 ORDER BY level", itx.guild_id)
        text = "\n".join(f"Level {r['level']} → <@&{r['role_id']}>" for r in rows) or "No role rewards yet."
        await itx.response.send_message(text, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Leveling(bot))
