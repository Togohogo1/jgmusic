"""Manages general listeners like on_ready or on_command_error"""

import aiosqlite

from discord.ext import commands

class Info(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        # Some debug info

        print(f"JoshGone logged on as {self.bot.user}.")
        print(f"SQLite version is {aiosqlite.sqlite_version}.")

        # BUG: owner_id not initialized until someone runs a command
        # https://discordpy.readthedocs.io/en/stable/ext/commands/api.html?highlight=owner_id#discord.ext.commands.Bot.owner_id
        print(f"Initializing bot `owner_id` [Workaround to potential bug].")
        appinfo = await self.bot.application_info()
        self.bot.owner_id = appinfo.owner.id

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        # Unpack the error for cleaner error messages
        if isinstance(error, commands.CommandInvokeError):
            error = error.__cause__ or error
        try:
            await ctx.send(f"Oops, an error occurred: `{error!r}`")
        except Exception:
            print(f"Error: {error!r}")
            raise

def setup(bot):
    return bot.add_cog(Info(bot))
