import io
import logging
import os
import re
import textwrap
import threading
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


# =========================================================
# CONFIGURATION
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("777-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID")
PORT = int(os.getenv("PORT", "10000"))

BANNER_URL = os.getenv(
    "BANNER_URL",
    (
        "https://cdn.discordapp.com/attachments/"
        "1528040063374594178/1528040160703418428/"
        "ChatGPT_Image_Jul_18_2026_03_05_11_PM.png"
        "?ex=6a5cd9cb&is=6a5b884b"
        "&hm=9ddfe47eadd879c09d84d364ddc4f2d3b195f2ce5be4f75f4edebcb8a3a677df"
    ),
)

GOLD_COLOUR = discord.Colour.from_rgb(255, 191, 36)

GOLD_RGB = (255, 191, 36)
SOFT_GOLD_RGB = (235, 190, 90)
WHITE_RGB = (245, 245, 245)
GREY_RGB = (170, 170, 170)

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. "
        "Add it to your Render environment variables."
    )


# =========================================================
# DISCORD BOT SETUP
# =========================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)

app = Flask(__name__)


# =========================================================
# RENDER WEB SERVER
# =========================================================

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


# =========================================================
# GENERAL HELPERS
# =========================================================

def get_welcome_channel(
    guild: discord.Guild,
) -> discord.TextChannel | None:
    if WELCOME_CHANNEL_ID:
        try:
            channel_id = int(WELCOME_CHANNEL_ID)
            channel = guild.get_channel(channel_id)

            if isinstance(channel, discord.TextChannel):
                return channel

        except ValueError:
            logger.warning(
                "WELCOME_CHANNEL_ID must contain only numbers."
            )

    if guild.system_channel:
        return guild.system_channel

    if guild.me is None:
        return None

    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me)

        if (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
        ):
            return channel

    return None


def clean_message_content(message: discord.Message) -> str:
    """
    Converts raw mentions into readable names and removes
    excess whitespace.
    """

    content = message.clean_content.strip()

    content = re.sub(
        r"\s+",
        " ",
        content,
    )

    return content


def get_font(
    size: int,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    possible_paths = []

    if bold:
        possible_paths.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/dejavu/DejaVuSansCondensed-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            ]
        )
    else:
        possible_paths.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/dejavu/DejaVuSansCondensed.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            ]
        )

    for path in possible_paths:
        if os.path.exists(path):
            return ImageFont.truetype(
                path,
                size=size,
            )

    return ImageFont.load_default()


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    starting_size: int = 68,
    minimum_size: int = 32,
) -> tuple[ImageFont.ImageFont, list[str]]:
    """
    Finds a font size and wrapped lines that fit within the
    allowed quote area.
    """

    for size in range(
        starting_size,
        minimum_size - 1,
        -2,
    ):
        font = get_font(size)

        average_character_width = max(
            draw.textlength(
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                font=font,
            ) / 26,
            1,
        )

        estimated_characters = max(
            int(max_width / average_character_width),
            10,
        )

        lines = textwrap.wrap(
            text,
            width=estimated_characters,
            break_long_words=True,
            replace_whitespace=True,
        )

        line_spacing = int(size * 0.35)

        line_heights = []

        for line in lines:
            box = draw.textbbox(
                (0, 0),
                line,
                font=font,
            )

            line_heights.append(
                box[3] - box[1]
            )

        total_height = (
            sum(line_heights)
            + line_spacing * max(len(lines) - 1, 0)
        )

        widest_line = max(
            (
                draw.textlength(
                    line,
                    font=font,
                )
                for line in lines
            ),
            default=0,
        )

        if (
            widest_line <= max_width
            and total_height <= max_height
        ):
            return font, lines

    font = get_font(minimum_size)

    average_character_width = max(
        draw.textlength(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            font=font,
        ) / 26,
        1,
    )

    estimated_characters = max(
        int(max_width / average_character_width),
        10,
    )

    lines = textwrap.wrap(
        text,
        width=estimated_characters,
        break_long_words=True,
        replace_whitespace=True,
    )

    return font, lines


async def download_avatar(
    user: discord.User | discord.Member,
) -> Image.Image:
    avatar_asset = user.display_avatar.replace(
        size=512,
        format="png",
    )

    avatar_bytes = await avatar_asset.read()

    return Image.open(
        io.BytesIO(avatar_bytes)
    ).convert("RGB")


# =========================================================
# QUOTE IMAGE GENERATOR
# =========================================================

