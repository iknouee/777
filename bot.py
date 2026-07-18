import logging
import os
import threading

import discord
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

BANNER_URL = (
    "https://cdn.discordapp.com/attachments/"
    "1528040063374594178/1528040160703418428/"
    "ChatGPT_Image_Jul_18_2026_03_05_11_PM.png"
    "?ex=6a5cd9cb&is=6a5b884b"
    "&hm=9ddfe47eadd879c09d84d364ddc4f2d3b195f2ce5be4f75f4edebcb8a3a677df"
)

GOLD_COLOUR = discord.Colour.from_rgb(255, 191, 36)

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Add it to your Render environment variables."
    )

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)

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
    app.run(
        host="0.0.0.0",
        port=PORT,
    )


def get_welcome_channel(guild: discord.Guild):
    if WELCOME_CHANNEL_ID:
        try:
            channel_id = int(WELCOME_CHANNEL_ID)
            channel = guild.get_channel(channel_id)

            if isinstance(channel, discord.TextChannel):
                return channel

        except ValueError:
            logger.warning("WELCOME_CHANNEL_ID must only contain numbers.")

    if guild.system_channel:
        return guild.system_channel

    if guild.me is None:
        return None

    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me)

        if permissions.view_channel and permissions.send_messages:
            return channel

    return None


@bot.event
async def on_ready():
    logger.info(
        "Logged in as %s | ID: %s",
        bot.user,
        bot.user.id if bot.user else "Unknown",
    )

    try:
        synced_commands = await bot.tree.sync()

        logger.info(
            "Synced %s slash command(s).",
            len(synced_commands),
        )

    except Exception:
        logger.exception("Failed to sync slash commands.")

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(
            name="with the 777 friend group"
        ),
    )


@bot.event
async def on_member_join(member: discord.Member):
    channel = get_welcome_channel(member.guild)

    if channel is None:
        logger.warning(
            "No welcome channel was found in %s.",
            member.guild.name,
        )
        return

    member_number = member.guild.member_count or 0

    embed = discord.Embed(
        title="✦ Welcome to 777 ✦",
        description=(
            f"Welcome {member.mention}!\n\n"
            f"You just joined **{member.guild.name}**.\n"
            "Make yourself at home and enjoy the server."
        ),
        colour=GOLD_COLOUR,
    )

    embed.set_author(
        name="777 Friend Group",
        icon_url=member.guild.icon.url if member.guild.icon else None,
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    embed.set_image(
        url=BANNER_URL
    )

    embed.add_field(
        name="Member",
        value=f"`#{member_number}`",
        inline=True,
    )

    embed.add_field(
        name="Account Created",
        value=discord.utils.format_dt(
            member.created_at,
            style="R",
        ),
        inline=True,
    )

    embed.add_field(
        name="Getting Started",
        value=(
            "Read the rules, choose your roles, "
            "and say hello to everyone."
        ),
        inline=False,
    )

    embed.set_footer(
        text="777 • Roblox friend group"
    )

    try:
        await channel.send(
            content=f"Welcome to the server, {member.mention}! 👑",
            embed=embed,
        )

    except discord.Forbidden:
        logger.warning(
            "777 does not have permission to send welcome messages in %s.",
            channel.name,
        )

    except discord.HTTPException:
        logger.exception(
            "Failed to send a welcome message."
        )


@bot.event
async def on_member_remove(member: discord.Member):
    channel = get_welcome_channel(member.guild)

    if channel is None:
        logger.warning(
            "No goodbye channel was found in %s.",
            member.guild.name,
        )
        return

    embed = discord.Embed(
        title="✦ Goodbye from 777 ✦",
        description=(
            f"**{member.display_name}** has left the server.\n\n"
            "Thanks for being part of the friend group."
        ),
        colour=GOLD_COLOUR,
    )

    embed.set_author(
        name="777 Friend Group",
        icon_url=member.guild.icon.url if member.guild.icon else None,
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    embed.set_image(
        url=BANNER_URL
    )

    embed.set_footer(
        text=f"777 now has {member.guild.member_count or 0} members"
    )

    try:
        await channel.send(embed=embed)

    except discord.Forbidden:
        logger.warning(
            "777 does not have permission to send goodbye messages in %s.",
            channel.name,
        )

    except discord.HTTPException:
        logger.exception(
            "Failed to send a goodbye message."
        )


@bot.tree.command(
    name="ping",
    description="Check whether 777 is online.",
)
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)

    embed = discord.Embed(
        title="🏓 777 is online",
        description=f"Current latency: `{latency}ms`",
        colour=GOLD_COLOUR,
    )

    embed.set_thumbnail(
        url=bot.user.display_avatar.url if bot.user else None
    )

    embed.set_footer(
        text="777 • Running normally"
    )

    await interaction.response.send_message(
        embed=embed
    )


@bot.tree.command(
    name="about",
    description="Learn more about the 777 bot.",
)
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="✦ 777 Bot ✦",
        description=(
            "A custom community bot made for the "
            "**777 Roblox friend group**."
        ),
        colour=GOLD_COLOUR,
    )

    embed.set_image(
        url=BANNER_URL
    )

    embed.add_field(
        name="Current Features",
        value=(
            "• Welcome messages\n"
            "• Goodbye messages\n"
            "• `/ping`\n"
            "• `/about`"
        ),
        inline=False,
    )

    embed.add_field(
        name="Coming Soon",
        value=(
            "• Quotes\n"
            "• Clips\n"
            "• Counting\n"
            "• Smash or Pass\n"
            "• Polls"
        ),
        inline=False,
    )

    embed.set_footer(
        text="Made for the 777 friend group"
    )

    await interaction.response.send_message(
        embed=embed
    )


if __name__ == "__main__":
    threading.Thread(
        target=run_web_server,
        daemon=True,
    ).start()

    bot.run(TOKEN)
