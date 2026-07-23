from discord.ext import commands


COMMAND_VERSION = "1"


class DeploymentSmokeCommands(commands.Cog):
    @commands.hybrid_command(name="hotload_command_smoke")
    async def hotload_command_smoke(self, ctx):
        await ctx.reply('{"ok":true,"channel":"command","version":"1"}')


async def setup(bot):
    await bot.add_cog(DeploymentSmokeCommands())
