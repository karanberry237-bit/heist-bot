import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from db import Database
from utils import error_embed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

EXTENSIONS = ["cogs.economy"]


class HeistBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned,  # slash-only bot
            intents=discord.Intents.default(),
            help_command=None,
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
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("Command error", exc_info=error)
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
