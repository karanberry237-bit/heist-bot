import os

import discord
from discord import app_commands

CURRENCY = os.getenv("CURRENCY_EMOJI", "🪙")
COLOR = 0xFF3366


def everywhere(func):
    """Make a slash command usable as a user-install app: in any server,
    in DMs with the bot, and in group DMs. Apply BELOW @app_commands.command()."""
    func = app_commands.allowed_installs(guilds=True, users=True)(func)
    func = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)(func)
    return func


def money(n: int) -> str:
    return f"{CURRENCY} **{n:,}**"


def embed(title: str | None = None, description: str | None = None, *, color: int = COLOR) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def error_embed(description: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {description}", color=0xED4245)