async def create_quote_image(
    message: discord.Message,
) -> io.BytesIO:
    quote_text = clean_message_content(message)

    if not quote_text:
        raise ValueError(
            "This message does not contain any text."
        )

    if len(quote_text) > 800:
        quote_text = quote_text[:797] + "..."

    width = 1200
    height = 675

    avatar = await download_avatar(message.author)

    background = ImageOps.fit(
        avatar,
        (width, height),
        method=Image.Resampling.LANCZOS,
    )

    background = background.filter(
        ImageFilter.GaussianBlur(radius=22)
    )

    background = ImageEnhance.Brightness(
        background
    ).enhance(0.18)

    background = ImageEnhance.Contrast(
        background
    ).enhance(1.25)

    canvas = background.convert("RGBA")

    dark_overlay = Image.new(
        "RGBA",
        (width, height),
        (0, 0, 0, 165),
    )

    canvas = Image.alpha_composite(
        canvas,
        dark_overlay,
    )

    draw = ImageDraw.Draw(canvas)

    # Left-side gold accent
    draw.rectangle(
        (0, 0, 12, height),
        fill=GOLD_RGB,
    )

    # Subtle top and bottom lines
    draw.line(
        (70, 56, width - 70, 56),
        fill=(255, 191, 36, 120),
        width=2,
    )

    draw.line(
        (70, height - 62, width - 70, height - 62),
        fill=(255, 191, 36, 120),
        width=2,
    )

    # Decorative quotation mark
    quote_mark_font = get_font(
        150,
        bold=True,
    )

    draw.text(
        (70, 68),
        "“",
        font=quote_mark_font,
        fill=(255, 191, 36, 160),
    )

    quote_area_left = 145
    quote_area_right = width - 120
    quote_area_top = 145
    quote_area_bottom = 470

    quote_font, wrapped_lines = fit_text(
        draw=draw,
        text=quote_text,
        max_width=quote_area_right - quote_area_left,
        max_height=quote_area_bottom - quote_area_top,
    )

    line_spacing = int(
        getattr(quote_font, "size", 48) * 0.35
    )

    line_dimensions = []

    for line in wrapped_lines:
        box = draw.textbbox(
            (0, 0),
            line,
            font=quote_font,
        )

        line_dimensions.append(
            (
                box[2] - box[0],
                box[3] - box[1],
            )
        )

    total_quote_height = (
        sum(height_value for _, height_value in line_dimensions)
        + line_spacing * max(len(wrapped_lines) - 1, 0)
    )

    current_y = (
        quote_area_top
        + (
            quote_area_bottom
            - quote_area_top
            - total_quote_height
        ) // 2
    )

    for index, line in enumerate(wrapped_lines):
        line_width, line_height = line_dimensions[index]

        line_x = quote_area_left + (
            quote_area_right
            - quote_area_left
            - line_width
        ) // 2

        draw.text(
            (line_x + 3, current_y + 3),
            line,
            font=quote_font,
            fill=(0, 0, 0, 190),
        )

        draw.text(
            (line_x, current_y),
            line,
            font=quote_font,
            fill=WHITE_RGB,
        )

        current_y += line_height + line_spacing

    display_name = message.author.display_name

    username = str(message.author)

    author_font = get_font(
        30,
        bold=True,
    )

    username_font = get_font(
        19,
        bold=False,
    )

    date_font = get_font(
        18,
        bold=False,
    )

    footer_font = get_font(
        17,
        bold=True,
    )

    author_text = f"— {display_name}"

    author_width = draw.textlength(
        author_text,
        font=author_font,
    )

    author_x = width - 85 - author_width

    draw.text(
        (author_x, 495),
        author_text,
        font=author_font,
        fill=SOFT_GOLD_RGB,
    )

    username_text = f"@{username}"

    username_width = draw.textlength(
        username_text,
        font=username_font,
    )

    draw.text(
        (
            width - 85 - username_width,
            535,
        ),
        username_text,
        font=username_font,
        fill=GREY_RGB,
    )

    message_time = message.created_at.astimezone(
        timezone.utc
    )

    date_text = message_time.strftime(
        "%d %b %Y • %H:%M UTC"
    )

    draw.text(
        (72, height - 47),
        date_text,
        font=date_font,
        fill=(155, 155, 155),
    )

    footer_text = "777 • MAKE IT A QUOTE"

    footer_width = draw.textlength(
        footer_text,
        font=footer_font,
    )

    draw.text(
        (
            width - footer_width - 72,
            height - 47,
        ),
        footer_text,
        font=footer_font,
        fill=SOFT_GOLD_RGB,
    )

    output = io.BytesIO()

    canvas.convert("RGB").save(
        output,
        format="PNG",
        optimize=True,
    )

    output.seek(0)

    return output


# =========================================================
# QUOTE RESPONSE
# =========================================================

