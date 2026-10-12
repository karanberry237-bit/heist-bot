import asyncio
import random
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import embed, error_embed, everywhere, money

DAILY_BASE = 500
DAILY_STREAK_BONUS = 50
DAILY_STREAK_CAP = 30
DAILY_COOLDOWN = 24 * 3600

WORK_COOLDOWN = 3600
WORK_RANGE = (100, 400)
WORK_JOBS = [
    "delivered pizzas on a scooter",
    "walked a very stubborn dog",
    "fixed a stranger's WiFi",
    "streamed to 3 viewers (2 were bots)",
    "washed cars downtown",
    "debugged a spreadsheet",
    "DJ'd a wedding",
    "sold lemonade at a heated markup",
    "tutored a kid in math",
    "repaired a vending machine",
]


def ts(seconds_from_now: float) -> str:
    return f"<t:{int(time.time() + seconds_from_now)}:R>"


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self._name_cache: dict[int, str] = {}

    # ---------- balance ----------

    @commands.hybrid_command(name="balance", aliases=["bal"], description="Check your balance or someone else's")
    @everywhere
    @app_commands.describe(user="Whose balance to check (default: you)")
    async def balance(self, ctx: commands.Context, user: Optional[discord.User] = None):
        target = user or ctx.author
        if target.bot:
            return await ctx.send(embed=error_embed("Bots don't have wallets."), ephemeral=True)
        bal = await self.db.get_balance(target.id)
        rank = await self.db.rank_of(target.id)
        e = embed(f"{target.display_name}'s wallet", f"Balance: {money(bal)}\nGlobal rank: **#{rank:,}**")
        e.set_thumbnail(url=target.display_avatar.url)
        await ctx.send(embed=e)

    # ---------- daily ----------

    @commands.hybrid_command(name="daily", description="Claim your daily reward (streaks give bonuses)")
    @everywhere
    async def daily(self, ctx: commands.Context):
        uid = ctx.author.id
        remaining = await self.db.claim_cooldown(uid, "daily", DAILY_COOLDOWN)
        if remaining > 0:
            return await ctx.send(
                embed=error_embed(f"You already claimed today. Come back {ts(remaining)}."), ephemeral=True
            )
        streak = await self.db.bump_daily_streak(uid)
        bonus = min(streak - 1, DAILY_STREAK_CAP) * DAILY_STREAK_BONUS
        reward = DAILY_BASE + bonus
        new_bal = await self.db.change_balance(uid, reward)
        e = embed("Daily reward claimed", f"You received {money(reward)}")
        e.add_field(name="Streak", value=f"🔥 {streak} day{'s' if streak != 1 else ''}")
        e.add_field(name="Balance", value=money(new_bal))
        e.set_footer(text="Claim again within 48h to keep your streak.")
        await ctx.send(embed=e)

    # ---------- work ----------

    @commands.hybrid_command(name="work", description="Work a shift for some quick cash")
    @everywhere
    async def work(self, ctx: commands.Context):
        uid = ctx.author.id
        remaining = await self.db.claim_cooldown(uid, "work", WORK_COOLDOWN)
        if remaining > 0:
            return await ctx.send(
                embed=error_embed(f"You're tired. You can work again {ts(remaining)}."), ephemeral=True
            )
        earned = random.randint(*WORK_RANGE)
        new_bal = await self.db.change_balance(uid, earned)
        job = random.choice(WORK_JOBS)
        e = embed("Shift complete", f"You {job} and earned {money(earned)}.")
        e.set_footer(text=f"Balance: {new_bal:,}")
        await ctx.send(embed=e)

    # ---------- pay ----------

    @commands.hybrid_command(name="pay", description="Send money to another user")
    @everywhere
    @app_commands.describe(user="Who to pay", amount="How much to send")
    async def pay(
        self,
        ctx: commands.Context,
        user: discord.User,
        amount: app_commands.Range[int, 1, 1_000_000_000],
    ):
        if user.bot:
            return await ctx.send(embed=error_embed("You can't pay a bot."), ephemeral=True)
        if user.id == ctx.author.id:
            return await ctx.send(embed=error_embed("You can't pay yourself."), ephemeral=True)
        ok = await self.db.transfer(ctx.author.id, user.id, amount)
        if not ok:
            return await ctx.send(embed=error_embed("You don't have enough money for that."), ephemeral=True)
        await ctx.send(embed=embed("Payment sent", f"{ctx.author.mention} paid {user.mention} {money(amount)}"))

    # ---------- leaderboard ----------

    async def _name(self, user_id: int) -> str:
        if user_id in self._name_cache:
            return self._name_cache[user_id]
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return f"User {user_id}"
        self._name_cache[user_id] = user.display_name
        return user.display_name

    @commands.hybrid_command(
        name="leaderboard", aliases=["lb", "top"], description="Richest users across the whole bot"
    )
    @everywhere
    async def leaderboard(self, ctx: commands.Context):
        await ctx.defer()
        rows = await self.db.top_balances(10)
        if not rows:
            return await ctx.send(embed=embed("Leaderboard", "Nobody has any money yet."))
        names = await asyncio.gather(*(self._name(r["user_id"]) for r in rows))
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'`#{i + 1}`'} **{name}** — {money(r['balance'])}"
            for i, (r, name) in enumerate(zip(rows, names))
        ]
        await ctx.send(embed=embed("Global leaderboard", "\n".join(lines)))


async def setup(bot):
    await bot.add_cog(Economy(bot))
