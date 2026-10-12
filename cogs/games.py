import asyncio
import random
import re

import discord
from discord import app_commands
from discord.ext import commands

from utils import COLOR, embed, error_embed, everywhere, money

MAX_BET = 500_000
GREEN, RED, GREY = 0x57F287, 0xED4245, 0x99AAB5


# ---------------------------------------------------------------- bet parsing

def parse_bet(raw: str, balance: int) -> int | None:
    """Accepts '500', '1,000', '5k', '1.5m', 'all', 'max', 'half'."""
    raw = raw.strip().lower().replace(",", "").replace("_", "")
    if raw in ("all", "max"):
        return min(balance, MAX_BET)
    if raw == "half":
        return min(balance // 2, MAX_BET)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", raw)
    if not m:
        return None
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    return int(float(m.group(1)) * mult)


# ---------------------------------------------------------------- slots

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 25, 20, 13, 8, 4]
SLOT_TRIPLE = {"🍒": 8, "🍋": 12, "🍇": 18, "🔔": 30, "💎": 60, "7️⃣": 150}
SLOT_TWO_CHERRIES = 1.5  # exactly two cherries


def slot_multiplier(reels: list[str]) -> float:
    if len(set(reels)) == 1:
        return SLOT_TRIPLE[reels[0]]
    if reels.count("🍒") == 2:
        return SLOT_TWO_CHERRIES
    return 0


# ---------------------------------------------------------------- blackjack

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]


def new_deck() -> list[tuple[int, str]]:
    deck = [(r, s) for r in range(13) for s in SUITS]
    random.shuffle(deck)
    return deck


def card_str(card) -> str:
    return f"`{RANKS[card[0]]}{card[1]}`"


def hand_value(cards) -> int:
    total = sum(min(r + 1, 10) for r, _ in cards)  # ace counts as 1 here
    if any(r == 0 for r, _ in cards) and total + 10 <= 21:
        total += 10
    return total


def is_blackjack(cards) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


class BlackjackView(discord.ui.View):
    def __init__(self, cog: "Games", interaction: discord.Interaction, bet: int):
        super().__init__(timeout=90)
        self.cog = cog
        self.db = cog.db
        self.interaction = interaction
        self.user = interaction.user
        self.bet = bet
        self.deck = new_deck()
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.can_double = True
        self.finished = False
        self.lock = asyncio.Lock()

    # ----- rendering

    def render(self, reveal: bool = False, *, description: str | None = None, color: int = COLOR) -> discord.Embed:
        e = embed("🃏 Blackjack", description, color=color)
        pv = hand_value(self.player)
        e.add_field(name=f"Your hand ({pv})", value=" ".join(card_str(c) for c in self.player), inline=False)
        if reveal:
            dv = hand_value(self.dealer)
            dealer_text = " ".join(card_str(c) for c in self.dealer)
            e.add_field(name=f"Dealer ({dv})", value=dealer_text, inline=False)
        else:
            e.add_field(name="Dealer", value=f"{card_str(self.dealer[0])} `?`", inline=False)
        e.set_footer(text=f"Bet: {self.bet:,}")
        return e

    # ----- game logic

    def initial_outcome(self) -> str | None:
        p, d = is_blackjack(self.player), is_blackjack(self.dealer)
        if p and d:
            return "push"
        if p:
            return "blackjack"
        if d:
            return "dealer_bj"
        return None

    def play_dealer(self):
        while hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())

    def compare(self) -> str:
        pv, dv = hand_value(self.player), hand_value(self.dealer)
        if dv > 21 or pv > dv:
            return "win"
        if pv == dv:
            return "push"
        return "lose"

    async def settle(self, outcome: str) -> discord.Embed:
        """Pays out exactly once and returns the final embed."""
        self.finished = True
        self.cog.active_blackjack.discard(self.user.id)
        bet = self.bet
        payout = {
            "blackjack": bet * 5 // 2,  # 3:2
            "win": bet * 2,
            "push": bet,
            "lose": 0,
            "bust": 0,
            "dealer_bj": 0,
        }[outcome]
        if payout:
            await self.db.change_balance(self.user.id, payout)
        balance = await self.db.get_balance(self.user.id)
        net = payout - bet
        text, color = {
            "blackjack": ("🎉 **Blackjack!**", GREEN),
            "win": ("✅ **You win!**", GREEN),
            "push": ("🤝 **Push.** Your bet is returned.", GREY),
            "lose": ("❌ **Dealer wins.**", RED),
            "bust": ("💥 **Bust!**", RED),
            "dealer_bj": ("❌ **Dealer has blackjack.**", RED),
        }[outcome]
        if net > 0:
            text += f"\nYou won {money(net)}"
        elif net < 0:
            text += f"\nYou lost {money(-net)}"
        text += f"\nBalance: {money(balance)}"
        return self.render(reveal=True, description=text, color=color)

    async def finish(self, interaction: discord.Interaction, outcome: str):
        e = await self.settle(outcome)
        self.stop()
        await interaction.response.edit_message(embed=e, view=None)

    async def dealer_turn(self, interaction: discord.Interaction):
        self.play_dealer()
        await self.finish(interaction, self.compare())

    # ----- auth

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                embed=error_embed("This isn't your game. Start your own with `/blackjack`."),
                ephemeral=True,
            )
            return False
        return True

    # ----- buttons

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary, emoji="🃏")
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.finished:
                return
            self.player.append(self.deck.pop())
            self.can_double = False
            self.double.disabled = True
            pv = hand_value(self.player)
            if pv > 21:
                return await self.finish(interaction, "bust")
            if pv == 21:
                return await self.dealer_turn(interaction)
            await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary, emoji="✋")
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.finished:
                return
            await self.dealer_turn(interaction)

    @discord.ui.button(label="Double", style=discord.ButtonStyle.success, emoji="💰")
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            if self.finished:
                return
            if not self.can_double:
                return await interaction.response.send_message(
                    embed=error_embed("You can only double on your first move."), ephemeral=True
                )
            if await self.db.change_balance(self.user.id, -self.bet) is None:
                return await interaction.response.send_message(
                    embed=error_embed("You don't have enough money to double."), ephemeral=True
                )
            self.bet *= 2
            self.player.append(self.deck.pop())
            if hand_value(self.player) > 21:
                return await self.finish(interaction, "bust")
            await self.dealer_turn(interaction)

    async def on_timeout(self):
        async with self.lock:
            if self.finished:
                return
            self.play_dealer()
            e = await self.settle(self.compare())
            e.set_footer(text=f"Bet: {self.bet:,} • Timed out, auto-stand")
            try:
                await self.interaction.edit_original_response(embed=e, view=None)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------- cog

