"""Help menu that works as both a prefix command and a slash command.

It builds itself from whatever cogs are loaded (prefix, hybrid and slash
commands), so new commands show up without editing this file.
"""
import asyncio
import io
import logging
import math
import os
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from rankcard import render_help_banner

log = logging.getLogger(__name__)

PER_PAGE = 6
# module name -> (emoji, label, colour)
CATS = {
    "economy": ("💰", "Economy", 0xF5B301),
    "games": ("🎲", "Games", 0xEB459E),
    "leveling": ("⭐", "Leveling", 0x9B59B6),
    "stats": ("📊", "Statistics", 0x3BA55D),
    "moderation": ("🛡️", "Moderation", 0xED4245),
    "setup": ("⚙️", "Setup", 0x5865F2),
    "help": ("✨", "General", 0x8B5CF6),
}
ORDER = list(CATS)
HOME_COLOR = 0x8B5CF6


def _locked(app_cmd) -> bool:
    while app_cmd is not None:
        if getattr(app_cmd, "default_permissions", None) is not None:
            return True
        app_cmd = getattr(app_cmd, "parent", None)
    return False


class HelpView(discord.ui.View):
    def __init__(self, cog, owner: discord.abc.User, data: dict):
        super().__init__(timeout=180)
        self.cog, self.owner, self.data = cog, owner, data
        self.key, self.page = "home", 0
        self.message: Optional[discord.Message] = None
        options = [discord.SelectOption(label="Home", value="home", emoji="🏠", description="Overview of everything")]
        for key, c in data["cats"].items():
            options.append(discord.SelectOption(
                label=c["label"], value=key, emoji=c["emoji"], description=f"{len(c['entries'])} commands"))
        self.select = discord.ui.Select(placeholder="✨ Browse a category…", options=options, row=0)
        self.select.callback = self.on_select
        self.add_item(self.select)
        self._sync()

    def _pages(self) -> int:
        if self.key == "home":
            return 1
        return max(1, math.ceil(len(self.data["cats"][self.key]["entries"]) / PER_PAGE))

    def _sync(self):
        for o in self.select.options:
            o.default = o.value == self.key
        self.go_prev.disabled = self.key == "home" or self.page <= 0
        self.go_next.disabled = self.key == "home" or self.page >= self._pages() - 1
        self.go_home.disabled = self.key == "home"

    def embed(self) -> discord.Embed:
        if self.key == "home":
            return self.cog.home_embed(self.data, self.owner)
        return self.cog.category_embed(self.data, self.key, self.page, self.owner)

    async def _render(self, itx: discord.Interaction):
        self._sync()
        await itx.response.edit_message(embed=self.embed(), view=self)

    async def interaction_check(self, itx: discord.Interaction) -> bool:
        if itx.user.id != self.owner.id:
            await itx.response.send_message("Run the help command yourself to get your own menu.", ephemeral=True)
            return False
        return True

    async def on_select(self, itx: discord.Interaction):
        self.key, self.page = self.select.values[0], 0
        await self._render(itx)

    @discord.ui.button(label="Prev", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def go_prev(self, itx: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._render(itx)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def go_next(self, itx: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._render(itx)

    @discord.ui.button(label="Home", emoji="🏠", style=discord.ButtonStyle.primary, row=1)
    async def go_home(self, itx: discord.Interaction, button: discord.ui.Button):
        self.key, self.page = "home", 0
        await self._render(itx)

    @discord.ui.button(label="Close", emoji="✖️", style=discord.ButtonStyle.danger, row=1)
    async def close(self, itx: discord.Interaction, button: discord.ui.Button):
        await itx.response.edit_message(view=None)
        self.stop()

    async def on_timeout(self):
        if not self.message:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.prefix = os.getenv("PREFIX", ",sonl")
        self._ids = {}       # top-level slash command name -> id (for clickable mentions)
        self._ids_at = 0.0
        self._banners = {}   # subtitle -> png bytes

    # ------------------------------------------------------------------ data
    def collect(self) -> dict:
        buckets = {}
        for cog in self.bot.cogs.values():
            key = cog.__module__.rsplit(".", 1)[-1]
            entries = {}
            for cmd in cog.walk_commands():
                if cmd.hidden or isinstance(cmd, commands.Group):
                    continue
                hybrid = isinstance(cmd, commands.HybridCommand)
                entries[cmd.qualified_name] = {
                    "name": cmd.qualified_name,
                    "desc": cmd.description or cmd.short_doc or "No description",
                    "params": " ".join(f"<{p.name}>" if p.required else f"[{p.name}]"
                                       for p in cmd.clean_params.values()),
                    "slash": hybrid, "prefix": True,
                    "lock": _locked(cmd.app_command) if hybrid else False,
                }
            for cmd in cog.walk_app_commands():
                if isinstance(cmd, app_commands.Group) or cmd.qualified_name in entries:
                    continue
                entries[cmd.qualified_name] = {
                    "name": cmd.qualified_name,
                    "desc": cmd.description or "No description",
                    "params": " ".join(f"<{p.name}>" if p.required else f"[{p.name}]" for p in cmd.parameters),
                    "slash": True, "prefix": False, "lock": _locked(cmd),
                }
            if entries:
                bucket = buckets.setdefault(key, [])
                bucket.extend(sorted(entries.values(), key=lambda e: e["name"]))
        cats = {}
        for key in sorted(buckets, key=lambda k: (ORDER.index(k) if k in ORDER else 99, k)):
            emoji, label, color = CATS.get(key, ("🧩", key.replace("_", " ").title(), HOME_COLOR))
            cats[key] = {"emoji": emoji, "label": label, "color": color, "entries": buckets[key]}
        return {"cats": cats, "total": sum(len(c["entries"]) for c in cats.values())}

    async def _load_ids(self, guild):
        if self._ids and time.monotonic() - self._ids_at < 600:
            return
        try:
            cmds = await self.bot.tree.fetch_commands()
            if not cmds and guild:
                cmds = await self.bot.tree.fetch_commands(guild=guild)
            self._ids = {c.name: c.id for c in cmds}
            self._ids_at = time.monotonic()
        except discord.HTTPException:
            pass

    async def build_data(self, guild) -> dict:
        await self._load_ids(guild)
        return self.collect()

    async def banner(self, data) -> discord.File:
        subtitle = " • ".join(c["label"] for c in data["cats"].values())
        if subtitle not in self._banners:
            try:
                avatar = await self.bot.user.display_avatar.replace(size=256, format="png").read()
            except discord.HTTPException:
                avatar = None
            buf = await asyncio.to_thread(render_help_banner, self.bot.user.name, subtitle, avatar)
            self._banners[subtitle] = buf.getvalue()
        return discord.File(io.BytesIO(self._banners[subtitle]), "help.png")

    # ------------------------------------------------------------------ embeds
    def fmt(self, e: dict) -> str:
        if e["slash"]:
            cid = self._ids.get(e["name"].split()[0])
            head = f"</{e['name']}:{cid}>" if cid else f"`/{e['name']}`"
        else:
            head = f"`{self.prefix} {e['name']}`"
        params = f" `{e['params']}`" if e["params"] else ""
        also = f" · `{self.prefix} {e['name']}`" if e["slash"] and e["prefix"] else ""
        return f"{'🔒 ' if e['lock'] else ''}{head}{params}\n-# {e['desc']}{also}"

    def home_embed(self, data: dict, user) -> discord.Embed:
        bot = self.bot
        e = discord.Embed(color=HOME_COLOR, timestamp=discord.utils.utcnow(), description=(
            "**Everything this bot can do, in one place.**\n"
            "Pick a category from the menu below to browse its commands.\n"
            f"```\n🌐 {len(bot.guilds):,} servers   ⚡ {data['total']} commands   📡 {round(bot.latency * 1000)}ms\n```"))
        e.set_author(name=f"{bot.user.name} • Help", icon_url=bot.user.display_avatar.url)
        e.set_image(url="attachment://help.png")
        for key, c in data["cats"].items():
            names = list(dict.fromkeys(x["name"].split()[0] for x in c["entries"]))
            shown = " ".join(f"`{n}`" for n in names[:4]) + (" …" if len(names) > 4 else "")
            e.add_field(name=f"{c['emoji']} {c['label']}", value=f"{shown}\n-# {len(c['entries'])} commands")
        e.set_footer(text=f"Slash: /   •   Prefix: {self.prefix}   •   🔒 = staff only",
                     icon_url=user.display_avatar.url)
        return e

    def category_embed(self, data: dict, key: str, page: int, user) -> discord.Embed:
        c = data["cats"][key]
        entries = c["entries"]
        pages = max(1, math.ceil(len(entries) / PER_PAGE))
        chunk = entries[page * PER_PAGE:(page + 1) * PER_PAGE]
        e = discord.Embed(title=f"{c['emoji']}  {c['label']}", color=c["color"],
                          description="\n\n".join(self.fmt(x) for x in chunk))
        e.set_image(url="attachment://help.png")
        e.set_footer(text=f"Page {page + 1}/{pages}   •   {len(entries)} commands   •   🔒 = staff only",
                     icon_url=user.display_avatar.url)
        return e

    # ------------------------------------------------------------------ command
    @commands.hybrid_command(name="help", description="Browse every command")
    @app_commands.describe(category="Jump straight to a category")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def help_cmd(self, ctx: commands.Context, category: Optional[str] = None):
        await ctx.defer()
        data = await self.build_data(ctx.guild)
        view = HelpView(self, ctx.author, data)
        if category:
            want = category.lower().strip()
            for key, c in data["cats"].items():
                if want in (key, c["label"].lower()):
                    view.key = key
                    view._sync()
                    break
        view.message = await ctx.send(embed=view.embed(), file=await self.banner(data), view=view)

    @help_cmd.autocomplete("category")
    async def _category_autocomplete(self, itx: discord.Interaction, current: str):
        cats = self.collect()["cats"]
        return [app_commands.Choice(name=f"{c['emoji']} {c['label']}", value=key)
                for key, c in cats.items() if current.lower() in c["label"].lower() or current.lower() in key][:25]


async def setup(bot):
    await bot.add_cog(Help(bot))