async def send_quote_result(
    interaction: discord.Interaction,
    message: discord.Message,
):
    if message.author.bot:
        await interaction.followup.send(
            "I cannot quote another bot's message.",
            ephemeral=True,
        )
        return

    content = clean_message_content(message)

    if not content:
        await interaction.followup.send(
            "That message does not contain any text to quote.",
            ephemeral=True,
        )
        return

    try:
        image = await create_quote_image(message)

        filename = (
            f"777_quote_{message.id}.png"
        )

        file = discord.File(
            image,
            filename=filename,
        )

        quote_embed = discord.Embed(
            title="✦ Make it a Quote ✦",
            description=(
                f"Quoted **{message.author.display_name}**\n"
                f"[Jump to the original message]({message.jump_url})"
            ),
            colour=GOLD_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

        quote_embed.set_image(
            url=f"attachment://{filename}"
        )

        quote_embed.set_footer(
            text=(
                f"Made by {interaction.user.display_name} "
                "• 777"
            ),
            icon_url=interaction.user.display_avatar.url,
        )

        await interaction.followup.send(
            embed=quote_embed,
            file=file,
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "I do not have permission to send files in this channel.",
            ephemeral=True,
        )

    except Exception:
        logger.exception(
            "Failed to create quote image."
        )

        await interaction.followup.send(
            "Something went wrong while creating the quote.",
            ephemeral=True,
        )


# =========================================================
# BOT EVENTS
# =========================================================

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
            "Synced %s application command(s).",
            len(synced_commands),
        )

    except Exception:
        logger.exception(
            "Failed to sync application commands."
        )

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(
            name="with the 777 friend group"
        ),
    )


@bot.event
async def on_member_join(
    member: discord.Member,
):
    channel = get_welcome_channel(
        member.guild
    )

    if channel is None:
        logger.warning(
            "No welcome channel found in %s.",
            member.guild.name,
        )
        return

    member_number = (
        member.guild.member_count or 0
    )

    embed = discord.Embed(
        title="✦ Welcome to 777 ✦",
        description=(
            f"Welcome {member.mention}!\n\n"
            f"You just joined **{member.guild.name}**.\n"
            "Make yourself at home and enjoy the server."
        ),
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )

    embed.set_author(
        name="777 Friend Group",
        icon_url=(
            member.guild.icon.url
            if member.guild.icon
            else None
        ),
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    if BANNER_URL:
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
            content=(
                f"Welcome to the server, "
                f"{member.mention}! 👑"
            ),
            embed=embed,
        )

    except discord.Forbidden:
        logger.warning(
            "Missing permission to send welcome "
            "messages in %s.",
            channel.name,
        )

    except discord.HTTPException:
        logger.exception(
            "Failed to send welcome message."
        )


