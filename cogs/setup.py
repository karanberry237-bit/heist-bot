"""/setup creates the levels and mod-logs channels, and the bot logs basic
moderation events (deleted/edited messages, bans, unbans) to the mod-logs channel.

Other cogs (e.g. your moderation cog) can post to the mod-logs channel with:
    await self.bot.send_modlog(guild, embed)
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_channels (
    guild_id BIGINT PRIMARY KEY, levels_channel_id BIGINT, modlog_channel_id BIGINT);
"""


def guild_only_cmd(f):
    f = app_commands.allowed_installs(guilds=True, users=False)(f)
    return app_commands.guild_only()(f)


class ServerSetup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cache = {}  # guild -> mod-log channel id (or None)

    async def cog_load(self):
        async with self.bot.db.pool.acquire() as c:
            await c.execute(SCHEMA)
        self.bot.send_modlog = self.send_modlog

    async def cog_unload(self):
        self.bot.__dict__.pop("send_modlog", None)

    # ------------------------------------------------------------------ mod log
    async def modlog_channel(self, guild):
        if guild.id not in self.cache:
            self.cache[guild.id] = await self.bot.db.pool.fetchval(
                "SELECT modlog_channel_id FROM guild_channels WHERE guild_id=$1", guild.id)
        cid = self.cache[guild.id]
        ch = guild.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def send_modlog(self, guild: discord.Guild, embed: discord.Embed):
        ch = await self.modlog_channel(guild)
        if not ch:
            return
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            log.warning("couldn't post to mod-logs in guild %s", guild.id)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        ch = await self.modlog_channel(message.guild)
        if not ch or message.channel.id == ch.id:
            return
        e = discord.Embed(title="Message deleted", color=0xED4245, timestamp=discord.utils.utcnow(),
                          description=(message.content or "*no text (attachment or embed)*")[:1000])
        e.add_field(name="Author", value=f"{message.author.mention} ({message.author.id})", inline=False)
        e.add_field(name="Channel", value=message.channel.mention)
        await self.send_modlog(message.guild, e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.guild is None or after.author.bot or before.content == after.content:
            return
        e = discord.Embed(title="Message edited", color=0xFEE75C, timestamp=discord.utils.utcnow(),
                          url=after.jump_url)
        e.add_field(name="Author", value=f"{after.author.mention} ({after.author.id})", inline=False)
        e.add_field(name="Channel", value=after.channel.mention, inline=False)
        e.add_field(name="Before", value=(before.content or "*empty*")[:500], inline=False)
        e.add_field(name="After", value=(after.content or "*empty*")[:500], inline=False)
        await self.send_modlog(after.guild, e)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        e = discord.Embed(title="Member banned", color=0xED4245, timestamp=discord.utils.utcnow(),
                          description=f"{user.mention} ({user})  •  ID {user.id}")
        await self.send_modlog(guild, e)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        e = discord.Embed(title="Member unbanned", color=0x57F287, timestamp=discord.utils.utcnow(),
                          description=f"{user.mention} ({user})  •  ID {user.id}")
        await self.send_modlog(guild, e)

    # ------------------------------------------------------------------ /setup
    @app_commands.command(name="setup", description="Create the levels and mod-logs channels")
    @app_commands.default_permissions(administrator=True)
    @guild_only_cmd
    async def setup_cmd(self, itx: discord.Interaction):
        guild = itx.guild
        if not itx.user.guild_permissions.administrator:
            return await itx.response.send_message("Only administrators can run this.", ephemeral=True)
        if not guild.me.guild_permissions.manage_channels:
            return await itx.response.send_message(
                "I need the **Manage Channels** permission first.", ephemeral=True)
        await itx.response.defer(ephemeral=True)

        pool = self.bot.db.pool
        row = await pool.fetchrow(
            "SELECT levels_channel_id, modlog_channel_id FROM guild_channels WHERE guild_id=$1", guild.id)
        levels = guild.get_channel(row["levels_channel_id"]) if row and row["levels_channel_id"] else None
        modlog = guild.get_channel(row["modlog_channel_id"]) if row and row["modlog_channel_id"] else None
        made = []
        bot_perms = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                embed_links=True, read_message_history=True)
        try:
            if not isinstance(levels, discord.TextChannel):
                levels = await guild.create_text_channel(
                    "levels", topic="Level-up announcements",
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(
                            send_messages=False, add_reactions=False, create_public_threads=False),
                        guild.me: bot_perms,
                    }, reason="/setup")
                made.append("levels")
            if not isinstance(modlog, discord.TextChannel):
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    guild.me: bot_perms,
                }
                mod_role_id = await self.bot.db.get_mod_role(guild.id)
                mod_role = guild.get_role(mod_role_id) if mod_role_id else None
                if mod_role:
                    overwrites[mod_role] = discord.PermissionOverwrite(
                        view_channel=True, read_message_history=True)
                modlog = await guild.create_text_channel(
                    "mod-logs", topic="Moderation logs (staff only)", overwrites=overwrites, reason="/setup")
                made.append("mod-logs")
        except discord.Forbidden:
            return await itx.followup.send("I couldn't create channels. Check my permissions.")

        await pool.execute(
            "INSERT INTO guild_channels (guild_id, levels_channel_id, modlog_channel_id) VALUES ($1,$2,$3) "
            "ON CONFLICT (guild_id) DO UPDATE SET levels_channel_id = EXCLUDED.levels_channel_id, "
            "modlog_channel_id = EXCLUDED.modlog_channel_id",
            guild.id, levels.id, modlog.id)
        self.cache.pop(guild.id, None)
        lv = self.bot.get_cog("Leveling")
        if lv:
            lv.chan_cache.pop(guild.id, None)

        e = discord.Embed(title="Setup complete", color=0x57F287)
        e.add_field(name="Levels", value=f"{levels.mention}{' (new)' if 'levels' in made else ''}")
        e.add_field(name="Mod logs", value=f"{modlog.mention}{' (new)' if 'mod-logs' in made else ''}")
        if "mod-logs" in made and not await self.bot.db.get_mod_role(guild.id):
            e.set_footer(text="Only admins can see mod-logs. Set a mod role, then give it access to the channel.")
        await itx.followup.send(embed=e)


async def setup(bot):
    await bot.add_cog(ServerSetup(bot))
