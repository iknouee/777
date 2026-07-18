import logging
import os
import threading

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("777-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Add it as an environment variable on Render."
    )

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
app = Flask(__name__)


@app.get("/")
def home():
    return {
        "bot": "777",
        "status": "online" if bot.is_ready() else "starting",
    }, 200


@app.get("/health")
def health():
    return "OK", 200


def run_web_server():
    app.run(host="0.0.0.0", port=PORT)


def get_welcome_channel(guild: discord.Guild):
    """Use WELCOME_CHANNEL_ID if configured, otherwise use the system channel."""
    if WELCOME_CHANNEL_ID:
        try:
            channel = guild.get_channel(int(WELCOME_CHANNEL_ID))
            if isinstance(channel, discord.TextChannel):
                return channel
        except ValueError:
            logger.warning("WELCOME_CHANNEL_ID is not a valid number.")

    if guild.system_channel:
        return guild.system_channel

    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me)
        if permissions.send_messages:
            return channel

    return None


@bot.event
async def on_ready():
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id)

    try:
        synced = await bot.tree.sync()
        logger.info("Synced %s slash command(s).", len(synced))
    except Exception:
        logger.exception("Failed to sync slash commands.")

    activity = discord.Game(name="with the 777 friend group")
    await bot.change_presence(status=discord.Status.online, activity=activity)


@bot.event
async def on_member_join(member: discord.Member):
    channel = get_welcome_channel(member.guild)
    if channel is None:
        logger.warning("No suitable welcome channel found in %s.", member.guild.name)
        return

    embed = discord.Embed(
        title="Welcome to 777!",
        description=(
            f"Hey {member.mention}, welcome to **{member.guild.name}**!\n"
            "Glad to have you in the friend group 🎉"
        ),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Member #{member.guild.member_count}")

    await channel.send(embed=embed)


@bot.event
async def on_member_remove(member: discord.Member):
    channel = get_welcome_channel(member.guild)
    if channel is None:
        logger.warning("No suitable goodbye channel found in %s.", member.guild.name)
        return

    embed = discord.Embed(
        title="See you later!",
        description=f"**{member.display_name}** has left **{member.guild.name}**.",
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    await channel.send(embed=embed)


@bot.tree.command(name="ping", description="Check whether 777 is online.")
async def ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)
    await interaction.response.send_message(
        f"🏓 **777 is online!** `{latency_ms}ms`"
    )


@bot.tree.command(name="about", description="Learn about the 777 bot.")
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="777",
        description=(
            "A custom bot for the 777 Roblox friend group.\n\n"
            "**Current features**\n"
            "• Welcome messages\n"
            "• Goodbye messages\n"
            "• `/ping`\n"
            "• `/about`"
        ),
    )
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(TOKEN)