@bot.event
async def on_member_remove(
    member: discord.Member,
):
    channel = get_welcome_channel(
        member.guild
    )

    if channel is None:
        logger.warning(
            "No goodbye channel found in %s.",
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
        timestamp=datetime.now(timezone.utc),
    )

    embed.set_author(
        name="777 Friend Group",
        icon_url=(
            member.guild.icon.url
            if member.guild.icon
            else None
        ),
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    if BANNER_URL:
        embed.set_image(
            url=BANNER_URL
        )

    embed.set_footer(
        text=(
            f"777 now has "
            f"{member.guild.member_count or 0} members"
        )
    )

    try:
        await channel.send(
            embed=embed
        )

    except discord.Forbidden:
        logger.warning(
            "Missing permission to send goodbye "
            "messages in %s.",
            channel.name,
        )

    except discord.HTTPException:
        logger.exception(
            "Failed to send goodbye message."
        )


# =========================================================
# MESSAGE CONTEXT MENU
# =========================================================

@app_commands.context_menu(
    name="Make it a Quote"
)
async def make_it_a_quote(
    interaction: discord.Interaction,
    message: discord.Message,
):
    await interaction.response.defer(
        thinking=True
    )

    await send_quote_result(
        interaction,
        message,
    )


bot.tree.add_command(
    make_it_a_quote
)


# =========================================================
# SLASH COMMANDS
# =========================================================

@bot.tree.command(
    name="ping",
    description="Check whether 777 is online.",
)
async def ping(
    interaction: discord.Interaction,
):
    latency = round(
        bot.latency * 1000
    )

    embed = discord.Embed(
        title="🏓 777 is online",
        description=(
            f"Current latency: `{latency}ms`"
        ),
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )

    if bot.user:
        embed.set_thumbnail(
            url=bot.user.display_avatar.url
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
async def about(
    interaction: discord.Interaction,
):
    embed = discord.Embed(
        title="✦ 777 Bot ✦",
        description=(
            "A custom community bot made for the "
            "**777 Roblox friend group**."
        ),
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )

    if BANNER_URL:
        embed.set_image(
            url=BANNER_URL
        )

    embed.add_field(
        name="Current Features",
        value=(
            "• Welcome messages\n"
            "• Goodbye messages\n"
            "• Quote image generator\n"
            "• Right-click message quoting\n"
            "• `/quote`\n"
            "• `!quote` replies\n"
            "• `/ping`\n"
            "• `/about`"
        ),
        inline=False,
    )

    embed.add_field(
        name="How to Make a Quote",
        value=(
            "Right-click a message and select:\n"
            "**Apps → Make it a Quote**\n\n"
            "You can also run `/quote` with a "
            "message link, or reply with `!quote`."
        ),
        inline=False,
    )

    embed.add_field(
        name="Coming Soon",
        value=(
            "• Saved quote database\n"
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


@bot.tree.command(
    name="quote",
    description="Turn a Discord message into a quote image.",
)
@app_commands.describe(
    message_link=(
        "Paste the Discord message link you want to quote."
    )
)
async def quote_slash(
    interaction: discord.Interaction,
    message_link: str,
):
    await interaction.response.defer(
        thinking=True
    )

    match = re.search(
        r"discord(?:app)?\.com/channels/"
        r"(\d+)/(\d+)/(\d+)",
        message_link,
    )

    if not match:
        await interaction.followup.send(
            "That does not look like a valid Discord message link.",
            ephemeral=True,
        )
        return

    guild_id = int(
        match.group(1)
    )

    channel_id = int(
        match.group(2)
    )

    message_id = int(
        match.group(3)
    )

    if (
        interaction.guild is None
        or interaction.guild.id != guild_id
    ):
        await interaction.followup.send(
            "That message must be from this server.",
            ephemeral=True,
        )
        return

    channel = interaction.guild.get_channel(
        channel_id
    )

    if not isinstance(
        channel,
        discord.TextChannel,
    ):
        await interaction.followup.send(
            "I could not find that text channel.",
            ephemeral=True,
        )
        return

    try:
        message = await channel.fetch_message(
            message_id
        )

    except discord.NotFound:
        await interaction.followup.send(
            "I could not find that message.",
            ephemeral=True,
        )
        return

    except discord.Forbidden:
        await interaction.followup.send(
            "I cannot access that message or channel.",
            ephemeral=True,
        )
        return

    await send_quote_result(
        interaction,
        message,
    )


# =========================================================
# PREFIX QUOTE COMMAND
# =========================================================

@bot.command(
    name="quote"
)
@commands.guild_only()
async def quote_reply(
    context: commands.Context,
):
    """
    Reply to a message with !quote.
    """

    if not context.message.reference:
        await context.reply(
            "Reply to a message with `!quote` "
            "to turn it into a quote image.",
            mention_author=False,
        )
        return

    resolved = context.message.reference.resolved

    if isinstance(
        resolved,
        discord.Message,
    ):
        target_message = resolved

    else:
        try:
            target_message = (
                await context.channel.fetch_message(
                    context.message.reference.message_id
                )
            )

        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
        ):
            await context.reply(
                "I could not access the message "
                "you replied to.",
                mention_author=False,
            )
            return

    if target_message.author.bot:
        await context.reply(
            "I cannot quote another bot's message.",
            mention_author=False,
        )
        return

    quote_text = clean_message_content(
        target_message
    )

    if not quote_text:
        await context.reply(
            "That message does not contain any text.",
            mention_author=False,
        )
        return

    async with context.typing():
        try:
            image = await create_quote_image(
                target_message
            )

            filename = (
                f"777_quote_{target_message.id}.png"
            )

            file = discord.File(
                image,
                filename=filename,
            )

            embed = discord.Embed(
                title="✦ Make it a Quote ✦",
                description=(
                    f"Quoted "
                    f"**{target_message.author.display_name}**\n"
                    f"[Jump to the original message]"
                    f"({target_message.jump_url})"
                ),
                colour=GOLD_COLOUR,
                timestamp=datetime.now(timezone.utc),
            )

            embed.set_image(
                url=f"attachment://{filename}"
            )

            embed.set_footer(
                text=(
                    f"Made by "
                    f"{context.author.display_name} • 777"
                ),
                icon_url=context.author.display_avatar.url,
            )

            await context.reply(
                embed=embed,
                file=file,
                mention_author=False,
            )

        except Exception:
            logger.exception(
                "Failed to create quote through !quote."
            )

            await context.reply(
                "Something went wrong while creating "
                "the quote.",
                mention_author=False,
            )


# =========================================================
# ERROR HANDLING
# =========================================================

@bot.event
async def on_command_error(
    context: commands.Context,
    error: commands.CommandError,
):
    if isinstance(
        error,
        commands.CommandNotFound,
    ):
        return

    logger.exception(
        "Prefix command error: %s",
        error,
    )

    try:
        await context.reply(
            "Something went wrong while running that command.",
            mention_author=False,
        )

    except discord.HTTPException:
        pass


# =========================================================
# START BOT
# =========================================================

if __name__ == "__main__":
    threading.Thread(
        target=run_web_server,
        daemon=True,
    ).start()

    bot.run(TOKEN)
