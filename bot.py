import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from db import Database
from utils import error_embed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

PREFIX = os.getenv("PREFIX", ",sonl")
EXTENSIONS = ["cogs.economy", "cogs.games", "cogs.moderation"]


class HeistBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Privileged intent: required for prefix commands (e.g. ",balance").
        # Enable "Message Content Intent" in the Developer Portal -> Bot tab.
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned_or(PREFIX),
            intents=intents,
            help_command=None,
            case_insensitive=True,
            strip_after_prefix=True,  # allows ", balance" as well as ",balance"
        )
        self.db = Database()

    async def setup_hook(self):
        await self.db.connect(os.environ["DATABASE_URL"])
        log.info("Database connected")

        for ext in EXTENSIONS:
            await self.load_extension(ext)
            log.info("Loaded %s", ext)

        self.tree.on_error = self.on_tree_error

        # Global sync can take up to an hour to show up; a dev guild syncs instantly.
        if os.getenv("SYNC_ON_START", "true").lower() == "true":
            dev_guild = os.getenv("DEV_GUILD_ID")
            if dev_guild:
                guild = discord.Object(id=int(dev_guild))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Synced commands to dev guild %s", dev_guild)
            else:
                await self.tree.sync()
                log.info("Synced commands globally")

    async def on_ready(self):
        log.info("Logged in as %s (%s) | prefix: %s", self.user, self.user.id, PREFIX)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handles errors for both prefix and slash (hybrid) invocations."""
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandInvokeError):
            error = error.original

        # Permission problems: not a moderator, missing Discord perms, DM use, etc.
        if isinstance(error, commands.CheckFailure):
            return await ctx.send(
                embed=error_embed(str(error) or "You can't use this command."), ephemeral=True
            )

        if isinstance(
            error,
            (commands.MissingRequiredArgument, commands.BadArgument, commands.BadUnionArgument, commands.TooManyArguments),
        ):
            sep = " " if ctx.clean_prefix[-1:].isalnum() else ""  # ",sonl ban" vs "/ban"
            usage = f"{ctx.clean_prefix}{sep}{ctx.command.qualified_name} {ctx.command.signature}".strip()
            return await ctx.send(embed=error_embed(f"Invalid input. Usage: `{usage}`"), ephemeral=True)

        log.error("Command error in %s", ctx.command, exc_info=error)
        await ctx.send(embed=error_embed("Something went wrong. Please try again."), ephemeral=True)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.error("App command error", exc_info=error)
        msg = error_embed("Something went wrong. Please try again.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=msg, ephemeral=True)
        else:
            await interaction.response.send_message(embed=msg, ephemeral=True)

    async def close(self):
        await self.db.close()
        await super().close()


def main():
    bot = HeistBot()
    bot.run(os.environ["DISCORD_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
