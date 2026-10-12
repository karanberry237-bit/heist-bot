"""Statbot-style analytics cog.

Tracks messages + voice time per member / channel / hour, and gives you:
  /stats me | user | server | channel   -> overview embeds with charts
  /top                                  -> leaderboards (members or channels)
  /statdock add | remove | list         -> channel-name counters (Statdocks)
  /statrole add | remove | list         -> roles from stats (Statroles)

Needs only non-privileged intents (guilds, guild_messages, voice_states).
Works only in servers where the bot is guild-installed.
"""
import asyncio
import io
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import discord
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from discord import app_commands  # noqa: E402
from discord.app_commands import Choice  # noqa: E402
from discord.ext import commands, tasks  # noqa: E402

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS stat_messages (
    guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
    hour TIMESTAMPTZ NOT NULL, count INT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, channel_id, user_id, hour));
CREATE INDEX IF NOT EXISTS stat_messages_gh ON stat_messages (guild_id, hour);
CREATE TABLE IF NOT EXISTS stat_voice (
    guild_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
    hour TIMESTAMPTZ NOT NULL, state TEXT NOT NULL, seconds INT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, channel_id, user_id, hour, state));
CREATE INDEX IF NOT EXISTS stat_voice_gh ON stat_voice (guild_id, hour);
CREATE TABLE IF NOT EXISTS stat_docks (
    id SERIAL PRIMARY KEY, guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL UNIQUE, template TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stat_roles (
    id SERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, role_id BIGINT NOT NULL UNIQUE,
    metric TEXT NOT NULL, minimum INT NOT NULL, period_days INT NOT NULL);
CREATE TABLE IF NOT EXISTS stat_role_members (
    role_id BIGINT NOT NULL, user_id BIGINT NOT NULL, PRIMARY KEY (role_id, user_id));
"""

# Shared WHERE clause: $1 guild, $2 since (nullable), $3 user (nullable), $4 channel (nullable)
W = (
    "guild_id = $1 AND ($2::timestamptz IS NULL OR hour >= $2) "
    "AND ($3::bigint IS NULL OR user_id = $3) AND ($4::bigint IS NULL OR channel_id = $4)"
)
METRICS = {  # name -> (table, column, extra where)
    "messages": ("stat_messages", "count", ""),
    "voice": ("stat_voice", "seconds", " AND state <> 'afk'"),
}
PERIODS = [
    Choice(name="Last 24 hours", value=1),
    Choice(name="Last 7 days", value=7),
    Choice(name="Last 30 days", value=30),
    Choice(name="Last 90 days", value=90),
    Choice(name="All time", value=0),
]
PERIOD_LABEL = {c.value: c.name for c in PERIODS}
INSTALLS = app_commands.AppInstallationType(guild=True, user=False)


def guild_only_cmd(f):
    f = app_commands.allowed_installs(guilds=True, users=False)(f)
    return app_commands.guild_only()(f)


def now_utc():
    return datetime.now(timezone.utc)


def hour_now():
    return now_utc().replace(minute=0, second=0, microsecond=0)


def since_for(days: int):
    return None if days == 0 else now_utc() - timedelta(days=days)


def fmt_secs(s: int) -> str:
    h, rem = divmod(int(s), 3600)
    return f"{h:,}h {rem // 60}m"


def voice_state(vs: discord.VoiceState, guild: discord.Guild) -> str:
    if guild.afk_channel and vs.channel and vs.channel.id == guild.afk_channel.id:
        return "afk"
    if vs.deaf or vs.self_deaf:
        return "deafened"
    if vs.mute or vs.self_mute:
        return "muted"
    return "active"


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def make_chart(title, msgs: dict, voice: dict, unit: str) -> io.BytesIO:
    step = timedelta(hours=1) if unit == "hour" else timedelta(days=1)
    keys = list(msgs) + list(voice)
    xs = []
    if keys:
        cur, end = min(keys), max(keys)
        while cur <= end:
            xs.append(cur)
            cur += step
    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=110)
    fig.patch.set_facecolor("#2b2d31")
    ax.set_facecolor("#2b2d31")
    if not xs:
        ax.text(0.5, 0.5, "No data yet", ha="center", va="center", color="#b5bac1", transform=ax.transAxes)
        ax.axis("off")
    else:
        width = 0.03 if unit == "hour" else 0.8
        ax.bar(xs, [msgs.get(x, 0) for x in xs], width=width, color="#5865F2", label="Messages")
        ax.set_ylabel("Messages", color="#b5bac1")
        ax2 = ax.twinx()
        ax2.plot(xs, [voice.get(x, 0) for x in xs], color="#57F287", linewidth=2, label="Voice (h)")
        ax2.set_ylabel("Voice hours", color="#b5bac1")
        for a in (ax, ax2):
            a.tick_params(colors="#b5bac1")
            for sp in a.spines.values():
                sp.set_color("#3f4147")
        ax.set_title(title, color="#ffffff", fontsize=11)
        fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pool: Optional[asyncpg.Pool] = None
        self._own_pool = False
        self.msg_buf = defaultdict(int)    # (guild, channel, user, hour) -> count
        self.voice_buf = defaultdict(int)  # (guild, channel, user, hour, state) -> seconds
        self.sessions = {}                 # (guild, user) -> [channel_id, state, since]

    # ------------------------------------------------------------------ lifecycle
    async def cog_load(self):
        pool = getattr(getattr(self.bot, "db", None), "pool", None)
        if pool is None:
            pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=3)
            self._own_pool = True
        self.pool = pool
        async with self.pool.acquire() as c:
            await c.execute(SCHEMA)
        self.flush_loop.start()
        self.dock_loop.start()
        self.role_loop.start()

    async def cog_unload(self):
        self.flush_loop.cancel()
        self.dock_loop.cancel()
        self.role_loop.cancel()
        if not self.pool.is_closing():
            await self.flush()
        if self._own_pool and self.pool:
            await self.pool.close()

    # ------------------------------------------------------------------ tracking
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        ch = getattr(message.channel, "parent_id", None) or message.channel.id
        self.msg_buf[(message.guild.id, ch, message.author.id, hour_now())] += 1

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        key = (member.guild.id, member.id)
        self._credit(key)
        if after.channel is None:
            self.sessions.pop(key, None)
        else:
            self.sessions[key] = [after.channel.id, voice_state(after, member.guild), now_utc()]

    def _credit(self, key):
        s = self.sessions.get(key)
        if not s:
            return
        now = now_utc()
        elapsed = int((now - s[2]).total_seconds())
        if elapsed > 0:
            self.voice_buf[(key[0], s[0], key[1], hour_now(), s[1])] += elapsed
        s[2] = now

    def _snapshot_voice(self):
        for guild in self.bot.guilds:
            for uid, vs in guild.voice_states.items():
                m = guild.get_member(uid)
                if vs.channel and not (m and m.bot):
                    self.sessions[(guild.id, uid)] = [vs.channel.id, voice_state(vs, guild), now_utc()]

    async def flush(self):
        for key in list(self.sessions):
            self._credit(key)
        msgs, self.msg_buf = self.msg_buf, defaultdict(int)
        voice, self.voice_buf = self.voice_buf, defaultdict(int)
        if not msgs and not voice:
            return
        try:
            async with self.pool.acquire() as c:
                if msgs:
                    await c.executemany(
                        "INSERT INTO stat_messages VALUES ($1,$2,$3,$4,$5) "
                        "ON CONFLICT (guild_id, channel_id, user_id, hour) "
                        "DO UPDATE SET count = stat_messages.count + EXCLUDED.count",
                        [(*k, v) for k, v in msgs.items()],
                    )
                if voice:
                    await c.executemany(
                        "INSERT INTO stat_voice VALUES ($1,$2,$3,$4,$5,$6) "
                        "ON CONFLICT (guild_id, channel_id, user_id, hour, state) "
                        "DO UPDATE SET seconds = stat_voice.seconds + EXCLUDED.seconds",
                        [(*k, v) for k, v in voice.items()],
                    )
        except Exception:
            log.exception("stats flush failed; keeping data for next run")
            for k, v in msgs.items():
                self.msg_buf[k] += v
            for k, v in voice.items():
                self.voice_buf[k] += v

    @tasks.loop(seconds=60)
    async def flush_loop(self):
        await self.flush()

    @flush_loop.before_loop
    async def _before_flush(self):
        await self.bot.wait_until_ready()
        self._snapshot_voice()

    # ------------------------------------------------------------------ queries
    async def totals(self, gid, since, uid=None, cid=None):
        async with self.pool.acquire() as c:
            msgs = await c.fetchval(f"SELECT COALESCE(SUM(count),0) FROM stat_messages WHERE {W}", gid, since, uid, cid)
            rows = await c.fetch(
                f"SELECT state, COALESCE(SUM(seconds),0) s FROM stat_voice WHERE {W} GROUP BY state",
                gid, since, uid, cid,
            )
        return int(msgs), {r["state"]: int(r["s"]) for r in rows}

    async def series(self, gid, since, uid, cid, unit):
        trunc = f"date_trunc('{unit}', hour AT TIME ZONE 'UTC')"
        async with self.pool.acquire() as c:
            m = await c.fetch(f"SELECT {trunc} t, SUM(count) v FROM stat_messages WHERE {W} GROUP BY 1", gid, since, uid, cid)
            v = await c.fetch(
                f"SELECT {trunc} t, SUM(seconds)/3600.0 v FROM stat_voice WHERE {W} AND state <> 'afk' GROUP BY 1",
                gid, since, uid, cid,
            )
        return {r["t"]: int(r["v"]) for r in m}, {r["t"]: float(r["v"]) for r in v}

    async def rank(self, gid, since, uid, metric):
        table, col, extra = METRICS[metric]
        q = (
            f"WITH t AS (SELECT user_id, SUM({col}) s FROM {table} "
            f"WHERE guild_id = $1 AND ($2::timestamptz IS NULL OR hour >= $2){extra} GROUP BY user_id) "
            "SELECT (SELECT s FROM t WHERE user_id = $3) mine, "
            "(SELECT COUNT(*) FROM t WHERE s > (SELECT s FROM t WHERE user_id = $3)) + 1 rnk, "
            "(SELECT COUNT(*) FROM t) total"
        )
        async with self.pool.acquire() as c:
            r = await c.fetchrow(q, gid, since, uid)
        return (int(r["rnk"]), int(r["total"])) if r and r["mine"] else None

    # ------------------------------------------------------------------ /stats
    stats = app_commands.Group(
        name="stats", description="Message and voice analytics",
        guild_only=True, allowed_installs=INSTALLS,
    )

    async def _overview(self, itx, title, days, uid=None, cid=None, thumb=None):
        await itx.response.defer()
        since = since_for(days)
        msgs, voice = await self.totals(itx.guild_id, since, uid, cid)
        unit = "hour" if days == 1 else "day"
        ms, vs = await self.series(itx.guild_id, since, uid, cid, unit)
        chart = await asyncio.to_thread(make_chart, title, ms, vs, unit)
        active = sum(s for st, s in voice.items() if st != "afk")
        e = discord.Embed(title=title, description=f"**{PERIOD_LABEL[days]}**", color=0x5865F2)
        e.add_field(name="💬 Messages", value=f"{msgs:,}")
        e.add_field(name="🎙️ Voice", value=fmt_secs(active))
        extras = [f"{k.title()}: {fmt_secs(v)}" for k, v in voice.items() if k != "active" and v]
        if extras:
            e.add_field(name="Voice breakdown", value=" • ".join(extras), inline=False)
        if uid:
            parts = []
            for metric, label in (("messages", "Messages"), ("voice", "Voice")):
                r = await self.rank(itx.guild_id, since, uid, metric)
                if r:
                    parts.append(f"{label} #{r[0]} of {r[1]}")
            if parts:
                e.add_field(name="🏆 Rank", value=" • ".join(parts), inline=False)
        if thumb:
            e.set_thumbnail(url=thumb)
        e.set_image(url="attachment://chart.png")
        e.set_footer(text="Tracking started when the bot joined; no history before that.")
        await itx.followup.send(embed=e, file=discord.File(chart, "chart.png"))

    @stats.command(name="me", description="Your message and voice stats")
    @app_commands.choices(period=PERIODS)
    async def stats_me(self, itx: discord.Interaction, period: Optional[Choice[int]] = None):
        await self._overview(itx, f"{itx.user.display_name}'s stats", period.value if period else 7,
                             uid=itx.user.id, thumb=itx.user.display_avatar.url)

    @stats.command(name="user", description="Stats for a member")
    @app_commands.choices(period=PERIODS)
    async def stats_user(self, itx: discord.Interaction, member: discord.Member,
                         period: Optional[Choice[int]] = None):
        await self._overview(itx, f"{member.display_name}'s stats", period.value if period else 7,
                             uid=member.id, thumb=member.display_avatar.url)

    @stats.command(name="server", description="Server-wide stats")
    @app_commands.choices(period=PERIODS)
    async def stats_server(self, itx: discord.Interaction, period: Optional[Choice[int]] = None):
        g = itx.guild
        await self._overview(itx, f"{g.name} stats", period.value if period else 7,
                             thumb=g.icon.url if g.icon else None)

    @stats.command(name="channel", description="Stats for a channel")
    @app_commands.choices(period=PERIODS)
    async def stats_channel(self, itx: discord.Interaction, channel: discord.abc.GuildChannel,
                            period: Optional[Choice[int]] = None):
        await self._overview(itx, f"#{channel.name} stats", period.value if period else 7, cid=channel.id)

    # ------------------------------------------------------------------ /top
    @app_commands.command(name="top", description="Leaderboards for messages or voice time")
    @app_commands.choices(
        metric=[Choice(name="Messages", value="messages"), Choice(name="Voice", value="voice")],
        by=[Choice(name="Members", value="user_id"), Choice(name="Channels", value="channel_id")],
        period=PERIODS,
    )
    @guild_only_cmd
    async def top(self, itx: discord.Interaction, metric: Choice[str],
                  by: Optional[Choice[str]] = None, period: Optional[Choice[int]] = None):
        days = period.value if period else 7
        group_col = by.value if by else "user_id"
        table, col, extra = METRICS[metric.value]
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                f"SELECT {group_col} k, SUM({col}) v FROM {table} WHERE {W}{extra} "
                "GROUP BY 1 ORDER BY v DESC LIMIT 10",
                itx.guild_id, since_for(days), None, None,
            )
        if not rows:
            return await itx.response.send_message("No data yet for that period.", ephemeral=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            tag = f"<@{r['k']}>" if group_col == "user_id" else f"<#{r['k']}>"
            val = f"{int(r['v']):,}" if metric.value == "messages" else fmt_secs(r["v"])
            lines.append(f"{medals[i] if i < 3 else f'`{i + 1}.`'} {tag} — {val}")
        e = discord.Embed(
            title=f"Top {metric.name.lower()} by {'member' if group_col == 'user_id' else 'channel'}",
            description="\n".join(lines), color=0x5865F2,
        )
        e.set_footer(text=PERIOD_LABEL[days])
        await itx.response.send_message(embed=e)

    # ------------------------------------------------------------------ Statdocks
    dock = app_commands.Group(
        name="statdock", description="Channel-name counters",
        guild_only=True, allowed_installs=INSTALLS,
        default_permissions=discord.Permissions(manage_channels=True),
    )

    async def _render(self, tpl: str, guild: discord.Guild) -> str:
        now = now_utc()
        async with self.pool.acquire() as c:
            today = await c.fetchval("SELECT COALESCE(SUM(count),0) FROM stat_messages WHERE guild_id=$1 AND hour >= $2",
                                     guild.id, now - timedelta(hours=24))
            total = await c.fetchval("SELECT COALESCE(SUM(count),0) FROM stat_messages WHERE guild_id=$1", guild.id)
        data = _Safe(
            members=guild.member_count or 0, channels=len(guild.channels), roles=len(guild.roles),
            boosts=guild.premium_subscription_count or 0, messages_today=int(today), messages_total=int(total),
            voice_now=sum(1 for k in self.sessions if k[0] == guild.id),
            date=now.strftime("%b %d"), time=now.strftime("%H:%M") + " UTC",
        )
        try:
            return tpl.format_map(data)[:100]
        except (ValueError, KeyError, IndexError):
            return tpl[:100]

    @dock.command(name="add", description="Turn a voice channel's name into a live counter")
    @app_commands.describe(
        channel="Channel to rename",
        template="Tokens: {members} {channels} {roles} {boosts} {messages_today} {messages_total} {voice_now} {date} {time}",
    )
    async def dock_add(self, itx: discord.Interaction, channel: discord.VoiceChannel, template: str):
        await itx.response.defer(ephemeral=True)
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO stat_docks (guild_id, channel_id, template) VALUES ($1,$2,$3) "
                "ON CONFLICT (channel_id) DO UPDATE SET template = EXCLUDED.template",
                itx.guild_id, channel.id, template,
            )
        try:
            await channel.edit(name=await self._render(template, itx.guild), reason="Statdock")
        except discord.Forbidden:
            return await itx.followup.send("Saved, but I need **Manage Channels** on that channel to rename it.")
        await itx.followup.send(f"Statdock set on {channel.mention}. It refreshes every 10 minutes.")

    @dock.command(name="remove", description="Stop updating a channel")
    async def dock_remove(self, itx: discord.Interaction, channel: discord.VoiceChannel):
        async with self.pool.acquire() as c:
            res = await c.execute("DELETE FROM stat_docks WHERE guild_id=$1 AND channel_id=$2", itx.guild_id, channel.id)
        await itx.response.send_message("Removed." if res.endswith("1") else "That channel isn't a statdock.", ephemeral=True)

    @dock.command(name="list", description="Show this server's statdocks")
    async def dock_list(self, itx: discord.Interaction):
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT channel_id, template FROM stat_docks WHERE guild_id=$1", itx.guild_id)
        text = "\n".join(f"<#{r['channel_id']}> → `{r['template']}`" for r in rows) or "No statdocks yet."
        await itx.response.send_message(text, ephemeral=True)

    @tasks.loop(minutes=10)
    async def dock_loop(self):
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT guild_id, channel_id, template FROM stat_docks")
        for r in rows:
            try:
                guild = self.bot.get_guild(r["guild_id"])
                ch = guild.get_channel(r["channel_id"]) if guild else None
                if not ch:
                    continue
                name = await self._render(r["template"], guild)
                if ch.name != name:
                    await ch.edit(name=name, reason="Statdock")
            except discord.HTTPException:
                log.warning("statdock update failed for %s", r["channel_id"])
            except Exception:
                log.exception("statdock error")

    @dock_loop.before_loop
    async def _before_dock(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ Statroles
    role = app_commands.Group(
        name="statrole", description="Give roles based on stats",
        guild_only=True, allowed_installs=INSTALLS,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    @role.command(name="add", description="Give a role to members who reach a stat (and remove it if they drop below)")
    @app_commands.choices(metric=[Choice(name="Messages", value="messages"), Choice(name="Voice hours", value="voice")])
    @app_commands.describe(minimum="Messages, or hours of voice", days="Look-back window in days (0 = all time)")
    async def role_add(self, itx: discord.Interaction, role: discord.Role, metric: Choice[str],
                       minimum: app_commands.Range[int, 1, 1_000_000], days: app_commands.Range[int, 0, 365] = 30):
        if role.is_default() or role.managed or role >= itx.guild.me.top_role:
            return await itx.response.send_message(
                "I can't manage that role. Pick a normal role that sits **below** my highest role.", ephemeral=True)
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO stat_roles (guild_id, role_id, metric, minimum, period_days) VALUES ($1,$2,$3,$4,$5) "
                "ON CONFLICT (role_id) DO UPDATE SET metric=EXCLUDED.metric, minimum=EXCLUDED.minimum, "
                "period_days=EXCLUDED.period_days",
                itx.guild_id, role.id, metric.value, minimum, days,
            )
        window = "all time" if days == 0 else f"the last {days} days"
        await itx.response.send_message(
            f"{role.mention} will go to members with **{minimum:,} {metric.name.lower()}** over {window}. Syncs every 15 minutes.",
            ephemeral=True)

    @role.command(name="remove", description="Delete a statrole rule")
    async def role_remove(self, itx: discord.Interaction, role: discord.Role):
        async with self.pool.acquire() as c:
            res = await c.execute("DELETE FROM stat_roles WHERE guild_id=$1 AND role_id=$2", itx.guild_id, role.id)
            await c.execute("DELETE FROM stat_role_members WHERE role_id=$1", role.id)
        await itx.response.send_message("Removed." if res.endswith("1") else "No rule for that role.", ephemeral=True)

    @role.command(name="list", description="Show statrole rules")
    async def role_list(self, itx: discord.Interaction):
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT role_id, metric, minimum, period_days FROM stat_roles WHERE guild_id=$1", itx.guild_id)
        text = "\n".join(
            f"<@&{r['role_id']}> → {r['minimum']:,} {r['metric']} / "
            f"{'all time' if r['period_days'] == 0 else str(r['period_days']) + 'd'}" for r in rows
        ) or "No statroles yet."
        await itx.response.send_message(text, ephemeral=True)

    async def _qualifying(self, r) -> set:
        table, col, extra = METRICS[r["metric"]]
        need = r["minimum"] * (3600 if r["metric"] == "voice" else 1)
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                f"SELECT user_id FROM {table} WHERE guild_id=$1 AND ($2::timestamptz IS NULL OR hour >= $2){extra} "
                f"GROUP BY user_id HAVING SUM({col}) >= $3",
                r["guild_id"], since_for(r["period_days"]), need,
            )
        return {x["user_id"] for x in rows}

    async def _member(self, guild, uid):
        m = guild.get_member(uid)
        if m:
            return m
        try:
            return await guild.fetch_member(uid)
        except discord.NotFound:
            return None

    @tasks.loop(minutes=15)
    async def role_loop(self):
        async with self.pool.acquire() as c:
            rules = await c.fetch("SELECT * FROM stat_roles")
        for r in rules:
            try:
                guild = self.bot.get_guild(r["guild_id"])
                role = guild.get_role(r["role_id"]) if guild else None
                if not role:
                    continue
                qualified = await self._qualifying(r)
                async with self.pool.acquire() as c:
                    assigned = {x["user_id"] for x in await c.fetch(
                        "SELECT user_id FROM stat_role_members WHERE role_id=$1", role.id)}
                for uid in qualified - assigned:
                    m = await self._member(guild, uid)
                    if m:
                        await m.add_roles(role, reason="Statrole")
                        async with self.pool.acquire() as c:
                            await c.execute("INSERT INTO stat_role_members VALUES ($1,$2) ON CONFLICT DO NOTHING", role.id, uid)
                        await asyncio.sleep(0.5)
                for uid in assigned - qualified:
                    m = await self._member(guild, uid)
                    if m:
                        await m.remove_roles(role, reason="Statrole")
                        await asyncio.sleep(0.5)
                    async with self.pool.acquire() as c:
                        await c.execute("DELETE FROM stat_role_members WHERE role_id=$1 AND user_id=$2", role.id, uid)
            except discord.HTTPException:
                log.warning("statrole sync failed for role %s", r["role_id"])
            except Exception:
                log.exception("statrole error")

    @role_loop.before_loop
    async def _before_role(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Stats(bot))