class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.active_blackjack: set[int] = set()

    async def take_bet(self, interaction: discord.Interaction, raw: str) -> int | None:
        """Parses the bet and removes it from the wallet. Returns the bet,
        or None (after replying with an error) if the bet is invalid."""
        uid = interaction.user.id
        balance = await self.db.get_balance(uid)
        bet = parse_bet(raw, balance)

        async def fail(msg: str):
            await interaction.response.send_message(embed=error_embed(msg), ephemeral=True)

        if bet is None:
            await fail("Invalid bet. Use a number, `all`, `half`, or something like `5k`.")
            return None
        if bet < 1:
            await fail("You're broke. Try `/daily` or `/work` first." if balance == 0 else "Minimum bet is 1.")
            return None
        if bet > MAX_BET:
            await fail(f"Maximum bet is {MAX_BET:,}.")
            return None
        if await self.db.change_balance(uid, -bet) is None:
            await fail(f"You only have {money(balance)}.")
            return None
        return bet

    # ----- /coinflip

    @app_commands.command(name="coinflip", description="Flip a coin. Double your bet or lose it")
    @everywhere
    @app_commands.describe(bet="Amount, or 'all' / 'half' / '5k'", side="Heads or tails")
    @app_commands.choices(
        side=[app_commands.Choice(name="Heads", value="heads"), app_commands.Choice(name="Tails", value="tails")]
    )
    async def coinflip(self, interaction: discord.Interaction, bet: str, side: app_commands.Choice[str]):
        amount = await self.take_bet(interaction, bet)
        if amount is None:
            return
        result = random.choice(["heads", "tails"])
        won = result == side.value
        if won:
            await self.db.change_balance(interaction.user.id, amount * 2)
        balance = await self.db.get_balance(interaction.user.id)
        if won:
            e = embed("🪙 Coinflip", f"It landed on **{result}**!\n✅ You won {money(amount)}", color=GREEN)
        else:
            e = embed("🪙 Coinflip", f"It landed on **{result}**.\n❌ You lost {money(amount)}", color=RED)
        e.set_footer(text=f"You picked {side.value}")
        e.add_field(name="Balance", value=money(balance))
        await interaction.response.send_message(embed=e)

    # ----- /slots

    @app_commands.command(name="slots", description="Spin the slot machine")
    @everywhere
    @app_commands.describe(bet="Amount, or 'all' / 'half' / '5k'")
    async def slots(self, interaction: discord.Interaction, bet: str):
        amount = await self.take_bet(interaction, bet)
        if amount is None:
            return
        reels = random.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)
        mult = slot_multiplier(reels)
        payout = int(amount * mult)
        if payout:
            await self.db.change_balance(interaction.user.id, payout)
        balance = await self.db.get_balance(interaction.user.id)

        def frame(shown: int) -> str:
            cells = [reels[i] if i < shown else "❓" for i in range(3)]
            return f"## [ {' | '.join(cells)} ]"

        await interaction.response.send_message(embed=embed("🎰 Slots", frame(0)))
        for shown in (1, 2):
            await asyncio.sleep(0.8)
            await interaction.edit_original_response(embed=embed("🎰 Slots", frame(shown)))
        await asyncio.sleep(0.8)

        net = payout - amount
        if net > 0:
            text, color = f"✅ **{mult:g}x**! You won {money(net)}", GREEN
        elif net == 0 and payout:
            text, color = "🤝 Break-even", GREY
        else:
            text, color = f"❌ No match. You lost {money(amount)}", RED
        e = embed("🎰 Slots", f"{frame(3)}\n{text}", color=color)
        e.add_field(name="Balance", value=money(balance))
        await interaction.edit_original_response(embed=e)

    # ----- /blackjack

    @app_commands.command(name="blackjack", description="Play blackjack against the dealer")
    @everywhere
    @app_commands.describe(bet="Amount, or 'all' / 'half' / '5k'")
    async def blackjack(self, interaction: discord.Interaction, bet: str):
        uid = interaction.user.id
        if uid in self.active_blackjack:
            return await interaction.response.send_message(
                embed=error_embed("You already have a blackjack game running. Finish it first."),
                ephemeral=True,
            )
        amount = await self.take_bet(interaction, bet)
        if amount is None:
            return
        self.active_blackjack.add(uid)
        try:
            view = BlackjackView(self, interaction, amount)
            outcome = view.initial_outcome()
            if outcome:
                e = await view.settle(outcome)
                await interaction.response.send_message(embed=e)
            else:
                await interaction.response.send_message(embed=view.render(), view=view)
        except Exception:
            # Something broke before the game started: refund the bet.
            self.active_blackjack.discard(uid)
            await self.db.change_balance(uid, amount)
            raise


async def setup(bot):
    await bot.add_cog(Games(bot))
