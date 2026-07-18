import asyncio
import io
import json
import logging
import os
import re
import random
import sqlite3
import time
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
COUNTING_CHANNEL_ID = os.getenv("COUNTING_CHANNEL_ID")
CLIPS_CHANNEL_ID = os.getenv("CLIPS_CHANNEL_ID")
SUGGESTIONS_CHANNEL_ID = os.getenv("SUGGESTIONS_CHANNEL_ID")
ECONOMY_DB_PATH = os.getenv("ECONOMY_DB_PATH", "economy.db")
ECONOMY_STARTING_BALANCE = int(
    os.getenv("ECONOMY_STARTING_BALANCE", "500")
)
SLOTS_MIN_BET = int(os.getenv("SLOTS_MIN_BET", "10"))
SLOTS_MAX_BET = int(os.getenv("SLOTS_MAX_BET", "10000"))
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

COUNTING_STATE_FILE = Path("counting_state.json")
counting_lock = asyncio.Lock()

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
# COUNTING STORAGE
# =========================================================

def default_counting_state() -> dict:
    return {
        "current": 0,
        "highest": 0,
        "last_user_id": None,
        "last_message_id": None,
    }


def load_counting_states() -> dict:
    if not COUNTING_STATE_FILE.exists():
        return {}

    try:
        with COUNTING_STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}

    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load counting state.")
        return {}


def save_counting_states(states: dict) -> None:
    temporary_file = COUNTING_STATE_FILE.with_suffix(".tmp")

    try:
        with temporary_file.open("w", encoding="utf-8") as file:
            json.dump(states, file, indent=2)

        temporary_file.replace(COUNTING_STATE_FILE)

    except OSError:
        logger.exception("Failed to save counting state.")


counting_states = load_counting_states()


def get_counting_state(guild_id: int) -> dict:
    key = str(guild_id)

    state = counting_states.get(key)

    if not isinstance(state, dict):
        state = default_counting_state()
        counting_states[key] = state

    defaults = default_counting_state()

    for field, default_value in defaults.items():
        state.setdefault(field, default_value)

    return state


def configured_counting_channel_id() -> int | None:
    if not COUNTING_CHANNEL_ID:
        return None

    try:
        return int(COUNTING_CHANNEL_ID)

    except ValueError:
        logger.warning("COUNTING_CHANNEL_ID must contain only numbers.")
        return None


async def reset_counting(
    channel: discord.TextChannel,
    state: dict,
    reason: str,
    member: discord.Member | discord.User | None = None,
) -> None:
    previous_count = int(state.get("current", 0))

    state["current"] = 0
    state["last_user_id"] = None
    state["last_message_id"] = None

    save_counting_states(counting_states)

    embed = discord.Embed(
        title="💥 Counting Reset",
        description=reason,
        colour=discord.Colour.red(),
        timestamp=datetime.now(timezone.utc),
    )

    if member:
        embed.set_author(
            name=member.display_name,
            icon_url=member.display_avatar.url,
        )

    embed.add_field(
        name="Previous streak",
        value=f"`{previous_count}`",
        inline=True,
    )

    embed.add_field(
        name="Start again",
        value="The next number is **1**.",
        inline=True,
    )

    embed.set_footer(text="777 • Counting")

    await channel.send(embed=embed)


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
    content = message.clean_content.strip()
    return re.sub(r"\s+", " ", content)


def get_font(
    size: int,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if bold:
        possible_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSansCondensed-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
    else:
        possible_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSansCondensed.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]

    for path in possible_paths:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)

    return ImageFont.load_default()


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    starting_size: int = 68,
    minimum_size: int = 32,
) -> tuple[ImageFont.ImageFont, list[str]]:
    for size in range(starting_size, minimum_size - 1, -2):
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
            box = draw.textbbox((0, 0), line, font=font)
            line_heights.append(box[3] - box[1])

        total_height = (
            sum(line_heights)
            + line_spacing * max(len(lines) - 1, 0)
        )

        widest_line = max(
            (
                draw.textlength(line, font=font)
                for line in lines
            ),
            default=0,
        )

        if widest_line <= max_width and total_height <= max_height:
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
# CLIPPED MESSAGE HELPERS
# =========================================================

def configured_clips_channel_id() -> int | None:
    if not CLIPS_CHANNEL_ID:
        return None

    try:
        return int(CLIPS_CHANNEL_ID)

    except ValueError:
        logger.warning("CLIPS_CHANNEL_ID must contain only numbers.")
        return None


def get_clips_channel(
    guild: discord.Guild,
) -> discord.TextChannel | None:
    channel_id = configured_clips_channel_id()

    if channel_id is None:
        return None

    channel = guild.get_channel(channel_id)

    if isinstance(channel, discord.TextChannel):
        return channel

    return None


async def send_clipped_message(
    interaction: discord.Interaction,
    message: discord.Message,
) -> None:
    if interaction.guild is None:
        await interaction.followup.send(
            "Messages can only be clipped inside a server.",
            ephemeral=True,
        )
        return

    clips_channel = get_clips_channel(interaction.guild)

    if clips_channel is None:
        await interaction.followup.send(
            "The clips channel has not been configured yet. "
            "Add `CLIPS_CHANNEL_ID` in Render.",
            ephemeral=True,
        )
        return

    if message.channel.id == clips_channel.id:
        await interaction.followup.send(
            "That message is already inside the clips channel.",
            ephemeral=True,
        )
        return

    if message.author.bot:
        await interaction.followup.send(
            "I cannot clip another bot's message.",
            ephemeral=True,
        )
        return

    content = message.clean_content.strip()

    if not content and not message.attachments and not message.embeds:
        await interaction.followup.send(
            "That message has no text or attachment to clip.",
            ephemeral=True,
        )
        return

    if len(content) > 3500:
        content = content[:3497] + "..."

    description = (
        f"> {content.replace(chr(10), chr(10) + '> ')}"
        if content
        else "*This message contained an attachment.*"
    )

    embed = discord.Embed(
        title="📎 777 Clip",
        description=description,
        colour=GOLD_COLOUR,
        timestamp=message.created_at,
        url=message.jump_url,
    )

    embed.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url,
    )

    embed.set_thumbnail(
        url=message.author.display_avatar.url
    )

    embed.add_field(
        name="Original Channel",
        value=message.channel.mention,
        inline=True,
    )

    embed.add_field(
        name="Clipped By",
        value=interaction.user.mention,
        inline=True,
    )

    embed.add_field(
        name="Original Message",
        value=f"[Jump to message]({message.jump_url})",
        inline=False,
    )

    image_attachment = next(
        (
            attachment
            for attachment in message.attachments
            if (
                attachment.content_type
                and attachment.content_type.startswith("image/")
            )
            or attachment.filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp")
            )
        ),
        None,
    )

    if image_attachment:
        embed.set_image(url=image_attachment.url)

    elif message.embeds:
        original_embed = message.embeds[0]

        if original_embed.image and original_embed.image.url:
            embed.set_image(url=original_embed.image.url)

        elif original_embed.thumbnail and original_embed.thumbnail.url:
            embed.set_image(url=original_embed.thumbnail.url)

    non_image_attachments = [
        attachment
        for attachment in message.attachments
        if attachment is not image_attachment
    ]

    if non_image_attachments:
        attachment_links = "\n".join(
            f"[{attachment.filename}]({attachment.url})"
            for attachment in non_image_attachments[:5]
        )

        embed.add_field(
            name="Attachments",
            value=attachment_links,
            inline=False,
        )

    embed.set_footer(
        text=f"Message ID: {message.id} • 777 Clips"
    )

    try:
        clipped_post = await clips_channel.send(embed=embed)

    except discord.Forbidden:
        await interaction.followup.send(
            "I cannot post in the configured clips channel. "
            "Check my View Channel, Send Messages and Embed Links permissions.",
            ephemeral=True,
        )
        return

    except discord.HTTPException:
        logger.exception("Failed to post a clipped message.")

        await interaction.followup.send(
            "Discord rejected the clip while I was posting it.",
            ephemeral=True,
        )
        return

    try:
        await message.add_reaction("📎")
    except (discord.Forbidden, discord.HTTPException):
        pass

    await interaction.followup.send(
        f"Clipped successfully: {clipped_post.jump_url}",
        ephemeral=True,
    )


# =========================================================
# QUOTE IMAGE GENERATOR
# =========================================================

async def create_quote_image(
    message: discord.Message,
) -> io.BytesIO:
    quote_text = clean_message_content(message)

    if not quote_text:
        raise ValueError("This message does not contain any text.")

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

    draw.rectangle(
        (0, 0, 12, height),
        fill=GOLD_RGB,
    )

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

    quote_mark_font = get_font(150, bold=True)

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
        box = draw.textbbox((0, 0), line, font=quote_font)

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

    author_font = get_font(30, bold=True)
    username_font = get_font(19)
    date_font = get_font(18)
    footer_font = get_font(17, bold=True)

    author_text = f"— {display_name}"

    author_width = draw.textlength(
        author_text,
        font=author_font,
    )

    draw.text(
        (width - 85 - author_width, 495),
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
        (width - 85 - username_width, 535),
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
        (width - footer_width - 72, height - 47),
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
        filename = f"777_quote_{message.id}.png"

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
            text=f"Made by {interaction.user.display_name} • 777",
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
        logger.exception("Failed to create quote image.")

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
            "Synced %s application command(s): %s",
            len(synced_commands),
            ", ".join(command.name for command in synced_commands),
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
    await security_check_join(member)

    channel = get_welcome_channel(member.guild)

    if channel is None:
        logger.warning(
            "No welcome channel found in %s.",
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
        embed.set_image(url=BANNER_URL)

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
            "Missing permission to send welcome messages in %s.",
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
    channel = get_welcome_channel(member.guild)

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
        embed.set_image(url=BANNER_URL)

    embed.set_footer(
        text=(
            f"777 now has "
            f"{member.guild.member_count or 0} members"
        )
    )

    try:
        await channel.send(embed=embed)

    except discord.Forbidden:
        logger.warning(
            "Missing permission to send goodbye messages in %s.",
            channel.name,
        )

    except discord.HTTPException:
        logger.exception(
            "Failed to send goodbye message."
        )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if message.guild is None:
        return

    if await security_check_message(message):
        return

    counting_channel_id = configured_counting_channel_id()

    if (
        counting_channel_id is None
        or message.channel.id != counting_channel_id
    ):
        return

    # Prefix commands are allowed to pass through without affecting counting.
    if message.content.startswith(bot.command_prefix):
        return

    raw_content = message.content.strip()

    # The counting channel should contain numbers only.
    if not re.fullmatch(r"\d+", raw_content):
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        warning = await message.channel.send(
            f"{message.author.mention}, numbers only in the counting channel."
        )

        try:
            await warning.delete(delay=5)
        except discord.HTTPException:
            pass

        return

    submitted_number = int(raw_content)

    async with counting_lock:
        state = get_counting_state(message.guild.id)
        expected_number = int(state["current"]) + 1
        last_user_id = state.get("last_user_id")

        if last_user_id == message.author.id:
            try:
                await message.add_reaction("❌")
            except discord.HTTPException:
                pass

            await reset_counting(
                channel=message.channel,
                state=state,
                reason=(
                    f"{message.author.mention} counted twice in a row.\n"
                    "Different people must take turns."
                ),
                member=message.author,
            )
            return

        if submitted_number != expected_number:
            try:
                await message.add_reaction("❌")
            except discord.HTTPException:
                pass

            await reset_counting(
                channel=message.channel,
                state=state,
                reason=(
                    f"{message.author.mention} sent **{submitted_number}**, "
                    f"but the correct number was **{expected_number}**."
                ),
                member=message.author,
            )
            return

        state["current"] = submitted_number
        state["last_user_id"] = message.author.id
        state["last_message_id"] = message.id

        if submitted_number > int(state["highest"]):
            state["highest"] = submitted_number

        save_counting_states(counting_states)

        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

        if submitted_number % 100 == 0:
            milestone_embed = discord.Embed(
                title="🎉 Counting Milestone!",
                description=(
                    f"The server reached **{submitted_number}**!\n"
                    f"Keep going — the next number is **{submitted_number + 1}**."
                ),
                colour=GOLD_COLOUR,
                timestamp=datetime.now(timezone.utc),
            )

            milestone_embed.set_footer(
                text="777 • Counting"
            )

            await message.channel.send(
                content="@here",
                embed=milestone_embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=True
                ),
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
    await interaction.response.defer(thinking=True)
    await send_quote_result(interaction, message)


bot.tree.add_command(make_it_a_quote)


@app_commands.context_menu(
    name="Clip Message"
)
async def clip_message(
    interaction: discord.Interaction,
    message: discord.Message,
):
    await interaction.response.defer(
        thinking=True,
        ephemeral=True,
    )

    await send_clipped_message(interaction, message)


bot.tree.add_command(clip_message)


# =========================================================
# LIVE COUNTDOWN HELPERS
# =========================================================

def countdown_text(seconds_remaining: int) -> str:
    seconds_remaining = max(0, seconds_remaining)
    minutes, seconds = divmod(seconds_remaining, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    return f"{minutes:02d}:{seconds:02d}"


def countdown_update_interval(seconds_remaining: int) -> int:
    if seconds_remaining <= 60:
        return 1

    if seconds_remaining <= 300:
        return 5

    return 15


# =========================================================
# SMASH OR PASS
# =========================================================

class SmashOrPassView(discord.ui.View):
    def __init__(
        self,
        target: discord.Member,
        creator: discord.Member | discord.User,
        duration_seconds: int = 60,
    ):
        super().__init__(timeout=duration_seconds)

        self.target = target
        self.creator = creator
        self.duration_seconds = duration_seconds
        self.smash_voters: set[int] = set()
        self.pass_voters: set[int] = set()
        self.message: discord.Message | None = None
        self.finished = False
        self.seconds_remaining = duration_seconds
        self.countdown_task: asyncio.Task | None = None

    def totals_text(self) -> str:
        smash_count = len(self.smash_voters)
        pass_count = len(self.pass_voters)
        total_votes = smash_count + pass_count

        if total_votes == 0:
            return (
                "🔥 **Smash:** `0`\n"
                "❌ **Pass:** `0`\n"
                "No votes yet."
            )

        smash_percentage = round(
            smash_count / total_votes * 100
        )

        pass_percentage = 100 - smash_percentage

        return (
            f"🔥 **Smash:** `{smash_count}` "
            f"({smash_percentage}%)\n"
            f"❌ **Pass:** `{pass_count}` "
            f"({pass_percentage}%)\n"
            f"🗳️ **Total votes:** `{total_votes}`"
        )

    def build_embed(
        self,
        final: bool = False,
    ) -> discord.Embed:
        smash_count = len(self.smash_voters)
        pass_count = len(self.pass_voters)
        total_votes = smash_count + pass_count

        if final:
            if total_votes == 0:
                result_text = "No one voted this round."
            elif smash_count > pass_count:
                result_text = "🔥 **SMASH wins!**"
            elif pass_count > smash_count:
                result_text = "❌ **PASS wins!**"
            else:
                result_text = "⚖️ **It is a tie!**"

            title = "🔥 Smash or Pass — Results"
            description = (
                f"Voting has ended for {self.target.mention}.\n\n"
                f"{result_text}"
            )

        else:
            title = "🔥 Smash or Pass"
            description = (
                f"What are we saying about {self.target.mention}?\n\n"
                "Press a button below to vote.\n"
                "**You can change your vote while voting is open.**"
            )

        embed = discord.Embed(
            title=title,
            description=description,
            colour=GOLD_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

        embed.set_author(
            name=self.target.display_name,
            icon_url=self.target.display_avatar.url,
        )

        embed.set_image(
            url=self.target.display_avatar.replace(
                size=1024,
                format="png",
            ).url
        )

        embed.add_field(
            name="Current Votes" if not final else "Final Votes",
            value=self.totals_text(),
            inline=False,
        )

        embed.add_field(
            name="Started By",
            value=self.creator.mention,
            inline=True,
        )

        if not final:
            embed.add_field(
                name="Voting Time",
                value=f"`{countdown_text(self.seconds_remaining)}`",
                inline=True,
            )

        embed.set_footer(
            text=(
                "777 • Smash or Pass"
                if not final
                else "777 • Voting closed"
            )
        )

        return embed

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.bot:
            await interaction.response.send_message(
                "Bots cannot vote.",
                ephemeral=True,
            )
            return False

        if interaction.user.id == self.target.id:
            await interaction.response.send_message(
                "You cannot vote on yourself.",
                ephemeral=True,
            )
            return False

        if self.finished:
            await interaction.response.send_message(
                "Voting has already ended.",
                ephemeral=True,
            )
            return False

        return True

    async def update_vote(
        self,
        interaction: discord.Interaction,
        choice: str,
    ) -> None:
        user_id = interaction.user.id

        if choice == "smash":
            already_selected = user_id in self.smash_voters

            self.pass_voters.discard(user_id)
            self.smash_voters.add(user_id)

            confirmation = (
                "Your vote is still **🔥 Smash**."
                if already_selected
                else "You voted **🔥 Smash**."
            )

        else:
            already_selected = user_id in self.pass_voters

            self.smash_voters.discard(user_id)
            self.pass_voters.add(user_id)

            confirmation = (
                "Your vote is still **❌ Pass**."
                if already_selected
                else "You voted **❌ Pass**."
            )

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

        await interaction.followup.send(
            confirmation,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Smash",
        emoji="🔥",
        style=discord.ButtonStyle.success,
    )
    async def smash_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.update_vote(interaction, "smash")

    @discord.ui.button(
        label="Pass",
        emoji="❌",
        style=discord.ButtonStyle.danger,
    )
    async def pass_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.update_vote(interaction, "pass")

    async def start_countdown(self) -> None:
        if self.countdown_task is None:
            self.countdown_task = asyncio.create_task(
                self.countdown_loop()
            )

    async def countdown_loop(self) -> None:
        try:
            while not self.finished and self.seconds_remaining > 0:
                interval = min(
                    countdown_update_interval(
                        self.seconds_remaining
                    ),
                    self.seconds_remaining,
                )

                await asyncio.sleep(interval)
                self.seconds_remaining = max(
                    0,
                    self.seconds_remaining - interval,
                )

                if self.finished:
                    return

                if self.seconds_remaining == 0:
                    await self.finish_vote()
                    return

                if self.message is not None:
                    try:
                        await self.message.edit(
                            embed=self.build_embed(),
                            view=self,
                        )
                    except discord.HTTPException:
                        logger.exception(
                            "Failed to update Smash or Pass timer."
                        )

        except asyncio.CancelledError:
            return

    async def finish_vote(self) -> None:
        if self.finished:
            return

        self.finished = True
        self.seconds_remaining = 0
        self.stop()

        for item in self.children:
            item.disabled = True

        if self.message is None:
            return

        try:
            await self.message.edit(
                embed=self.build_embed(final=True),
                view=self,
            )

        except discord.HTTPException:
            logger.exception(
                "Failed to close a Smash or Pass vote."
            )

    async def on_timeout(self):
        await self.finish_vote()


# =========================================================
# POLLS
# =========================================================

class PollOptionButton(discord.ui.Button):
    def __init__(
        self,
        option_index: int,
        option_text: str,
        row: int,
    ):
        super().__init__(
            label=option_text[:80],
            style=discord.ButtonStyle.secondary,
            custom_id=f"777_poll_option_{option_index}",
            row=row,
        )

        self.option_index = option_index

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        view = self.view

        if not isinstance(view, PollView):
            await interaction.response.send_message(
                "This poll is no longer available.",
                ephemeral=True,
            )
            return

        await view.cast_vote(
            interaction,
            self.option_index,
        )


class EndPollButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="End Poll",
            emoji="🛑",
            style=discord.ButtonStyle.danger,
            row=4,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        view = self.view

        if not isinstance(view, PollView):
            await interaction.response.send_message(
                "This poll is no longer available.",
                ephemeral=True,
            )
            return

        if interaction.user.id != view.creator.id:
            permissions = getattr(
                interaction.user,
                "guild_permissions",
                None,
            )

            if not permissions or not permissions.manage_messages:
                await interaction.response.send_message(
                    "Only the poll creator or a moderator can end this poll.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True)
        await view.finish_poll(ended_early=True)

        await interaction.followup.send(
            "The poll has been ended.",
            ephemeral=True,
        )


class PollView(discord.ui.View):
    def __init__(
        self,
        question: str,
        options: list[str],
        creator: discord.Member | discord.User,
        duration_seconds: int,
        anonymous: bool,
    ):
        super().__init__(timeout=duration_seconds)

        self.question = question
        self.options = options
        self.creator = creator
        self.duration_seconds = duration_seconds
        self.anonymous = anonymous

        self.votes: dict[int, int] = {}
        self.message: discord.Message | None = None
        self.finished = False
        self.seconds_remaining = duration_seconds
        self.countdown_task: asyncio.Task | None = None

        for index, option in enumerate(options):
            self.add_item(
                PollOptionButton(
                    option_index=index,
                    option_text=option,
                    row=index // 2,
                )
            )

        self.add_item(EndPollButton())

    def vote_counts(self) -> list[int]:
        counts = [0 for _ in self.options]

        for option_index in self.votes.values():
            if 0 <= option_index < len(counts):
                counts[option_index] += 1

        return counts

    def results_text(self) -> str:
        counts = self.vote_counts()
        total_votes = len(self.votes)
        lines = []

        number_emojis = [
            "1️⃣",
            "2️⃣",
            "3️⃣",
            "4️⃣",
            "5️⃣",
        ]

        for index, option in enumerate(self.options):
            count = counts[index]

            percentage = (
                round(count / total_votes * 100)
                if total_votes > 0
                else 0
            )

            lines.append(
                f"{number_emojis[index]} **{option}**\n"
                f"`{count}` vote{'s' if count != 1 else ''} • "
                f"`{percentage}%`"
            )

        return "\n\n".join(lines)

    def winner_text(self) -> str:
        counts = self.vote_counts()
        total_votes = len(self.votes)

        if total_votes == 0:
            return "No votes were cast."

        highest_count = max(counts)

        winners = [
            self.options[index]
            for index, count in enumerate(counts)
            if count == highest_count
        ]

        if len(winners) == 1:
            return f"🏆 **Winner:** {winners[0]}"

        joined_winners = ", ".join(winners)

        return f"⚖️ **Tie:** {joined_winners}"

    def build_embed(
        self,
        final: bool = False,
        ended_early: bool = False,
    ) -> discord.Embed:
        if final:
            title = "📊 Poll Results"
            ending_note = (
                "The poll was ended early."
                if ended_early
                else "Voting has ended."
            )

            description = (
                f"**{self.question}**\n\n"
                f"{ending_note}\n"
                f"{self.winner_text()}"
            )

        else:
            title = "📊 777 Poll"
            description = (
                f"**{self.question}**\n\n"
                "Choose one option below. "
                "You can change your vote while the poll is open."
            )

        embed = discord.Embed(
            title=title,
            description=description,
            colour=GOLD_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(
            name="Results" if final else "Live Results",
            value=self.results_text(),
            inline=False,
        )

        embed.add_field(
            name="Votes",
            value=f"`{len(self.votes)}`",
            inline=True,
        )

        embed.add_field(
            name="Created By",
            value=self.creator.mention,
            inline=True,
        )

        if not final:
            embed.add_field(
                name="Time",
                value=f"`{countdown_text(self.seconds_remaining)}`",
                inline=True,
            )

        embed.add_field(
            name="Voting",
            value=(
                "Anonymous"
                if self.anonymous
                else "Private vote confirmations"
            ),
            inline=True,
        )

        embed.set_footer(
            text=(
                "777 • Poll open"
                if not final
                else "777 • Poll closed"
            )
        )

        return embed

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.bot:
            await interaction.response.send_message(
                "Bots cannot vote.",
                ephemeral=True,
            )
            return False

        if self.finished:
            await interaction.response.send_message(
                "This poll has already ended.",
                ephemeral=True,
            )
            return False

        return True

    async def cast_vote(
        self,
        interaction: discord.Interaction,
        option_index: int,
    ) -> None:
        previous_vote = self.votes.get(
            interaction.user.id
        )

        self.votes[interaction.user.id] = option_index

        selected_option = self.options[option_index]

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

        if previous_vote is None:
            confirmation = (
                f"Your vote for **{selected_option}** was recorded."
            )

        elif previous_vote == option_index:
            confirmation = (
                f"Your vote is still **{selected_option}**."
            )

        else:
            confirmation = (
                f"Your vote was changed to **{selected_option}**."
            )

        await interaction.followup.send(
            confirmation,
            ephemeral=True,
        )

    async def start_countdown(self) -> None:
        if self.countdown_task is None:
            self.countdown_task = asyncio.create_task(
                self.countdown_loop()
            )

    async def countdown_loop(self) -> None:
        try:
            while not self.finished and self.seconds_remaining > 0:
                interval = min(
                    countdown_update_interval(
                        self.seconds_remaining
                    ),
                    self.seconds_remaining,
                )

                await asyncio.sleep(interval)
                self.seconds_remaining = max(
                    0,
                    self.seconds_remaining - interval,
                )

                if self.finished:
                    return

                if self.seconds_remaining == 0:
                    await self.finish_poll()
                    return

                if self.message is not None:
                    try:
                        await self.message.edit(
                            embed=self.build_embed(),
                            view=self,
                        )
                    except discord.HTTPException:
                        logger.exception(
                            "Failed to update poll timer."
                        )

        except asyncio.CancelledError:
            return

    async def finish_poll(
        self,
        ended_early: bool = False,
    ) -> None:
        if self.finished:
            return

        self.finished = True
        self.seconds_remaining = 0
        self.stop()

        if (
            self.countdown_task is not None
            and self.countdown_task is not asyncio.current_task()
        ):
            self.countdown_task.cancel()

        for item in self.children:
            item.disabled = True

        if self.message is None:
            return

        try:
            await self.message.edit(
                embed=self.build_embed(
                    final=True,
                    ended_early=ended_early,
                ),
                view=self,
            )

        except discord.HTTPException:
            logger.exception(
                "Failed to close a poll."
            )

    async def on_timeout(self):
        await self.finish_poll()


# =========================================================
# SUGGESTIONS
# =========================================================

def configured_suggestions_channel_id() -> int | None:
    if not SUGGESTIONS_CHANNEL_ID:
        return None

    try:
        return int(SUGGESTIONS_CHANNEL_ID)

    except ValueError:
        logger.warning(
            "SUGGESTIONS_CHANNEL_ID must contain only numbers."
        )
        return None


def get_suggestions_channel(
    guild: discord.Guild,
) -> discord.TextChannel | None:
    channel_id = configured_suggestions_channel_id()

    if channel_id is None:
        return None

    channel = guild.get_channel(channel_id)

    if isinstance(channel, discord.TextChannel):
        return channel

    return None


class SuggestionView(discord.ui.View):
    def __init__(
        self,
        author: discord.Member | discord.User,
        suggestion_text: str,
    ):
        super().__init__(timeout=None)

        self.author = author
        self.suggestion_text = suggestion_text
        self.upvotes: set[int] = set()
        self.downvotes: set[int] = set()
        self.status = "Pending"
        self.message: discord.Message | None = None

    def build_embed(self) -> discord.Embed:
        status_icons = {
            "Pending": "🟡",
            "Accepted": "🟢",
            "Rejected": "🔴",
        }

        embed = discord.Embed(
            title="💡 777 Suggestion",
            description=self.suggestion_text,
            colour=GOLD_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

        embed.set_author(
            name=self.author.display_name,
            icon_url=self.author.display_avatar.url,
        )

        embed.add_field(
            name="Status",
            value=(
                f"{status_icons.get(self.status, '🟡')} "
                f"**{self.status}**"
            ),
            inline=True,
        )

        embed.add_field(
            name="Votes",
            value=(
                f"👍 `{len(self.upvotes)}`\n"
                f"👎 `{len(self.downvotes)}`"
            ),
            inline=True,
        )

        embed.set_footer(
            text=f"Suggested by {self.author.display_name} • 777"
        )

        return embed

    async def update_message(self) -> None:
        if self.message is None:
            return

        try:
            await self.message.edit(
                embed=self.build_embed(),
                view=self,
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to update suggestion."
            )

    async def register_vote(
        self,
        interaction: discord.Interaction,
        upvote: bool,
    ) -> None:
        if self.status != "Pending":
            await interaction.response.send_message(
                "Voting is closed because this suggestion has been reviewed.",
                ephemeral=True,
            )
            return

        user_id = interaction.user.id

        if upvote:
            self.downvotes.discard(user_id)
            self.upvotes.add(user_id)
            vote_text = "👍 upvoted"
        else:
            self.upvotes.discard(user_id)
            self.downvotes.add(user_id)
            vote_text = "👎 downvoted"

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

        await interaction.followup.send(
            f"You {vote_text} this suggestion.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Upvote",
        emoji="👍",
        style=discord.ButtonStyle.success,
        custom_id="777_suggestion_upvote",
    )
    async def upvote_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.register_vote(interaction, True)

    @discord.ui.button(
        label="Downvote",
        emoji="👎",
        style=discord.ButtonStyle.danger,
        custom_id="777_suggestion_downvote",
    )
    async def downvote_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.register_vote(interaction, False)

    async def review(
        self,
        interaction: discord.Interaction,
        new_status: str,
    ) -> None:
        permissions = getattr(
            interaction.user,
            "guild_permissions",
            None,
        )

        if not permissions or not permissions.manage_messages:
            await interaction.response.send_message(
                "Only moderators can review suggestions.",
                ephemeral=True,
            )
            return

        self.status = new_status

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

        await interaction.followup.send(
            f"Suggestion marked as **{new_status}**.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Accept",
        emoji="✅",
        style=discord.ButtonStyle.primary,
        custom_id="777_suggestion_accept",
        row=1,
    )
    async def accept_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.review(interaction, "Accepted")

    @discord.ui.button(
        label="Reject",
        emoji="⛔",
        style=discord.ButtonStyle.secondary,
        custom_id="777_suggestion_reject",
        row=1,
    )
    async def reject_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.review(interaction, "Rejected")


# =========================================================
# GIVEAWAYS
# =========================================================

class GiveawayEnterButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Enter Giveaway",
            emoji="🎉",
            style=discord.ButtonStyle.success,
            custom_id="777_giveaway_enter",
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        view = self.view

        if not isinstance(view, GiveawayView):
            await interaction.response.send_message(
                "This giveaway is no longer available.",
                ephemeral=True,
            )
            return

        await view.toggle_entry(interaction)


class GiveawayEndButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="End Giveaway",
            emoji="🛑",
            style=discord.ButtonStyle.danger,
            custom_id="777_giveaway_end",
            row=1,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        view = self.view

        if not isinstance(view, GiveawayView):
            await interaction.response.send_message(
                "This giveaway is no longer available.",
                ephemeral=True,
            )
            return

        if interaction.user.id != view.creator.id:
            permissions = getattr(
                interaction.user,
                "guild_permissions",
                None,
            )

            if not permissions or not permissions.manage_messages:
                await interaction.response.send_message(
                    "Only the giveaway creator or a moderator can end it.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True)
        await view.finish_giveaway(ended_early=True)

        await interaction.followup.send(
            "The giveaway has been ended.",
            ephemeral=True,
        )


class GiveawayRerollButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Reroll",
            emoji="🔁",
            style=discord.ButtonStyle.primary,
            custom_id="777_giveaway_reroll",
            row=1,
            disabled=True,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        view = self.view

        if not isinstance(view, GiveawayView):
            await interaction.response.send_message(
                "This giveaway is no longer available.",
                ephemeral=True,
            )
            return

        permissions = getattr(
            interaction.user,
            "guild_permissions",
            None,
        )

        if (
            interaction.user.id != view.creator.id
            and (
                not permissions
                or not permissions.manage_messages
            )
        ):
            await interaction.response.send_message(
                "Only the giveaway creator or a moderator can reroll.",
                ephemeral=True,
            )
            return

        if not view.finished:
            await interaction.response.send_message(
                "The giveaway must end before it can be rerolled.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        winners = await view.select_winners()

        if not winners:
            await interaction.followup.send(
                "There are no eligible entrants to reroll.",
                ephemeral=True,
            )
            return

        winner_mentions = ", ".join(
            winner.mention
            for winner in winners
        )

        if view.message is not None:
            await view.message.reply(
                f"🔁 **Giveaway reroll:** {winner_mentions} won "
                f"**{view.prize}**!",
                allowed_mentions=discord.AllowedMentions(
                    users=True,
                    roles=False,
                    everyone=False,
                ),
            )

        await interaction.followup.send(
            f"Rerolled winner(s): {winner_mentions}",
            ephemeral=True,
        )


class GiveawayView(discord.ui.View):
    def __init__(
        self,
        prize: str,
        creator: discord.Member | discord.User,
        winner_count: int,
        duration_seconds: int,
        required_role: discord.Role | None = None,
    ):
        super().__init__(timeout=duration_seconds)

        self.prize = prize
        self.creator = creator
        self.winner_count = winner_count
        self.duration_seconds = duration_seconds
        self.required_role = required_role

        self.entries: set[int] = set()
        self.message: discord.Message | None = None
        self.finished = False
        self.seconds_remaining = duration_seconds
        self.ends_at_monotonic = time.monotonic() + duration_seconds
        self.ends_at_unix = int(
            datetime.now(timezone.utc).timestamp()
        ) + duration_seconds
        self.countdown_task: asyncio.Task | None = None
        self.last_winners: list[discord.Member] = []

        self.add_item(GiveawayEnterButton())
        self.add_item(GiveawayEndButton())
        self.add_item(GiveawayRerollButton())

    def build_embed(
        self,
        final: bool = False,
        ended_early: bool = False,
    ) -> discord.Embed:
        if final:
            if self.last_winners:
                winner_mentions = ", ".join(
                    winner.mention
                    for winner in self.last_winners
                )

                result_text = (
                    f"🎉 **Winner{'s' if len(self.last_winners) != 1 else ''}:** "
                    f"{winner_mentions}"
                )
            else:
                result_text = "No eligible entrants joined."

            ending_text = (
                "The giveaway was ended early."
                if ended_early
                else "The giveaway has ended."
            )

            description = (
                f"**Prize:** {self.prize}\n\n"
                f"{ending_text}\n"
                f"{result_text}"
            )

            title = "🎉 Giveaway Results"

        else:
            description = (
                f"**Prize:** {self.prize}\n\n"
                "Press **Enter Giveaway** below to join. "
                "Press it again to leave."
            )

            title = "🎉 777 Giveaway"

        embed = discord.Embed(
            title=title,
            description=description,
            colour=GOLD_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(
            name="Entries",
            value=f"`{len(self.entries)}`",
            inline=True,
        )

        embed.add_field(
            name="Winners",
            value=f"`{self.winner_count}`",
            inline=True,
        )

        embed.add_field(
            name="Hosted By",
            value=self.creator.mention,
            inline=True,
        )

        if self.required_role is not None:
            embed.add_field(
                name="Required Role",
                value=self.required_role.mention,
                inline=True,
            )

        if not final:
            embed.add_field(
                name="Time Remaining",
                value=(
                    f"`{countdown_text(self.seconds_remaining)}`\n"
                    f"Ends <t:{self.ends_at_unix}:R>"
                ),
                inline=True,
            )

        embed.set_footer(
            text=(
                "777 • Giveaway open"
                if not final
                else "777 • Giveaway closed"
            )
        )

        return embed

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.bot:
            await interaction.response.send_message(
                "Bots cannot enter giveaways.",
                ephemeral=True,
            )
            return False

        if self.finished:
            await interaction.response.send_message(
                "This giveaway has already ended.",
                ephemeral=True,
            )
            return False

        return True

    async def toggle_entry(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if (
            self.required_role is not None
            and isinstance(interaction.user, discord.Member)
            and self.required_role not in interaction.user.roles
        ):
            await interaction.response.send_message(
                f"You need the {self.required_role.mention} role to enter.",
                ephemeral=True,
            )
            return

        user_id = interaction.user.id

        if user_id in self.entries:
            self.entries.remove(user_id)
            response_text = "You left the giveaway."
        else:
            self.entries.add(user_id)
            response_text = "You entered the giveaway. Good luck!"

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self,
        )

        await interaction.followup.send(
            response_text,
            ephemeral=True,
        )

    async def start_countdown(self) -> None:
        if (
            self.countdown_task is None
            or self.countdown_task.done()
        ):
            self.countdown_task = asyncio.create_task(
                self.countdown_loop(),
                name=(
                    f"777-giveaway-"
                    f"{self.message.id if self.message else 'pending'}"
                ),
            )

            logger.info(
                "Started giveaway countdown for %s seconds.",
                self.duration_seconds,
            )

    async def countdown_loop(self) -> None:
        try:
            last_displayed = None

            while not self.finished:
                remaining = max(
                    0,
                    int(
                        self.ends_at_monotonic
                        - time.monotonic()
                        + 0.999
                    ),
                )

                self.seconds_remaining = remaining

                if remaining <= 0:
                    await self.finish_giveaway()
                    return

                if (
                    self.message is not None
                    and remaining != last_displayed
                ):
                    try:
                        await self.message.edit(
                            embed=self.build_embed(),
                            view=self,
                        )
                        last_displayed = remaining

                    except discord.NotFound:
                        self.finished = True
                        self.stop()
                        return

                    except discord.HTTPException:
                        logger.warning(
                            "Giveaway timer update was rate limited "
                            "or temporarily failed; it will retry."
                        )

                # Update every second during the final minute.
                # Earlier updates are spaced out to avoid Discord rate limits.
                if remaining <= 60:
                    sleep_for = 1
                elif remaining <= 300:
                    sleep_for = 5
                else:
                    sleep_for = 15

                await asyncio.sleep(
                    min(sleep_for, remaining)
                )

        except asyncio.CancelledError:
            return

        except Exception:
            logger.exception(
                "Unexpected error in giveaway countdown."
            )

            if not self.finished:
                await self.finish_giveaway()

    async def select_winners(
        self,
    ) -> list[discord.Member]:
        if self.message is None or self.message.guild is None:
            return []

        eligible_members = []

        for user_id in self.entries:
            member = self.message.guild.get_member(user_id)

            if member is None or member.bot:
                continue

            if (
                self.required_role is not None
                and self.required_role not in member.roles
            ):
                continue

            eligible_members.append(member)

        if not eligible_members:
            return []

        amount = min(
            self.winner_count,
            len(eligible_members),
        )

        return random.sample(
            eligible_members,
            k=amount,
        )

    async def finish_giveaway(
        self,
        ended_early: bool = False,
    ) -> None:
        if self.finished:
            return

        self.finished = True
        self.seconds_remaining = 0
        self.ends_at_monotonic = time.monotonic()
        self.stop()

        if (
            self.countdown_task is not None
            and self.countdown_task is not asyncio.current_task()
        ):
            self.countdown_task.cancel()

        self.last_winners = await self.select_winners()

        for item in self.children:
            if isinstance(item, GiveawayRerollButton):
                item.disabled = not bool(self.entries)
            else:
                item.disabled = True

        if self.message is None:
            return

        try:
            await self.message.edit(
                embed=self.build_embed(
                    final=True,
                    ended_early=ended_early,
                ),
                view=self,
            )

            if self.last_winners:
                winner_mentions = ", ".join(
                    winner.mention
                    for winner in self.last_winners
                )

                await self.message.reply(
                    f"🎉 Congratulations {winner_mentions}! "
                    f"You won **{self.prize}**!",
                    allowed_mentions=discord.AllowedMentions(
                        users=True,
                        roles=False,
                        everyone=False,
                    ),
                )

        except discord.HTTPException:
            logger.exception(
                "Failed to close a giveaway."
            )

    async def on_timeout(self):
        await self.finish_giveaway()


# =========================================================
# ECONOMY DATABASE
# =========================================================

economy_lock = threading.RLock()


def economy_connection() -> sqlite3.Connection:
    database_path = Path(ECONOMY_DB_PATH)

    if database_path.parent != Path("."):
        database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    connection = sqlite3.connect(
        database_path,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_economy_database() -> None:
    with economy_lock, economy_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS economy_users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                wallet INTEGER NOT NULL DEFAULT 0,
                bank INTEGER NOT NULL DEFAULT 0,
                total_earned INTEGER NOT NULL DEFAULT 0,
                total_lost INTEGER NOT NULL DEFAULT 0,
                daily_streak INTEGER NOT NULL DEFAULT 0,
                last_daily INTEGER,
                last_work INTEGER,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS economy_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                transaction_type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                metadata TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_economy_leaderboard
            ON economy_users(guild_id, wallet, bank);
            """
        )


def ensure_economy_user(
    guild_id: int,
    user_id: int,
) -> None:
    now = int(time.time())

    with economy_lock, economy_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO economy_users (
                guild_id,
                user_id,
                wallet,
                bank,
                total_earned,
                total_lost,
                daily_streak,
                created_at
            )
            VALUES (?, ?, ?, 0, ?, 0, 0, ?)
            """,
            (
                guild_id,
                user_id,
                ECONOMY_STARTING_BALANCE,
                ECONOMY_STARTING_BALANCE,
                now,
            ),
        )


def get_economy_account(
    guild_id: int,
    user_id: int,
) -> dict:
    ensure_economy_user(guild_id, user_id)

    with economy_lock, economy_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM economy_users
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

    return dict(row)


def record_economy_transaction(
    connection: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    transaction_type: str,
    amount: int,
    balance_after: int,
    metadata: str = "",
) -> None:
    connection.execute(
        """
        INSERT INTO economy_transactions (
            guild_id,
            user_id,
            transaction_type,
            amount,
            balance_after,
            metadata,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            user_id,
            transaction_type,
            amount,
            balance_after,
            metadata,
            int(time.time()),
        ),
    )


def change_wallet(
    guild_id: int,
    user_id: int,
    amount: int,
    transaction_type: str,
    metadata: str = "",
) -> tuple[bool, int]:
    ensure_economy_user(guild_id, user_id)

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        row = connection.execute(
            """
            SELECT wallet, total_earned, total_lost
            FROM economy_users
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

        new_wallet = row["wallet"] + amount

        if new_wallet < 0:
            connection.rollback()
            return False, row["wallet"]

        earned_change = max(amount, 0)
        lost_change = max(-amount, 0)

        connection.execute(
            """
            UPDATE economy_users
            SET wallet = ?,
                total_earned = total_earned + ?,
                total_lost = total_lost + ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                new_wallet,
                earned_change,
                lost_change,
                guild_id,
                user_id,
            ),
        )

        record_economy_transaction(
            connection,
            guild_id,
            user_id,
            transaction_type,
            amount,
            new_wallet,
            metadata,
        )

        connection.commit()
        return True, new_wallet


def transfer_wallet_bank(
    guild_id: int,
    user_id: int,
    amount: int,
    to_bank: bool,
) -> tuple[bool, dict]:
    ensure_economy_user(guild_id, user_id)

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        row = connection.execute(
            """
            SELECT wallet, bank
            FROM economy_users
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

        wallet = row["wallet"]
        bank = row["bank"]

        if to_bank:
            if amount > wallet:
                connection.rollback()
                return False, {"wallet": wallet, "bank": bank}

            wallet -= amount
            bank += amount
            transaction_type = "deposit"
            transaction_amount = -amount

        else:
            if amount > bank:
                connection.rollback()
                return False, {"wallet": wallet, "bank": bank}

            bank -= amount
            wallet += amount
            transaction_type = "withdraw"
            transaction_amount = amount

        connection.execute(
            """
            UPDATE economy_users
            SET wallet = ?, bank = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (wallet, bank, guild_id, user_id),
        )

        record_economy_transaction(
            connection,
            guild_id,
            user_id,
            transaction_type,
            transaction_amount,
            wallet,
            f"bank_balance={bank}",
        )

        connection.commit()
        return True, {"wallet": wallet, "bank": bank}


def pay_economy_user(
    guild_id: int,
    sender_id: int,
    recipient_id: int,
    amount: int,
) -> tuple[bool, str]:
    ensure_economy_user(guild_id, sender_id)
    ensure_economy_user(guild_id, recipient_id)

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        sender = connection.execute(
            """
            SELECT wallet
            FROM economy_users
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, sender_id),
        ).fetchone()

        if sender["wallet"] < amount:
            connection.rollback()
            return False, "insufficient"

        recipient = connection.execute(
            """
            SELECT wallet
            FROM economy_users
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, recipient_id),
        ).fetchone()

        sender_wallet = sender["wallet"] - amount
        recipient_wallet = recipient["wallet"] + amount

        connection.execute(
            """
            UPDATE economy_users
            SET wallet = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (sender_wallet, guild_id, sender_id),
        )

        connection.execute(
            """
            UPDATE economy_users
            SET wallet = ?,
                total_earned = total_earned + ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                recipient_wallet,
                amount,
                guild_id,
                recipient_id,
            ),
        )

        record_economy_transaction(
            connection,
            guild_id,
            sender_id,
            "pay_sent",
            -amount,
            sender_wallet,
            f"recipient={recipient_id}",
        )

        record_economy_transaction(
            connection,
            guild_id,
            recipient_id,
            "pay_received",
            amount,
            recipient_wallet,
            f"sender={sender_id}",
        )

        connection.commit()
        return True, "ok"


def claim_daily_reward(
    guild_id: int,
    user_id: int,
) -> tuple[bool, int, int, int]:
    ensure_economy_user(guild_id, user_id)
    now = int(time.time())
    cooldown = 24 * 60 * 60

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        row = connection.execute(
            """
            SELECT wallet, last_daily, daily_streak
            FROM economy_users
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

        last_daily = row["last_daily"]

        if last_daily and now - last_daily < cooldown:
            remaining = cooldown - (now - last_daily)
            connection.rollback()
            return False, remaining, row["daily_streak"], row["wallet"]

        if last_daily and now - last_daily <= 48 * 60 * 60:
            streak = row["daily_streak"] + 1
        else:
            streak = 1

        streak = min(streak, 30)
        reward = 250 + min(streak * 25, 500)
        new_wallet = row["wallet"] + reward

        connection.execute(
            """
            UPDATE economy_users
            SET wallet = ?,
                total_earned = total_earned + ?,
                daily_streak = ?,
                last_daily = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                new_wallet,
                reward,
                streak,
                now,
                guild_id,
                user_id,
            ),
        )

        record_economy_transaction(
            connection,
            guild_id,
            user_id,
            "daily",
            reward,
            new_wallet,
            f"streak={streak}",
        )

        connection.commit()
        return True, reward, streak, new_wallet


def perform_work(
    guild_id: int,
    user_id: int,
) -> tuple[bool, int, int, str]:
    ensure_economy_user(guild_id, user_id)
    now = int(time.time())
    cooldown = 30 * 60

    jobs = [
        ("delivered Roblox pizzas", 80, 180),
        ("tested a new obby", 100, 220),
        ("moderated a chaotic server", 120, 260),
        ("built a Roblox map", 150, 320),
        ("streamed a gaming session", 90, 240),
        ("won a small tournament", 180, 360),
    ]

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        row = connection.execute(
            """
            SELECT wallet, last_work
            FROM economy_users
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

        last_work = row["last_work"]

        if last_work and now - last_work < cooldown:
            remaining = cooldown - (now - last_work)
            connection.rollback()
            return False, remaining, row["wallet"], ""

        job_name, minimum, maximum = random.choice(jobs)
        reward = random.randint(minimum, maximum)
        new_wallet = row["wallet"] + reward

        connection.execute(
            """
            UPDATE economy_users
            SET wallet = ?,
                total_earned = total_earned + ?,
                last_work = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                new_wallet,
                reward,
                now,
                guild_id,
                user_id,
            ),
        )

        record_economy_transaction(
            connection,
            guild_id,
            user_id,
            "work",
            reward,
            new_wallet,
            job_name,
        )

        connection.commit()
        return True, reward, new_wallet, job_name


def economy_leaderboard(
    guild_id: int,
    limit: int = 10,
) -> list[dict]:
    with economy_lock, economy_connection() as connection:
        rows = connection.execute(
            """
            SELECT user_id, wallet, bank, wallet + bank AS net_worth
            FROM economy_users
            WHERE guild_id = ?
            ORDER BY net_worth DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()

    return [dict(row) for row in rows]


def format_coins(amount: int) -> str:
    return f"{amount:,} 🪙"


def format_cooldown(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}h {minutes}m {seconds}s"

    if minutes:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


# =========================================================
# PREMIUM SLOTS
# =========================================================

SLOT_SYMBOLS = [
    {
        "key": "cherry",
        "emoji": "🍒",
        "label": "CHERRY",
        "weight": 28,
        "triple": 3,
    },
    {
        "key": "lemon",
        "emoji": "🍋",
        "label": "LEMON",
        "weight": 24,
        "triple": 4,
    },
    {
        "key": "grape",
        "emoji": "🍇",
        "label": "GRAPE",
        "weight": 20,
        "triple": 5,
    },
    {
        "key": "bell",
        "emoji": "🔔",
        "label": "BELL",
        "weight": 14,
        "triple": 8,
    },
    {
        "key": "diamond",
        "emoji": "💎",
        "label": "DIAMOND",
        "weight": 9,
        "triple": 15,
    },
    {
        "key": "seven",
        "emoji": "7",
        "label": "SEVEN",
        "weight": 5,
        "triple": 30,
    },
]


def spin_slot_reels() -> list[dict]:
    return random.choices(
        SLOT_SYMBOLS,
        weights=[symbol["weight"] for symbol in SLOT_SYMBOLS],
        k=3,
    )


def calculate_slot_multiplier(
    reels: list[dict],
) -> tuple[float, str]:
    keys = [symbol["key"] for symbol in reels]

    if len(set(keys)) == 1:
        symbol = reels[0]
        multiplier = float(symbol["triple"])

        if symbol["key"] == "seven":
            return multiplier, "777 JACKPOT"

        return multiplier, f"TRIPLE {symbol['label']}"

    for symbol_key in set(keys):
        if keys.count(symbol_key) == 2:
            matching = next(
                symbol
                for symbol in SLOT_SYMBOLS
                if symbol["key"] == symbol_key
            )

            pair_multipliers = {
                "cherry": 1.2,
                "lemon": 1.25,
                "grape": 1.35,
                "bell": 1.5,
                "diamond": 2.0,
                "seven": 3.0,
            }

            return (
                pair_multipliers[matching["key"]],
                f"PAIR OF {matching['label']}S",
            )

    if "cherry" in keys:
        return 0.35, "CHERRY REFUND"

    return 0.0, "NO WIN"


def get_slot_font(
    size: int,
    bold: bool = False,
) -> ImageFont.ImageFont:
    candidates = [
        (
            "/usr/share/fonts/truetype/dejavu/"
            + (
                "DejaVuSans-Bold.ttf"
                if bold
                else "DejaVuSans.ttf"
            )
        ),
        (
            "/usr/share/fonts/truetype/liberation2/"
            + (
                "LiberationSans-Bold.ttf"
                if bold
                else "LiberationSans-Regular.ttf"
            )
        ),
    ]

    for font_path in candidates:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size)

    return ImageFont.load_default()


def create_slots_image(
    player_name: str,
    reels: list[dict],
    bet: int,
    payout: int,
    multiplier: float,
    balance: int,
    result_name: str,
) -> io.BytesIO:
    width = 1200
    height = 720
    image = Image.new(
        "RGB",
        (width, height),
        (7, 7, 10),
    )
    draw = ImageDraw.Draw(image)

    gold = (245, 190, 45)
    light_gold = (255, 225, 120)
    dark_gold = (105, 72, 10)
    panel = (19, 19, 25)
    reel_background = (238, 232, 212)
    reel_text = (20, 18, 15)
    muted = (170, 170, 178)

    # Border and machine shell.
    draw.rounded_rectangle(
        (30, 30, width - 30, height - 30),
        radius=38,
        fill=panel,
        outline=gold,
        width=8,
    )

    draw.rounded_rectangle(
        (65, 70, width - 65, 190),
        radius=28,
        fill=(10, 10, 14),
        outline=dark_gold,
        width=4,
    )

    title_font = get_slot_font(68, bold=True)
    subtitle_font = get_slot_font(28, bold=True)
    symbol_font = get_slot_font(92, bold=True)
    label_font = get_slot_font(24, bold=True)
    info_font = get_slot_font(30, bold=True)
    small_font = get_slot_font(23, bold=False)

    draw.text(
        (width // 2, 112),
        "777 ROYALE SLOTS",
        font=title_font,
        fill=light_gold,
        anchor="mm",
    )

    draw.text(
        (width // 2, 165),
        result_name,
        font=subtitle_font,
        fill=gold,
        anchor="mm",
    )

    reel_width = 290
    reel_height = 260
    gap = 34
    total_reel_width = reel_width * 3 + gap * 2
    start_x = (width - total_reel_width) // 2
    reel_y = 230

    for index, symbol in enumerate(reels):
        x1 = start_x + index * (reel_width + gap)
        x2 = x1 + reel_width

        draw.rounded_rectangle(
            (x1, reel_y, x2, reel_y + reel_height),
            radius=30,
            fill=reel_background,
            outline=gold,
            width=7,
        )

        display_symbol = (
            "7"
            if symbol["key"] == "seven"
            else symbol["emoji"]
        )

        draw.text(
            ((x1 + x2) // 2, reel_y + 105),
            display_symbol,
            font=symbol_font,
            fill=(
                (190, 20, 25)
                if symbol["key"] == "seven"
                else reel_text
            ),
            anchor="mm",
            embedded_color=True,
        )

        draw.text(
            ((x1 + x2) // 2, reel_y + 215),
            symbol["label"],
            font=label_font,
            fill=dark_gold,
            anchor="mm",
        )

    # Winning line.
    line_y = reel_y + reel_height // 2
    draw.line(
        (start_x - 18, line_y, start_x + total_reel_width + 18, line_y),
        fill=(220, 35, 45),
        width=6,
    )

    info_y = 545
    columns = [
        ("BET", format_coins(bet)),
        (
            "PAYOUT",
            format_coins(payout),
        ),
        (
            "MULTIPLIER",
            f"{multiplier:g}×",
        ),
        (
            "BALANCE",
            format_coins(balance),
        ),
    ]

    column_width = (width - 120) // len(columns)

    for index, (label, value) in enumerate(columns):
        center_x = 60 + column_width * index + column_width // 2

        draw.text(
            (center_x, info_y),
            label,
            font=small_font,
            fill=muted,
            anchor="mm",
        )

        draw.text(
            (center_x, info_y + 44),
            value,
            font=info_font,
            fill=light_gold,
            anchor="mm",
        )

    draw.text(
        (width // 2, 665),
        f"PLAYER: {player_name[:32]}  •  PLAY RESPONSIBLY  •  777",
        font=small_font,
        fill=muted,
        anchor="mm",
    )

    output = io.BytesIO()
    image.save(
        output,
        format="PNG",
        optimize=True,
    )
    output.seek(0)
    return output


def slots_spin_embed(
    player: discord.Member | discord.User,
    bet: int,
    frame: list[str],
    stage: int,
) -> discord.Embed:
    display = " │ ".join(frame)

    embed = discord.Embed(
        title="🎰 777 Royale Slots",
        description=(
            f"### `{display}`\n\n"
            f"Spinning reel **{stage}/3**..."
        ),
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )

    embed.set_author(
        name=player.display_name,
        icon_url=player.display_avatar.url,
    )

    embed.add_field(
        name="Bet",
        value=format_coins(bet),
        inline=True,
    )

    embed.add_field(
        name="Status",
        value="The reels are spinning...",
        inline=True,
    )

    embed.set_footer(
        text="777 • Royale Casino"
    )
    return embed


class SlotsReplayView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        bet: int,
    ):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.bet = bet

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the player who started this spin can use these buttons.",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(
        label="Spin Again",
        emoji="🎰",
        style=discord.ButtonStyle.success,
    )
    async def spin_again(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await execute_slots_spin(
            interaction,
            self.bet,
            from_button=True,
        )

    @discord.ui.button(
        label="Double Bet",
        emoji="⚡",
        style=discord.ButtonStyle.primary,
    )
    async def double_bet(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        doubled_bet = min(
            self.bet * 2,
            SLOTS_MAX_BET,
        )

        await execute_slots_spin(
            interaction,
            doubled_bet,
            from_button=True,
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


slots_user_locks: dict[tuple[int, int], asyncio.Lock] = {}


def get_slots_lock(
    guild_id: int,
    user_id: int,
) -> asyncio.Lock:
    key = (guild_id, user_id)

    if key not in slots_user_locks:
        slots_user_locks[key] = asyncio.Lock()

    return slots_user_locks[key]


async def execute_slots_spin(
    interaction: discord.Interaction,
    bet: int,
    from_button: bool = False,
) -> None:
    if interaction.guild is None:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Slots can only be played inside a server.",
                ephemeral=True,
            )
        return

    if bet < SLOTS_MIN_BET or bet > SLOTS_MAX_BET:
        message = (
            f"Your bet must be between "
            f"{format_coins(SLOTS_MIN_BET)} and "
            f"{format_coins(SLOTS_MAX_BET)}."
        )

        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
            )
        return

    lock = get_slots_lock(
        interaction.guild.id,
        interaction.user.id,
    )

    if lock.locked():
        message = "Your previous slots spin is still running."

        if interaction.response.is_done():
            await interaction.followup.send(
                message,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                message,
                ephemeral=True,
            )
        return

    async with lock:
        if not interaction.response.is_done():
            await interaction.response.defer()

        account = await asyncio.to_thread(
            get_economy_account,
            interaction.guild.id,
            interaction.user.id,
        )

        if account["wallet"] < bet:
            await interaction.followup.send(
                f"You need {format_coins(bet)} in your wallet, "
                f"but you only have {format_coins(account['wallet'])}.",
                ephemeral=True,
            )
            return

        charged, balance_after_bet = await asyncio.to_thread(
            change_wallet,
            interaction.guild.id,
            interaction.user.id,
            -bet,
            "slots_bet",
            f"bet={bet}",
        )

        if not charged:
            await interaction.followup.send(
                "Your balance changed before the spin could start. Try again.",
                ephemeral=True,
            )
            return

        hidden = ["❔", "❔", "❔"]

        try:
            await interaction.edit_original_response(
                embed=slots_spin_embed(
                    interaction.user,
                    bet,
                    hidden,
                    0,
                ),
                attachments=[],
                view=None,
            )
        except discord.HTTPException:
            pass

        reels = spin_slot_reels()

        for stage in range(1, 4):
            frame = [
                reels[index]["emoji"]
                if index < stage
                else random.choice(
                    ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]
                )
                for index in range(3)
            ]

            await asyncio.sleep(0.75)

            try:
                await interaction.edit_original_response(
                    embed=slots_spin_embed(
                        interaction.user,
                        bet,
                        frame,
                        stage,
                    ),
                    attachments=[],
                    view=None,
                )
            except discord.HTTPException:
                logger.warning(
                    "A slots animation frame could not be displayed."
                )

        multiplier, result_name = calculate_slot_multiplier(
            reels
        )
        payout = int(round(bet * multiplier))
        profit = payout - bet

        if payout > 0:
            _, final_balance = await asyncio.to_thread(
                change_wallet,
                interaction.guild.id,
                interaction.user.id,
                payout,
                "slots_payout",
                (
                    f"bet={bet};multiplier={multiplier};"
                    f"result={result_name}"
                ),
            )
        else:
            final_balance = balance_after_bet

        result_image = await asyncio.to_thread(
            create_slots_image,
            interaction.user.display_name,
            reels,
            bet,
            payout,
            multiplier,
            final_balance,
            result_name,
        )

        filename = (
            f"777_slots_{interaction.user.id}_"
            f"{int(time.time())}.png"
        )
        file = discord.File(
            result_image,
            filename=filename,
        )

        if profit > 0:
            outcome = (
                f"## 🎉 You won {format_coins(profit)} profit!"
            )
        elif profit == 0:
            outcome = "## 🤝 You broke even."
        else:
            outcome = (
                f"## 💸 You lost {format_coins(abs(profit))}."
            )

        result_embed = discord.Embed(
            title=(
                "💎 777 JACKPOT!"
                if result_name == "777 JACKPOT"
                else "🎰 777 Royale Slots"
            ),
            description=(
                f"{outcome}\n\n"
                f"**Result:** {result_name}"
            ),
            colour=GOLD_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

        result_embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url,
        )

        result_embed.set_image(
            url=f"attachment://{filename}"
        )

        result_embed.add_field(
            name="Bet",
            value=format_coins(bet),
            inline=True,
        )

        result_embed.add_field(
            name="Payout",
            value=format_coins(payout),
            inline=True,
        )

        result_embed.add_field(
            name="Multiplier",
            value=f"`{multiplier:g}×`",
            inline=True,
        )

        result_embed.add_field(
            name="New Wallet",
            value=format_coins(final_balance),
            inline=False,
        )

        result_embed.set_footer(
            text="777 • Royale Casino • Play responsibly"
        )

        replay_view = SlotsReplayView(
            owner_id=interaction.user.id,
            bet=bet,
        )

        try:
            await interaction.edit_original_response(
                embed=result_embed,
                attachments=[file],
                view=replay_view,
            )

        except discord.HTTPException:
            logger.exception(
                "Failed to display slots result."
            )

            await interaction.followup.send(
                embed=result_embed,
                file=file,
                view=replay_view,
            )


initialize_economy_database()



# =========================================================
# BLACKJACK
# =========================================================

BLACKJACK_MIN_BET = int(
    os.getenv("BLACKJACK_MIN_BET", str(SLOTS_MIN_BET))
)
BLACKJACK_MAX_BET = int(
    os.getenv("BLACKJACK_MAX_BET", str(SLOTS_MAX_BET))
)

CARD_SUITS = ["♠", "♥", "♦", "♣"]
CARD_RANKS = [
    "A", "2", "3", "4", "5", "6", "7",
    "8", "9", "10", "J", "Q", "K",
]


def build_blackjack_deck() -> list[tuple[str, str]]:
    deck = [
        (rank, suit)
        for suit in CARD_SUITS
        for rank in CARD_RANKS
    ]
    random.shuffle(deck)
    return deck


def blackjack_hand_value(
    hand: list[tuple[str, str]],
) -> int:
    value = 0
    aces = 0

    for rank, _ in hand:
        if rank in {"J", "Q", "K"}:
            value += 10
        elif rank == "A":
            value += 11
            aces += 1
        else:
            value += int(rank)

    while value > 21 and aces:
        value -= 10
        aces -= 1

    return value


def blackjack_is_natural(
    hand: list[tuple[str, str]],
) -> bool:
    return (
        len(hand) == 2
        and blackjack_hand_value(hand) == 21
    )


def blackjack_card_text(
    card: tuple[str, str],
) -> str:
    return f"{card[0]}{card[1]}"


def blackjack_hand_text(
    hand: list[tuple[str, str]],
    hide_first: bool = False,
) -> str:
    cards = []

    for index, card in enumerate(hand):
        if hide_first and index == 0:
            cards.append("🂠")
        else:
            cards.append(blackjack_card_text(card))

    return "  ".join(cards)


def draw_blackjack_card(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    card: tuple[str, str] | None,
    hidden: bool = False,
) -> None:
    x, y = position
    width = 145
    height = 205
    gold = (230, 180, 45)

    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=18,
        fill=(245, 242, 228),
        outline=gold,
        width=5,
    )

    if hidden or card is None:
        draw.rounded_rectangle(
            (x + 12, y + 12, x + width - 12, y + height - 12),
            radius=14,
            fill=(20, 20, 28),
            outline=(120, 85, 15),
            width=3,
        )

        small = get_slot_font(26, bold=True)
        large = get_slot_font(64, bold=True)

        draw.text(
            (x + width // 2, y + 58),
            "777",
            font=large,
            fill=gold,
            anchor="mm",
        )
        draw.text(
            (x + width // 2, y + 145),
            "ROYAL",
            font=small,
            fill=gold,
            anchor="mm",
        )
        return

    rank, suit = card
    red = suit in {"♥", "♦"}
    colour = (190, 25, 35) if red else (18, 18, 22)

    rank_font = get_slot_font(36, bold=True)
    suit_font = get_slot_font(54, bold=True)
    center_font = get_slot_font(78, bold=True)

    draw.text(
        (x + 18, y + 18),
        rank,
        font=rank_font,
        fill=colour,
    )
    draw.text(
        (x + 20, y + 58),
        suit,
        font=suit_font,
        fill=colour,
    )
    draw.text(
        (x + width // 2, y + height // 2 + 16),
        suit,
        font=center_font,
        fill=colour,
        anchor="mm",
    )


def create_blackjack_image(
    player_name: str,
    player_hand: list[tuple[str, str]],
    dealer_hand: list[tuple[str, str]],
    bet: int,
    balance: int,
    status: str,
    hide_dealer: bool,
) -> io.BytesIO:
    width = 1400
    height = 820

    image = Image.new(
        "RGB",
        (width, height),
        (6, 8, 8),
    )
    draw = ImageDraw.Draw(image)

    gold = (235, 185, 45)
    felt = (16, 73, 51)
    felt_dark = (7, 42, 30)
    light_gold = (255, 225, 135)
    muted = (195, 198, 190)

    draw.rounded_rectangle(
        (28, 28, width - 28, height - 28),
        radius=42,
        fill=(18, 18, 23),
        outline=gold,
        width=8,
    )

    draw.rounded_rectangle(
        (60, 72, width - 60, height - 65),
        radius=110,
        fill=felt,
        outline=(100, 76, 15),
        width=6,
    )

    draw.rounded_rectangle(
        (96, 106, width - 96, height - 100),
        radius=90,
        outline=felt_dark,
        width=5,
    )

    title_font = get_slot_font(56, bold=True)
    header_font = get_slot_font(30, bold=True)
    info_font = get_slot_font(27, bold=True)
    small_font = get_slot_font(22, bold=False)

    draw.text(
        (width // 2, 74),
        "777 ROYALE BLACKJACK",
        font=title_font,
        fill=light_gold,
        anchor="mm",
    )

    draw.text(
        (width // 2, 132),
        status,
        font=header_font,
        fill=gold,
        anchor="mm",
    )

    dealer_value = (
        "?"
        if hide_dealer
        else str(blackjack_hand_value(dealer_hand))
    )

    draw.text(
        (120, 182),
        f"DEALER  •  VALUE {dealer_value}",
        font=header_font,
        fill=(245, 245, 240),
    )

    card_gap = 165
    dealer_start = 150

    for index, card in enumerate(dealer_hand):
        draw_blackjack_card(
            draw,
            (dealer_start + index * card_gap, 225),
            card,
            hidden=(hide_dealer and index == 0),
        )

    player_value = blackjack_hand_value(player_hand)

    draw.text(
        (120, 485),
        f"{player_name[:28].upper()}  •  VALUE {player_value}",
        font=header_font,
        fill=(245, 245, 240),
    )

    for index, card in enumerate(player_hand):
        draw_blackjack_card(
            draw,
            (dealer_start + index * card_gap, 525),
            card,
        )

    panel_x = 1040

    draw.rounded_rectangle(
        (panel_x, 215, 1290, 630),
        radius=28,
        fill=(12, 31, 24),
        outline=gold,
        width=4,
    )

    details = [
        ("BET", format_coins(bet)),
        ("BALANCE", format_coins(balance)),
        ("PLAYER", str(player_value)),
        ("DEALER", dealer_value),
    ]

    detail_y = 270

    for label, value in details:
        draw.text(
            (1165, detail_y),
            label,
            font=small_font,
            fill=muted,
            anchor="mm",
        )
        draw.text(
            (1165, detail_y + 38),
            value,
            font=info_font,
            fill=light_gold,
            anchor="mm",
        )
        detail_y += 88

    draw.text(
        (width // 2, 772),
        "BLACKJACK PAYS 3:2  •  DEALER STANDS ON 17  •  777",
        font=small_font,
        fill=muted,
        anchor="mm",
    )

    output = io.BytesIO()
    image.save(
        output,
        format="PNG",
        optimize=True,
    )
    output.seek(0)
    return output


active_blackjack_games: dict[
    tuple[int, int],
    "BlackjackView",
] = {}


class BlackjackView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        player_id: int,
        player_name: str,
        bet: int,
        deck: list[tuple[str, str]],
        player_hand: list[tuple[str, str]],
        dealer_hand: list[tuple[str, str]],
        balance_after_bet: int,
    ):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.player_id = player_id
        self.player_name = player_name
        self.bet = bet
        self.deck = deck
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.balance_after_bet = balance_after_bet
        self.message: discord.Message | None = None
        self.finished = False
        self.action_lock = asyncio.Lock()

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message(
                "This blackjack table belongs to another player.",
                ephemeral=True,
            )
            return False

        return True

    def disable_all(self) -> None:
        for item in self.children:
            item.disabled = True

    async def render(
        self,
        interaction: discord.Interaction,
        status: str,
        hide_dealer: bool = True,
        result_description: str | None = None,
    ) -> None:
        image = await asyncio.to_thread(
            create_blackjack_image,
            self.player_name,
            self.player_hand,
            self.dealer_hand,
            self.bet,
            self.balance_after_bet,
            status,
            hide_dealer,
        )

        filename = (
            f"777_blackjack_{self.player_id}_"
            f"{int(time.time())}.png"
        )

        file = discord.File(
            image,
            filename=filename,
        )

        embed = discord.Embed(
            title="🃏 777 Royale Blackjack",
            description=(
                result_description
                or (
                    f"**Your hand:** "
                    f"{blackjack_hand_text(self.player_hand)}\n"
                    f"**Dealer:** "
                    f"{blackjack_hand_text(self.dealer_hand, True)}"
                )
            ),
            colour=GOLD_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

        embed.set_image(
            url=f"attachment://{filename}"
        )

        embed.add_field(
            name="Bet",
            value=format_coins(self.bet),
            inline=True,
        )

        embed.add_field(
            name="Your Value",
            value=str(
                blackjack_hand_value(self.player_hand)
            ),
            inline=True,
        )

        embed.add_field(
            name="Wallet",
            value=format_coins(self.balance_after_bet),
            inline=True,
        )

        embed.set_footer(
            text="777 • Hit, Stand, or Double Down"
        )

        await interaction.edit_original_response(
            embed=embed,
            attachments=[file],
            view=self,
        )

    async def finish_game(
        self,
        interaction: discord.Interaction,
        outcome: str,
        payout: int,
        status: str,
    ) -> None:
        if self.finished:
            return

        self.finished = True
        self.disable_all()
        self.stop()

        if payout > 0:
            _, self.balance_after_bet = await asyncio.to_thread(
                change_wallet,
                self.guild_id,
                self.player_id,
                payout,
                "blackjack_payout",
                (
                    f"bet={self.bet};"
                    f"outcome={outcome};"
                    f"payout={payout}"
                ),
            )

        player_value = blackjack_hand_value(
            self.player_hand
        )
        dealer_value = blackjack_hand_value(
            self.dealer_hand
        )

        image = await asyncio.to_thread(
            create_blackjack_image,
            self.player_name,
            self.player_hand,
            self.dealer_hand,
            self.bet,
            self.balance_after_bet,
            status,
            False,
        )

        filename = (
            f"777_blackjack_final_{self.player_id}_"
            f"{int(time.time())}.png"
        )

        file = discord.File(
            image,
            filename=filename,
        )

        net = payout - self.bet

        if net > 0:
            result_line = (
                f"## 🎉 You won {format_coins(net)} profit!"
            )
        elif net == 0:
            result_line = "## 🤝 Push — your bet was returned."
        else:
            result_line = (
                f"## 💸 You lost {format_coins(abs(net))}."
            )

        embed = discord.Embed(
            title="🃏 Blackjack Result",
            description=(
                f"{result_line}\n\n"
                f"**Outcome:** {outcome}\n"
                f"**Your hand:** "
                f"{blackjack_hand_text(self.player_hand)} "
                f"(**{player_value}**)\n"
                f"**Dealer hand:** "
                f"{blackjack_hand_text(self.dealer_hand)} "
                f"(**{dealer_value}**)"
            ),
            colour=GOLD_COLOUR,
            timestamp=datetime.now(timezone.utc),
        )

        embed.set_image(
            url=f"attachment://{filename}"
        )

        embed.add_field(
            name="Bet",
            value=format_coins(self.bet),
            inline=True,
        )

        embed.add_field(
            name="Payout",
            value=format_coins(payout),
            inline=True,
        )

        embed.add_field(
            name="New Wallet",
            value=format_coins(self.balance_after_bet),
            inline=True,
        )

        embed.set_footer(
            text="777 • Royale Casino • Play responsibly"
        )

        try:
            await interaction.edit_original_response(
                embed=embed,
                attachments=[file],
                view=self,
            )
        finally:
            active_blackjack_games.pop(
                (self.guild_id, self.player_id),
                None,
            )

    async def dealer_play_and_finish(
        self,
        interaction: discord.Interaction,
    ) -> None:
        while blackjack_hand_value(self.dealer_hand) < 17:
            await asyncio.sleep(0.65)
            self.dealer_hand.append(self.deck.pop())

        player_value = blackjack_hand_value(
            self.player_hand
        )
        dealer_value = blackjack_hand_value(
            self.dealer_hand
        )

        if dealer_value > 21:
            await self.finish_game(
                interaction,
                "Dealer busted",
                self.bet * 2,
                "DEALER BUST",
            )
        elif player_value > dealer_value:
            await self.finish_game(
                interaction,
                "You beat the dealer",
                self.bet * 2,
                "PLAYER WINS",
            )
        elif player_value == dealer_value:
            await self.finish_game(
                interaction,
                "Push",
                self.bet,
                "PUSH",
            )
        else:
            await self.finish_game(
                interaction,
                "Dealer wins",
                0,
                "DEALER WINS",
            )

    @discord.ui.button(
        label="Hit",
        emoji="🃏",
        style=discord.ButtonStyle.success,
    )
    async def hit(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        async with self.action_lock:
            if self.finished:
                await interaction.response.send_message(
                    "This blackjack game has already ended.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            self.player_hand.append(self.deck.pop())
            value = blackjack_hand_value(
                self.player_hand
            )

            if value > 21:
                await self.finish_game(
                    interaction,
                    "You busted",
                    0,
                    "PLAYER BUST",
                )
            elif value == 21:
                await self.dealer_play_and_finish(
                    interaction
                )
            else:
                await self.render(
                    interaction,
                    "YOUR MOVE",
                )

    @discord.ui.button(
        label="Stand",
        emoji="✋",
        style=discord.ButtonStyle.primary,
    )
    async def stand(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        async with self.action_lock:
            if self.finished:
                await interaction.response.send_message(
                    "This blackjack game has already ended.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()
            await self.dealer_play_and_finish(
                interaction
            )

    @discord.ui.button(
        label="Double Down",
        emoji="⚡",
        style=discord.ButtonStyle.danger,
    )
    async def double_down(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        async with self.action_lock:
            if self.finished:
                await interaction.response.send_message(
                    "This blackjack game has already ended.",
                    ephemeral=True,
                )
                return

            if len(self.player_hand) != 2:
                await interaction.response.send_message(
                    "Double Down is only available on your first move.",
                    ephemeral=True,
                )
                return

            account = await asyncio.to_thread(
                get_economy_account,
                self.guild_id,
                self.player_id,
            )

            if account["wallet"] < self.bet:
                await interaction.response.send_message(
                    f"You need another {format_coins(self.bet)} "
                    f"to double down.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer()

            charged, new_balance = await asyncio.to_thread(
                change_wallet,
                self.guild_id,
                self.player_id,
                -self.bet,
                "blackjack_double_down",
                f"original_bet={self.bet}",
            )

            if not charged:
                await interaction.followup.send(
                    "Your balance changed before the double down completed.",
                    ephemeral=True,
                )
                return

            self.bet *= 2
            self.balance_after_bet = new_balance
            self.player_hand.append(self.deck.pop())

            if blackjack_hand_value(self.player_hand) > 21:
                await self.finish_game(
                    interaction,
                    "You busted after doubling down",
                    0,
                    "DOUBLE DOWN BUST",
                )
            else:
                await self.dealer_play_and_finish(
                    interaction
                )

    async def on_timeout(self):
        if self.finished:
            return

        self.finished = True
        self.disable_all()
        active_blackjack_games.pop(
            (self.guild_id, self.player_id),
            None,
        )

        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def start_blackjack_game(
    interaction: discord.Interaction,
    bet: int,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Blackjack can only be played inside a server.",
            ephemeral=True,
        )
        return

    if bet < BLACKJACK_MIN_BET or bet > BLACKJACK_MAX_BET:
        await interaction.response.send_message(
            f"Your bet must be between "
            f"{format_coins(BLACKJACK_MIN_BET)} and "
            f"{format_coins(BLACKJACK_MAX_BET)}.",
            ephemeral=True,
        )
        return

    key = (
        interaction.guild.id,
        interaction.user.id,
    )

    if key in active_blackjack_games:
        await interaction.response.send_message(
            "You already have an active blackjack game.",
            ephemeral=True,
        )
        return

    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        interaction.user.id,
    )

    if account["wallet"] < bet:
        await interaction.response.send_message(
            f"You need {format_coins(bet)} in your wallet, "
            f"but you only have {format_coins(account['wallet'])}.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    charged, balance_after_bet = await asyncio.to_thread(
        change_wallet,
        interaction.guild.id,
        interaction.user.id,
        -bet,
        "blackjack_bet",
        f"bet={bet}",
    )

    if not charged:
        await interaction.followup.send(
            "Your balance changed before the game started.",
            ephemeral=True,
        )
        return

    deck = build_blackjack_deck()
    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]

    view = BlackjackView(
        interaction.guild.id,
        interaction.user.id,
        interaction.user.display_name,
        bet,
        deck,
        player_hand,
        dealer_hand,
        balance_after_bet,
    )

    active_blackjack_games[key] = view

    player_natural = blackjack_is_natural(
        player_hand
    )
    dealer_natural = blackjack_is_natural(
        dealer_hand
    )

    if player_natural or dealer_natural:
        if player_natural and dealer_natural:
            await view.finish_game(
                interaction,
                "Both have blackjack — push",
                bet,
                "DOUBLE BLACKJACK",
            )
        elif player_natural:
            payout = int(round(bet * 2.5))
            await view.finish_game(
                interaction,
                "Natural blackjack",
                payout,
                "BLACKJACK!",
            )
        else:
            await view.finish_game(
                interaction,
                "Dealer has blackjack",
                0,
                "DEALER BLACKJACK",
            )
        return

    await view.render(
        interaction,
        "YOUR MOVE",
    )

    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        view.message = None



# =========================================================
# FULL ECONOMY SUITE
# Roulette • Shop • Inventory • Crates • Minigames
# Achievements • Profiles • Prestige
# =========================================================

FULL_ECONOMY_SHOP = {
    "lucky_clover": {
        "name": "Lucky Clover",
        "emoji": "🍀",
        "price": 2500,
        "sell": 1250,
        "description": "Small passive bonus to gambling payouts.",
        "consumable": False,
    },
    "golden_ticket": {
        "name": "Golden Ticket",
        "emoji": "🎟️",
        "price": 1800,
        "sell": 900,
        "description": "Use it for an instant coin reward.",
        "consumable": True,
    },
    "mystery_crate": {
        "name": "Mystery Crate",
        "emoji": "📦",
        "price": 3000,
        "sell": 1200,
        "description": "Open it for coins or rare loot.",
        "consumable": True,
    },
    "vault_key": {
        "name": "Vault Key",
        "emoji": "🗝️",
        "price": 7500,
        "sell": 3500,
        "description": "A prestigious collector item.",
        "consumable": False,
    },
    "xp_boost": {
        "name": "XP Boost",
        "emoji": "⚡",
        "price": 1500,
        "sell": 650,
        "description": "Use it to gain instant economy XP.",
        "consumable": True,
    },
}

ACHIEVEMENT_DEFINITIONS = {
    "first_win": ("First Win", "Win any tracked game."),
    "high_roller": ("High Roller", "Place a bet of 5,000 coins or more."),
    "millionaire": ("Millionaire", "Reach a net worth of 1,000,000 coins."),
    "daily_7": ("Weekly Grinder", "Reach a 7-day daily streak."),
    "prestige_1": ("Reborn", "Prestige for the first time."),
    "collector": ("Collector", "Own at least 5 total shop items."),
}

MINIGAME_COOLDOWNS = {
    "fish": 60,
    "mine": 90,
    "hunt": 120,
    "crime": 180,
    "heist": 600,
}


def initialize_full_economy_tables() -> None:
    with economy_lock, economy_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS economy_inventory (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id, item_key)
            );

            CREATE TABLE IF NOT EXISTS economy_stats (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                prestige INTEGER NOT NULL DEFAULT 0,
                games_won INTEGER NOT NULL DEFAULT 0,
                games_lost INTEGER NOT NULL DEFAULT 0,
                biggest_win INTEGER NOT NULL DEFAULT 0,
                roulette_wins INTEGER NOT NULL DEFAULT 0,
                slots_wins INTEGER NOT NULL DEFAULT 0,
                blackjack_wins INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS economy_achievements (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                achievement_key TEXT NOT NULL,
                unlocked_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, achievement_key)
            );

            CREATE TABLE IF NOT EXISTS economy_cooldowns (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                action_key TEXT NOT NULL,
                last_used INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, action_key)
            );
            """
        )


def ensure_full_economy_user(guild_id: int, user_id: int) -> None:
    ensure_economy_user(guild_id, user_id)

    with economy_lock, economy_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO economy_stats (
                guild_id, user_id
            )
            VALUES (?, ?)
            """,
            (guild_id, user_id),
        )


def get_user_stats(guild_id: int, user_id: int) -> dict:
    ensure_full_economy_user(guild_id, user_id)

    with economy_lock, economy_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM economy_stats
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

    return dict(row)


def add_xp(guild_id: int, user_id: int, amount: int) -> tuple[int, int]:
    ensure_full_economy_user(guild_id, user_id)

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT xp, level
            FROM economy_stats
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

        xp = row["xp"] + max(0, amount)
        level = max(1, int((xp / 500) ** 0.5) + 1)

        connection.execute(
            """
            UPDATE economy_stats
            SET xp = ?, level = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (xp, level, guild_id, user_id),
        )
        connection.commit()

    return xp, level


def update_game_stats(
    guild_id: int,
    user_id: int,
    won: bool,
    amount: int,
    game: str,
) -> None:
    ensure_full_economy_user(guild_id, user_id)

    game_column = {
        "roulette": "roulette_wins",
        "slots": "slots_wins",
        "blackjack": "blackjack_wins",
    }.get(game)

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        if won and game_column:
            connection.execute(
                f"""
                UPDATE economy_stats
                SET games_won = games_won + 1,
                    biggest_win = MAX(biggest_win, ?),
                    {game_column} = {game_column} + 1
                WHERE guild_id = ? AND user_id = ?
                """,
                (max(0, amount), guild_id, user_id),
            )
        elif won:
            connection.execute(
                """
                UPDATE economy_stats
                SET games_won = games_won + 1,
                    biggest_win = MAX(biggest_win, ?)
                WHERE guild_id = ? AND user_id = ?
                """,
                (max(0, amount), guild_id, user_id),
            )
        else:
            connection.execute(
                """
                UPDATE economy_stats
                SET games_lost = games_lost + 1
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )

        connection.commit()

    add_xp(guild_id, user_id, 40 if won else 12)


def inventory_quantity(
    guild_id: int,
    user_id: int,
    item_key: str,
) -> int:
    with economy_lock, economy_connection() as connection:
        row = connection.execute(
            """
            SELECT quantity
            FROM economy_inventory
            WHERE guild_id = ? AND user_id = ? AND item_key = ?
            """,
            (guild_id, user_id, item_key),
        ).fetchone()

    return int(row["quantity"]) if row else 0


def change_inventory(
    guild_id: int,
    user_id: int,
    item_key: str,
    amount: int,
) -> tuple[bool, int]:
    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT quantity
            FROM economy_inventory
            WHERE guild_id = ? AND user_id = ? AND item_key = ?
            """,
            (guild_id, user_id, item_key),
        ).fetchone()

        current = int(row["quantity"]) if row else 0
        new_quantity = current + amount

        if new_quantity < 0:
            connection.rollback()
            return False, current

        connection.execute(
            """
            INSERT INTO economy_inventory (
                guild_id, user_id, item_key, quantity
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, item_key)
            DO UPDATE SET quantity = excluded.quantity
            """,
            (guild_id, user_id, item_key, new_quantity),
        )
        connection.commit()

    return True, new_quantity


def get_inventory(guild_id: int, user_id: int) -> list[dict]:
    with economy_lock, economy_connection() as connection:
        rows = connection.execute(
            """
            SELECT item_key, quantity
            FROM economy_inventory
            WHERE guild_id = ? AND user_id = ? AND quantity > 0
            ORDER BY quantity DESC, item_key
            """,
            (guild_id, user_id),
        ).fetchall()

    return [dict(row) for row in rows]


def check_cooldown(
    guild_id: int,
    user_id: int,
    action_key: str,
    duration: int,
) -> tuple[bool, int]:
    now = int(time.time())

    with economy_lock, economy_connection() as connection:
        row = connection.execute(
            """
            SELECT last_used
            FROM economy_cooldowns
            WHERE guild_id = ? AND user_id = ? AND action_key = ?
            """,
            (guild_id, user_id, action_key),
        ).fetchone()

        if row and now - row["last_used"] < duration:
            return False, duration - (now - row["last_used"])

        connection.execute(
            """
            INSERT INTO economy_cooldowns (
                guild_id, user_id, action_key, last_used
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, action_key)
            DO UPDATE SET last_used = excluded.last_used
            """,
            (guild_id, user_id, action_key, now),
        )
        connection.commit()

    return True, 0


def unlock_achievement(
    guild_id: int,
    user_id: int,
    key: str,
) -> bool:
    if key not in ACHIEVEMENT_DEFINITIONS:
        return False

    with economy_lock, economy_connection() as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO economy_achievements (
                guild_id, user_id, achievement_key, unlocked_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, user_id, key, int(time.time())),
        )
        connection.commit()
        return cursor.rowcount > 0


def get_achievements(guild_id: int, user_id: int) -> list[str]:
    with economy_lock, economy_connection() as connection:
        rows = connection.execute(
            """
            SELECT achievement_key
            FROM economy_achievements
            WHERE guild_id = ? AND user_id = ?
            ORDER BY unlocked_at
            """,
            (guild_id, user_id),
        ).fetchall()

    return [row["achievement_key"] for row in rows]


def evaluate_achievements(guild_id: int, user_id: int) -> list[str]:
    unlocked = []
    account = get_economy_account(guild_id, user_id)
    stats = get_user_stats(guild_id, user_id)
    inventory = get_inventory(guild_id, user_id)

    if stats["games_won"] >= 1 and unlock_achievement(guild_id, user_id, "first_win"):
        unlocked.append("first_win")

    if account["wallet"] + account["bank"] >= 1_000_000 and unlock_achievement(guild_id, user_id, "millionaire"):
        unlocked.append("millionaire")

    if account["daily_streak"] >= 7 and unlock_achievement(guild_id, user_id, "daily_7"):
        unlocked.append("daily_7")

    if stats["prestige"] >= 1 and unlock_achievement(guild_id, user_id, "prestige_1"):
        unlocked.append("prestige_1")

    if sum(item["quantity"] for item in inventory) >= 5 and unlock_achievement(guild_id, user_id, "collector"):
        unlocked.append("collector")

    return unlocked


def roulette_colour(number: int) -> str:
    if number == 0:
        return "green"

    red_numbers = {
        1, 3, 5, 7, 9, 12, 14, 16, 18,
        19, 21, 23, 25, 27, 30, 32, 34, 36,
    }
    return "red" if number in red_numbers else "black"


def create_roulette_image(
    player_name: str,
    result: int,
    colour: str,
    bet_type: str,
    selection: str,
    bet: int,
    payout: int,
    balance: int,
) -> io.BytesIO:
    width, height = 1200, 700
    image = Image.new("RGB", (width, height), (7, 7, 10))
    draw = ImageDraw.Draw(image)

    gold = (238, 186, 45)
    light_gold = (255, 225, 130)
    panel = (18, 18, 24)
    felt = (13, 67, 47)
    red = (175, 25, 32)
    black = (20, 20, 25)
    green = (20, 120, 65)

    draw.rounded_rectangle(
        (28, 28, width - 28, height - 28),
        radius=38,
        fill=panel,
        outline=gold,
        width=8,
    )

    title_font = get_slot_font(58, bold=True)
    result_font = get_slot_font(130, bold=True)
    header_font = get_slot_font(29, bold=True)
    body_font = get_slot_font(25, bold=True)

    draw.text(
        (width // 2, 82),
        "777 ROYALE ROULETTE",
        font=title_font,
        fill=light_gold,
        anchor="mm",
    )

    wheel_box = (80, 145, 560, 620)
    draw.ellipse(wheel_box, fill=(35, 28, 18), outline=gold, width=8)
    draw.ellipse((130, 195, 510, 575), fill=felt, outline=gold, width=5)

    result_colour = {
        "red": red,
        "black": black,
        "green": green,
    }[colour]

    draw.ellipse(
        (220, 285, 420, 485),
        fill=result_colour,
        outline=light_gold,
        width=7,
    )
    draw.text(
        (320, 385),
        str(result),
        font=result_font,
        fill=(255, 255, 245),
        anchor="mm",
    )

    draw.rounded_rectangle(
        (640, 155, 1125, 600),
        radius=30,
        fill=(13, 13, 18),
        outline=gold,
        width=5,
    )

    lines = [
        ("RESULT", f"{result} • {colour.upper()}"),
        ("BET TYPE", bet_type.upper()),
        ("SELECTION", selection.upper()),
        ("BET", format_coins(bet)),
        ("PAYOUT", format_coins(payout)),
        ("BALANCE", format_coins(balance)),
    ]

    y = 210
    for label, value in lines:
        draw.text((685, y), label, font=header_font, fill=gold)
        draw.text((685, y + 38), value, font=body_font, fill=(240, 240, 235))
        y += 70

    draw.text(
        (width // 2, 660),
        f"PLAYER: {player_name[:32]} • 777 • PLAY RESPONSIBLY",
        font=body_font,
        fill=(175, 175, 180),
        anchor="mm",
    )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


async def execute_roulette(
    interaction: discord.Interaction,
    bet: int,
    bet_type: str,
    selection: str,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Roulette can only be played inside a server.",
            ephemeral=True,
        )
        return

    bet_type = bet_type.lower().strip()
    selection = selection.lower().strip()

    if bet < SLOTS_MIN_BET or bet > SLOTS_MAX_BET:
        await interaction.response.send_message(
            f"Bet between {format_coins(SLOTS_MIN_BET)} and {format_coins(SLOTS_MAX_BET)}.",
            ephemeral=True,
        )
        return

    valid = False
    multiplier = 0

    if bet_type == "colour" and selection in {"red", "black", "green"}:
        valid = True
        multiplier = 14 if selection == "green" else 2
    elif bet_type == "parity" and selection in {"odd", "even"}:
        valid = True
        multiplier = 2
    elif bet_type == "number":
        try:
            number_selection = int(selection)
            valid = 0 <= number_selection <= 36
            multiplier = 36
        except ValueError:
            valid = False

    if not valid:
        await interaction.response.send_message(
            "Use `colour` with red/black/green, `parity` with odd/even, or `number` with 0–36.",
            ephemeral=True,
        )
        return

    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        interaction.user.id,
    )

    if account["wallet"] < bet:
        await interaction.response.send_message(
            "You do not have enough wallet coins.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    charged, balance = await asyncio.to_thread(
        change_wallet,
        interaction.guild.id,
        interaction.user.id,
        -bet,
        "roulette_bet",
        f"type={bet_type};selection={selection}",
    )

    if not charged:
        await interaction.followup.send(
            "Your balance changed. Try again.",
            ephemeral=True,
        )
        return

    message = await interaction.edit_original_response(
        embed=discord.Embed(
            title="🎡 Roulette is spinning...",
            description="`● ○ ○ ○ ○`",
            colour=GOLD_COLOUR,
        )
    )

    for frame in [
        "`○ ● ○ ○ ○`",
        "`○ ○ ● ○ ○`",
        "`○ ○ ○ ● ○`",
        "`○ ○ ○ ○ ●`",
    ]:
        await asyncio.sleep(0.55)
        try:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="🎡 Roulette is spinning...",
                    description=frame,
                    colour=GOLD_COLOUR,
                )
            )
        except discord.HTTPException:
            pass

    result = random.randint(0, 36)
    colour = roulette_colour(result)

    won = False
    if bet_type == "colour":
        won = selection == colour
    elif bet_type == "parity":
        won = result != 0 and (
            (selection == "even" and result % 2 == 0)
            or (selection == "odd" and result % 2 == 1)
        )
    else:
        won = int(selection) == result

    payout = bet * multiplier if won else 0

    if payout:
        _, balance = await asyncio.to_thread(
            change_wallet,
            interaction.guild.id,
            interaction.user.id,
            payout,
            "roulette_payout",
            f"result={result};colour={colour}",
        )

    profit = payout - bet
    await asyncio.to_thread(
        update_game_stats,
        interaction.guild.id,
        interaction.user.id,
        won,
        max(0, profit),
        "roulette",
    )

    image = await asyncio.to_thread(
        create_roulette_image,
        interaction.user.display_name,
        result,
        colour,
        bet_type,
        selection,
        bet,
        payout,
        balance,
    )

    filename = f"roulette_{interaction.user.id}_{int(time.time())}.png"
    file = discord.File(image, filename=filename)

    embed = discord.Embed(
        title="🎡 777 Roulette Result",
        description=(
            f"## {'🎉 You won!' if won else '💸 You lost.'}\n\n"
            f"The wheel landed on **{result} {colour.upper()}**."
        ),
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_image(url=f"attachment://{filename}")
    embed.add_field(name="Bet", value=format_coins(bet), inline=True)
    embed.add_field(name="Payout", value=format_coins(payout), inline=True)
    embed.add_field(name="Wallet", value=format_coins(balance), inline=True)

    await interaction.edit_original_response(
        embed=embed,
        attachments=[file],
    )


def perform_economy_minigame(
    guild_id: int,
    user_id: int,
    action: str,
) -> tuple[bool, str, int, int]:
    cooldown = MINIGAME_COOLDOWNS[action]
    ready, remaining = check_cooldown(
        guild_id, user_id, action, cooldown
    )

    if not ready:
        return False, f"Cooldown: {format_cooldown(remaining)}", 0, 0

    account = get_economy_account(guild_id, user_id)
    reward = 0
    description = ""

    if action == "fish":
        catches = [
            ("a tiny fish", 40, 80),
            ("a golden carp", 120, 240),
            ("an old boot", 5, 15),
            ("a rare shark", 300, 500),
        ]
        name, low, high = random.choice(catches)
        reward = random.randint(low, high)
        description = f"You caught **{name}**."

    elif action == "mine":
        finds = [
            ("coal", 70, 130),
            ("iron", 110, 190),
            ("gold", 220, 400),
            ("a diamond", 500, 800),
        ]
        name, low, high = random.choice(finds)
        reward = random.randint(low, high)
        description = f"You mined **{name}**."

    elif action == "hunt":
        finds = [
            ("a rabbit", 90, 170),
            ("a deer", 180, 330),
            ("a legendary beast", 650, 950),
        ]
        name, low, high = random.choice(finds)
        reward = random.randint(low, high)
        description = f"You hunted **{name}**."

    elif action == "crime":
        if random.random() < 0.58:
            reward = random.randint(250, 700)
            description = "Your risky crime succeeded."
        else:
            loss = min(account["wallet"], random.randint(100, 450))
            change_wallet(guild_id, user_id, -loss, "crime_failed")
            return True, f"You were caught and fined {format_coins(loss)}.", -loss, account["wallet"] - loss

    elif action == "heist":
        if account["wallet"] < 1000:
            return False, "You need at least 1,000 wallet coins to attempt a heist.", 0, account["wallet"]

        if random.random() < 0.32:
            reward = random.randint(1500, 5000)
            description = "The heist succeeded."
        else:
            loss = min(account["wallet"], random.randint(500, 1500))
            change_wallet(guild_id, user_id, -loss, "heist_failed")
            return True, f"The heist failed and cost you {format_coins(loss)}.", -loss, account["wallet"] - loss

    _, wallet = change_wallet(
        guild_id,
        user_id,
        reward,
        f"minigame_{action}",
        description,
    )
    add_xp(guild_id, user_id, random.randint(15, 40))
    return True, description, reward, wallet


initialize_full_economy_tables()


# =========================================================
# ECONOMY POLISH, ADMIN TOOLS, ITEM EFFECTS, AUDIT & HELP
# =========================================================

ECONOMY_MAX_TRANSFER = int(
    os.getenv("ECONOMY_MAX_TRANSFER", "1000000")
)
ECONOMY_MAX_BALANCE = int(
    os.getenv("ECONOMY_MAX_BALANCE", "9000000000000000")
)
ECONOMY_ADMIN_LOG_CHANNEL_ID = os.getenv(
    "ECONOMY_ADMIN_LOG_CHANNEL_ID"
)

HELP_CATEGORIES = {
    "Community": [
        "/quote", "/clip", "/counting", "/smash_or_pass",
        "/poll", "/suggest", "/giveaway",
    ],
    "Economy": [
        "/balance", "/daily", "/work", "/deposit",
        "/withdraw", "/pay", "/leaderboard", "/profile",
        "/stats", "/achievements", "/prestige",
    ],
    "Casino": [
        "/slots", "/blackjack", "/roulette",
    ],
    "Shop": [
        "/shop", "/buy", "/inventory", "/use", "/sell",
    ],
    "Activities": [
        "/fish", "/mine", "/hunt", "/crime", "/heist",
    ],
    "Moderation": [
        "/ban", "/unban", "/kick", "/timeout", "/untimeout",
        "/warn", "/warnings", "/clear_warnings", "/purge",
        "/lock", "/unlock", "/slowmode", "/security_status",
    ],
    "Admin": [
        "/economy_add", "/economy_remove", "/economy_set",
        "/economy_reset", "/economy_lookup", "/economy_rollback",
    ],
}


def initialize_polish_tables() -> None:
    with economy_lock, economy_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS economy_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                amount INTEGER,
                previous_wallet INTEGER,
                new_wallet INTEGER,
                reason TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS economy_effects (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                effect_key TEXT NOT NULL,
                value REAL NOT NULL DEFAULT 0,
                expires_at INTEGER,
                PRIMARY KEY (guild_id, user_id, effect_key)
            );

            CREATE TABLE IF NOT EXISTS economy_game_stats (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                total_wagered INTEGER NOT NULL DEFAULT 0,
                total_payout INTEGER NOT NULL DEFAULT 0,
                biggest_bet INTEGER NOT NULL DEFAULT 0,
                biggest_jackpot INTEGER NOT NULL DEFAULT 0,
                slots_spins INTEGER NOT NULL DEFAULT 0,
                blackjack_games INTEGER NOT NULL DEFAULT 0,
                blackjack_pushes INTEGER NOT NULL DEFAULT 0,
                roulette_spins INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            );
            """
        )


def clamp_balance(value: int) -> int:
    return max(0, min(int(value), ECONOMY_MAX_BALANCE))


def ensure_game_stats(guild_id: int, user_id: int) -> None:
    with economy_lock, economy_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO economy_game_stats (
                guild_id, user_id
            )
            VALUES (?, ?)
            """,
            (guild_id, user_id),
        )


def update_detailed_game_stats(
    guild_id: int,
    user_id: int,
    game: str,
    bet: int,
    payout: int,
    jackpot: bool = False,
    push: bool = False,
) -> None:
    ensure_game_stats(guild_id, user_id)

    game_column = {
        "slots": "slots_spins",
        "blackjack": "blackjack_games",
        "roulette": "roulette_spins",
    }.get(game)

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")

        if game_column:
            connection.execute(
                f"""
                UPDATE economy_game_stats
                SET total_wagered = total_wagered + ?,
                    total_payout = total_payout + ?,
                    biggest_bet = MAX(biggest_bet, ?),
                    biggest_jackpot = MAX(
                        biggest_jackpot,
                        ?
                    ),
                    {game_column} = {game_column} + 1,
                    blackjack_pushes = blackjack_pushes + ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (
                    max(0, bet),
                    max(0, payout),
                    max(0, bet),
                    max(0, payout if jackpot else 0),
                    1 if push else 0,
                    guild_id,
                    user_id,
                ),
            )

        connection.commit()


def get_detailed_game_stats(
    guild_id: int,
    user_id: int,
) -> dict:
    ensure_game_stats(guild_id, user_id)

    with economy_lock, economy_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM economy_game_stats
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

    return dict(row)


def set_effect(
    guild_id: int,
    user_id: int,
    effect_key: str,
    value: float,
    duration_seconds: int | None,
) -> None:
    expires_at = (
        int(time.time()) + duration_seconds
        if duration_seconds
        else None
    )

    with economy_lock, economy_connection() as connection:
        connection.execute(
            """
            INSERT INTO economy_effects (
                guild_id, user_id, effect_key, value, expires_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, effect_key)
            DO UPDATE SET
                value = excluded.value,
                expires_at = excluded.expires_at
            """,
            (
                guild_id,
                user_id,
                effect_key,
                value,
                expires_at,
            ),
        )
        connection.commit()


def get_effect(
    guild_id: int,
    user_id: int,
    effect_key: str,
) -> float:
    now = int(time.time())

    with economy_lock, economy_connection() as connection:
        row = connection.execute(
            """
            SELECT value, expires_at
            FROM economy_effects
            WHERE guild_id = ? AND user_id = ? AND effect_key = ?
            """,
            (guild_id, user_id, effect_key),
        ).fetchone()

        if not row:
            return 0.0

        if row["expires_at"] and row["expires_at"] <= now:
            connection.execute(
                """
                DELETE FROM economy_effects
                WHERE guild_id = ? AND user_id = ? AND effect_key = ?
                """,
                (guild_id, user_id, effect_key),
            )
            connection.commit()
            return 0.0

        return float(row["value"])


def record_admin_audit(
    guild_id: int,
    actor_id: int,
    target_id: int,
    action: str,
    amount: int | None,
    previous_wallet: int | None,
    new_wallet: int | None,
    reason: str,
) -> int:
    with economy_lock, economy_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO economy_audit_log (
                guild_id, actor_id, target_id, action,
                amount, previous_wallet, new_wallet,
                reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                actor_id,
                target_id,
                action,
                amount,
                previous_wallet,
                new_wallet,
                reason[:500],
                int(time.time()),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def get_audit_entry(
    guild_id: int,
    audit_id: int,
) -> dict | None:
    with economy_lock, economy_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM economy_audit_log
            WHERE guild_id = ? AND id = ?
            """,
            (guild_id, audit_id),
        ).fetchone()

    return dict(row) if row else None


def admin_set_wallet(
    guild_id: int,
    user_id: int,
    new_wallet: int,
) -> tuple[int, int]:
    ensure_economy_user(guild_id, user_id)
    new_wallet = clamp_balance(new_wallet)

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT wallet
            FROM economy_users
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

        previous = int(row["wallet"])

        connection.execute(
            """
            UPDATE economy_users
            SET wallet = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (new_wallet, guild_id, user_id),
        )
        connection.commit()

    return previous, new_wallet


async def send_economy_audit_embed(
    guild: discord.Guild,
    actor: discord.abc.User,
    target: discord.abc.User,
    action: str,
    amount: int | None,
    previous: int | None,
    new: int | None,
    reason: str,
    audit_id: int,
) -> None:
    if not ECONOMY_ADMIN_LOG_CHANNEL_ID:
        return

    try:
        channel_id = int(ECONOMY_ADMIN_LOG_CHANNEL_ID)
    except ValueError:
        return

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    embed = discord.Embed(
        title="🧾 Economy Admin Audit",
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Audit ID", value=f"`{audit_id}`", inline=True)
    embed.add_field(name="Action", value=action, inline=True)
    embed.add_field(name="Actor", value=actor.mention, inline=True)
    embed.add_field(name="Target", value=target.mention, inline=True)

    if amount is not None:
        embed.add_field(
            name="Amount",
            value=format_coins(amount),
            inline=True,
        )

    if previous is not None and new is not None:
        embed.add_field(
            name="Wallet Change",
            value=f"{format_coins(previous)} → {format_coins(new)}",
            inline=False,
        )

    embed.add_field(
        name="Reason",
        value=reason or "No reason supplied.",
        inline=False,
    )

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        logger.exception("Failed to send economy audit log.")


def is_economy_admin(
    interaction: discord.Interaction,
) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False

    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.guild_permissions.manage_guild
    )


async def require_economy_admin(
    interaction: discord.Interaction,
) -> bool:
    if is_economy_admin(interaction):
        return True

    await interaction.response.send_message(
        "You need Administrator or Manage Server permission.",
        ephemeral=True,
    )
    return False


class HelpCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=name,
                description=f"View {name.lower()} commands.",
            )
            for name in HELP_CATEGORIES
        ]

        super().__init__(
            placeholder="Choose a help category...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        category = self.values[0]
        commands = HELP_CATEGORIES[category]

        embed = discord.Embed(
            title=f"777 Help • {category}",
            description="\n".join(
                f"• `{command}`" for command in commands
            ),
            colour=GOLD_COLOUR,
        )
        embed.set_footer(
            text="Use Discord's slash-command picker for full options."
        )

        await interaction.response.edit_message(
            embed=embed,
            view=self.view,
        )


class HelpView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.add_item(HelpCategorySelect())

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Open your own `/help` menu to use it.",
                ephemeral=True,
            )
            return False
        return True


initialize_polish_tables()


# =========================================================
# MODERATION & SERVER SECURITY
# =========================================================

MOD_LOG_CHANNEL_ID = os.getenv("MOD_LOG_CHANNEL_ID")
SECURITY_ALERT_CHANNEL_ID = os.getenv(
    "SECURITY_ALERT_CHANNEL_ID",
    MOD_LOG_CHANNEL_ID or "",
)

ANTI_SPAM_ENABLED = (
    os.getenv("ANTI_SPAM_ENABLED", "true").lower()
    in {"1", "true", "yes", "on"}
)
ANTI_SPAM_MESSAGE_LIMIT = max(
    3,
    int(os.getenv("ANTI_SPAM_MESSAGE_LIMIT", "6")),
)
ANTI_SPAM_WINDOW_SECONDS = max(
    2,
    int(os.getenv("ANTI_SPAM_WINDOW_SECONDS", "8")),
)
ANTI_SPAM_TIMEOUT_MINUTES = max(
    1,
    int(os.getenv("ANTI_SPAM_TIMEOUT_MINUTES", "5")),
)

ANTI_RAID_ENABLED = (
    os.getenv("ANTI_RAID_ENABLED", "true").lower()
    in {"1", "true", "yes", "on"}
)
ANTI_RAID_JOIN_LIMIT = max(
    3,
    int(os.getenv("ANTI_RAID_JOIN_LIMIT", "8")),
)
ANTI_RAID_WINDOW_SECONDS = max(
    5,
    int(os.getenv("ANTI_RAID_WINDOW_SECONDS", "20")),
)
ANTI_RAID_ACCOUNT_AGE_HOURS = max(
    0,
    int(os.getenv("ANTI_RAID_ACCOUNT_AGE_HOURS", "24")),
)
ANTI_RAID_AUTO_TIMEOUT = (
    os.getenv("ANTI_RAID_AUTO_TIMEOUT", "false").lower()
    in {"1", "true", "yes", "on"}
)

_security_message_history: dict[
    tuple[int, int],
    list[float],
] = {}
_security_join_history: dict[int, list[float]] = {}
_security_alert_cooldowns: dict[tuple[int, str], float] = {}


def initialize_security_tables() -> None:
    with economy_lock, economy_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS moderation_warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS moderation_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                target_id INTEGER,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                duration_seconds INTEGER,
                created_at INTEGER NOT NULL
            );
            """
        )


def configured_channel(
    guild: discord.Guild,
    raw_id: str | None,
) -> discord.TextChannel | None:
    if not raw_id:
        return None

    try:
        channel_id = int(raw_id)
    except ValueError:
        return None

    channel = guild.get_channel(channel_id)
    return (
        channel
        if isinstance(channel, discord.TextChannel)
        else None
    )


def member_is_moderator(member: discord.Member) -> bool:
    permissions = member.guild_permissions
    return any(
        (
            permissions.administrator,
            permissions.manage_guild,
            permissions.moderate_members,
            permissions.kick_members,
            permissions.ban_members,
            permissions.manage_messages,
        )
    )


def bot_can_act_on(
    guild: discord.Guild,
    target: discord.Member,
) -> tuple[bool, str]:
    bot_member = guild.me

    if bot_member is None:
        return False, "The bot member could not be resolved."

    if target.id == guild.owner_id:
        return False, "The server owner cannot be moderated."

    if target.top_role >= bot_member.top_role:
        return (
            False,
            "My highest role must be above the target member's highest role.",
        )

    return True, ""


def moderator_can_act_on(
    moderator: discord.Member,
    target: discord.Member,
) -> tuple[bool, str]:
    if moderator.id == target.id:
        return False, "You cannot moderate yourself."

    if target.id == moderator.guild.owner_id:
        return False, "The server owner cannot be moderated."

    if (
        moderator.id != moderator.guild.owner_id
        and target.top_role >= moderator.top_role
    ):
        return (
            False,
            "Your highest role must be above the target member's highest role.",
        )

    return True, ""


async def moderation_precheck(
    interaction: discord.Interaction,
    target: discord.Member,
) -> bool:
    if interaction.guild is None or not isinstance(
        interaction.user,
        discord.Member,
    ):
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return False

    allowed, reason = moderator_can_act_on(
        interaction.user,
        target,
    )
    if not allowed:
        await interaction.response.send_message(
            reason,
            ephemeral=True,
        )
        return False

    allowed, reason = bot_can_act_on(
        interaction.guild,
        target,
    )
    if not allowed:
        await interaction.response.send_message(
            reason,
            ephemeral=True,
        )
        return False

    return True


def record_moderation_action(
    guild_id: int,
    moderator_id: int,
    target_id: int | None,
    action: str,
    reason: str,
    duration_seconds: int | None = None,
) -> int:
    with economy_lock, economy_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO moderation_actions (
                guild_id, moderator_id, target_id,
                action, reason, duration_seconds, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                moderator_id,
                target_id,
                action,
                reason[:1000],
                duration_seconds,
                int(time.time()),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


async def send_mod_log(
    guild: discord.Guild,
    action: str,
    moderator: discord.abc.User | None,
    target: discord.abc.User | None,
    reason: str,
    case_id: int | None = None,
    duration: str | None = None,
    extra: str | None = None,
) -> None:
    channel = configured_channel(
        guild,
        MOD_LOG_CHANNEL_ID,
    )
    if channel is None:
        return

    embed = discord.Embed(
        title=f"🛡️ {action}",
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )

    if case_id is not None:
        embed.add_field(
            name="Case",
            value=f"`{case_id}`",
            inline=True,
        )

    if moderator is not None:
        embed.add_field(
            name="Moderator",
            value=f"{moderator.mention} (`{moderator.id}`)",
            inline=True,
        )

    if target is not None:
        embed.add_field(
            name="Target",
            value=f"{target.mention} (`{target.id}`)",
            inline=True,
        )

    if duration:
        embed.add_field(
            name="Duration",
            value=duration,
            inline=True,
        )

    embed.add_field(
        name="Reason",
        value=reason or "No reason provided.",
        inline=False,
    )

    if extra:
        embed.add_field(
            name="Details",
            value=extra,
            inline=False,
        )

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        logger.exception("Failed to send moderation log.")


async def send_security_alert(
    guild: discord.Guild,
    title: str,
    description: str,
    alert_key: str,
    cooldown: int = 30,
) -> None:
    now = time.monotonic()
    key = (guild.id, alert_key)

    if now - _security_alert_cooldowns.get(key, 0) < cooldown:
        return

    _security_alert_cooldowns[key] = now

    channel = configured_channel(
        guild,
        SECURITY_ALERT_CHANNEL_ID,
    )
    if channel is None:
        return

    embed = discord.Embed(
        title=f"🚨 {title}",
        description=description,
        colour=discord.Colour.red(),
        timestamp=datetime.now(timezone.utc),
    )

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        logger.exception("Failed to send security alert.")


async def security_check_message(
    message: discord.Message,
) -> bool:
    if (
        not ANTI_SPAM_ENABLED
        or message.guild is None
        or not isinstance(message.author, discord.Member)
        or member_is_moderator(message.author)
    ):
        return False

    now = time.monotonic()
    key = (message.guild.id, message.author.id)
    history = _security_message_history.setdefault(key, [])
    cutoff = now - ANTI_SPAM_WINDOW_SECONDS

    history[:] = [
        timestamp
        for timestamp in history
        if timestamp >= cutoff
    ]
    history.append(now)

    if len(history) < ANTI_SPAM_MESSAGE_LIMIT:
        return False

    history.clear()

    try:
        await message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass

    timeout_until = discord.utils.utcnow() + timedelta(
        minutes=ANTI_SPAM_TIMEOUT_MINUTES
    )

    action_taken = "messages flagged"

    try:
        can_act, _ = bot_can_act_on(
            message.guild,
            message.author,
        )
        if can_act:
            await message.author.timeout(
                timeout_until,
                reason="777 anti-spam protection",
            )
            action_taken = (
                f"timed out for {ANTI_SPAM_TIMEOUT_MINUTES} minutes"
            )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Anti-spam timeout failed.")

    await send_security_alert(
        message.guild,
        "Anti-Spam Triggered",
        (
            f"{message.author.mention} sent at least "
            f"**{ANTI_SPAM_MESSAGE_LIMIT} messages** within "
            f"**{ANTI_SPAM_WINDOW_SECONDS} seconds** and was "
            f"{action_taken}."
        ),
        f"spam:{message.author.id}",
    )

    return True


async def security_check_join(
    member: discord.Member,
) -> None:
    if not ANTI_RAID_ENABLED:
        return

    now = time.monotonic()
    history = _security_join_history.setdefault(
        member.guild.id,
        [],
    )
    cutoff = now - ANTI_RAID_WINDOW_SECONDS
    history[:] = [
        timestamp
        for timestamp in history
        if timestamp >= cutoff
    ]
    history.append(now)

    account_age = (
        discord.utils.utcnow() - member.created_at
    )
    young_account = (
        account_age
        < timedelta(hours=ANTI_RAID_ACCOUNT_AGE_HOURS)
    )

    if len(history) >= ANTI_RAID_JOIN_LIMIT:
        await send_security_alert(
            member.guild,
            "Possible Join Raid",
            (
                f"Detected **{len(history)} joins** within "
                f"**{ANTI_RAID_WINDOW_SECONDS} seconds**.\n"
                f"Latest member: {member.mention} (`{member.id}`)"
            ),
            "join_raid",
            cooldown=ANTI_RAID_WINDOW_SECONDS,
        )

    if young_account:
        await send_security_alert(
            member.guild,
            "New Account Joined",
            (
                f"{member.mention}'s account is only "
                f"{discord.utils.format_dt(member.created_at, style='R')} old."
            ),
            f"young:{member.id}",
            cooldown=5,
        )

        if ANTI_RAID_AUTO_TIMEOUT:
            can_act, _ = bot_can_act_on(
                member.guild,
                member,
            )
            if can_act:
                try:
                    await member.timeout(
                        discord.utils.utcnow()
                        + timedelta(minutes=10),
                        reason="777 new-account security review",
                    )
                except (
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    logger.exception(
                        "New-account timeout failed."
                    )


def add_warning(
    guild_id: int,
    user_id: int,
    moderator_id: int,
    reason: str,
) -> int:
    with economy_lock, economy_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO moderation_warnings (
                guild_id, user_id, moderator_id,
                reason, created_at, active
            )
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                guild_id,
                user_id,
                moderator_id,
                reason[:1000],
                int(time.time()),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def get_active_warnings(
    guild_id: int,
    user_id: int,
) -> list[dict]:
    with economy_lock, economy_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM moderation_warnings
            WHERE guild_id = ? AND user_id = ? AND active = 1
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (guild_id, user_id),
        ).fetchall()

    return [dict(row) for row in rows]


def clear_active_warnings(
    guild_id: int,
    user_id: int,
) -> int:
    with economy_lock, economy_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE moderation_warnings
            SET active = 0
            WHERE guild_id = ? AND user_id = ? AND active = 1
            """,
            (guild_id, user_id),
        )
        connection.commit()
        return int(cursor.rowcount)


initialize_security_tables()

# =========================================================
# SLASH COMMANDS
# =========================================================

@bot.tree.command(
    name="balance",
    description="View your or another member's economy balance.",
)
@app_commands.describe(
    member="Optional member whose balance you want to view."
)
async def balance(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    target = member or interaction.user

    if target.bot:
        await interaction.response.send_message(
            "Bots do not have economy accounts.",
            ephemeral=True,
        )
        return

    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        target.id,
    )

    net_worth = account["wallet"] + account["bank"]

    embed = discord.Embed(
        title="💰 777 Economy",
        description=f"Balance for {target.mention}",
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )

    embed.set_thumbnail(
        url=target.display_avatar.url
    )

    embed.add_field(
        name="Wallet",
        value=format_coins(account["wallet"]),
        inline=True,
    )

    embed.add_field(
        name="Bank",
        value=format_coins(account["bank"]),
        inline=True,
    )

    embed.add_field(
        name="Net Worth",
        value=format_coins(net_worth),
        inline=True,
    )

    embed.add_field(
        name="Daily Streak",
        value=f"`{account['daily_streak']}` day(s)",
        inline=True,
    )

    embed.set_footer(
        text="777 • Economy"
    )

    await interaction.response.send_message(
        embed=embed
    )


@bot.tree.command(
    name="daily",
    description="Claim your daily coins and build a streak.",
)
async def daily(
    interaction: discord.Interaction,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    success, value, streak, wallet = await asyncio.to_thread(
        claim_daily_reward,
        interaction.guild.id,
        interaction.user.id,
    )

    if not success:
        await interaction.response.send_message(
            f"Your daily reward is not ready yet. "
            f"Try again in **{format_cooldown(value)}**.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="🎁 Daily Reward",
        description=(
            f"You claimed **{format_coins(value)}**!\n"
            f"Your streak is now **{streak} day(s)**."
        ),
        colour=GOLD_COLOUR,
    )

    embed.add_field(
        name="Wallet",
        value=format_coins(wallet),
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed
    )


@bot.tree.command(
    name="work",
    description="Work a job to earn coins.",
)
async def work(
    interaction: discord.Interaction,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    success, value, wallet, job_name = await asyncio.to_thread(
        perform_work,
        interaction.guild.id,
        interaction.user.id,
    )

    if not success:
        await interaction.response.send_message(
            f"You are tired. Work again in "
            f"**{format_cooldown(value)}**.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="🛠️ Work Complete",
        description=(
            f"You **{job_name}** and earned "
            f"**{format_coins(value)}**."
        ),
        colour=GOLD_COLOUR,
    )

    embed.add_field(
        name="Wallet",
        value=format_coins(wallet),
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed
    )


@bot.tree.command(
    name="deposit",
    description="Move coins from your wallet into your bank.",
)
@app_commands.describe(
    amount="Number of coins to deposit."
)
async def deposit(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 1_000_000_000],
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    success, account = await asyncio.to_thread(
        transfer_wallet_bank,
        interaction.guild.id,
        interaction.user.id,
        amount,
        True,
    )

    if not success:
        await interaction.response.send_message(
            f"You only have {format_coins(account['wallet'])} "
            f"in your wallet.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"🏦 Deposited **{format_coins(amount)}**.\n"
        f"Wallet: **{format_coins(account['wallet'])}**\n"
        f"Bank: **{format_coins(account['bank'])}**"
    )


@bot.tree.command(
    name="withdraw",
    description="Move coins from your bank into your wallet.",
)
@app_commands.describe(
    amount="Number of coins to withdraw."
)
async def withdraw(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 1_000_000_000],
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    success, account = await asyncio.to_thread(
        transfer_wallet_bank,
        interaction.guild.id,
        interaction.user.id,
        amount,
        False,
    )

    if not success:
        await interaction.response.send_message(
            f"You only have {format_coins(account['bank'])} "
            f"in your bank.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"💵 Withdrew **{format_coins(amount)}**.\n"
        f"Wallet: **{format_coins(account['wallet'])}**\n"
        f"Bank: **{format_coins(account['bank'])}**"
    )


@bot.tree.command(
    name="pay",
    description="Send wallet coins to another member.",
)
@app_commands.describe(
    member="The member receiving the coins.",
    amount="Number of coins to send.",
)
async def pay(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 1, 1_000_000_000],
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    if member.bot:
        await interaction.response.send_message(
            "You cannot pay a bot.",
            ephemeral=True,
        )
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You cannot pay yourself.",
            ephemeral=True,
        )
        return

    if amount > ECONOMY_MAX_TRANSFER:
        await interaction.response.send_message(
            f"Transfers are limited to "
            f"{format_coins(ECONOMY_MAX_TRANSFER)} per command.",
            ephemeral=True,
        )
        return

    success, reason = await asyncio.to_thread(
        pay_economy_user,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        amount,
    )

    if not success:
        await interaction.response.send_message(
            "You do not have enough coins in your wallet.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"💸 {interaction.user.mention} paid "
        f"{member.mention} **{format_coins(amount)}**."
    )


@bot.tree.command(
    name="leaderboard",
    description="View the richest members in the server.",
)
async def leaderboard(
    interaction: discord.Interaction,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    rows = await asyncio.to_thread(
        economy_leaderboard,
        interaction.guild.id,
        10,
    )

    if not rows:
        await interaction.response.send_message(
            "No economy accounts exist yet.",
            ephemeral=True,
        )
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []

    for index, row in enumerate(rows):
        member = interaction.guild.get_member(
            row["user_id"]
        )

        member_name = (
            member.display_name
            if member
            else f"User {row['user_id']}"
        )

        prefix = (
            medals[index]
            if index < len(medals)
            else f"`#{index + 1}`"
        )

        lines.append(
            f"{prefix} **{member_name}** — "
            f"{format_coins(row['net_worth'])}"
        )

    embed = discord.Embed(
        title="🏆 777 Rich List",
        description="\n".join(lines),
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )

    embed.set_footer(
        text="Wallet + bank combined"
    )

    await interaction.response.send_message(
        embed=embed
    )


@bot.tree.command(
    name="slots",
    description="Play animated 777 Royale Slots.",
)
@app_commands.describe(
    bet="How many wallet coins to wager."
)
async def slots(
    interaction: discord.Interaction,
    bet: app_commands.Range[int, 1, 1_000_000_000],
):
    await execute_slots_spin(
        interaction,
        bet,
    )


@bot.tree.command(
    name="blackjack",
    description="Play interactive 777 Royale Blackjack.",
)
@app_commands.describe(
    bet="How many wallet coins to wager."
)
async def blackjack(
    interaction: discord.Interaction,
    bet: app_commands.Range[int, 1, 1_000_000_000],
):
    await start_blackjack_game(
        interaction,
        bet,
    )


@bot.tree.command(
    name="roulette",
    description="Play animated 777 roulette.",
)
@app_commands.describe(
    bet="Wallet coins to wager.",
    bet_type="colour, parity, or number.",
    selection="red/black/green, odd/even, or 0-36.",
)
async def roulette(
    interaction: discord.Interaction,
    bet: app_commands.Range[int, 1, 1_000_000_000],
    bet_type: str,
    selection: str,
):
    await execute_roulette(interaction, bet, bet_type, selection)


@bot.tree.command(
    name="shop",
    description="Browse the 777 economy shop.",
)
async def shop(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛒 777 Shop",
        description="Use `/buy item amount` to purchase an item.",
        colour=GOLD_COLOUR,
    )

    for key, item in FULL_ECONOMY_SHOP.items():
        embed.add_field(
            name=f"{item['emoji']} {item['name']} (`{key}`)",
            value=f"{item['description']}\nPrice: **{format_coins(item['price'])}**",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="buy",
    description="Buy an item from the shop.",
)
async def buy(
    interaction: discord.Interaction,
    item: str,
    amount: app_commands.Range[int, 1, 100] = 1,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    item = item.lower().strip()
    data = FULL_ECONOMY_SHOP.get(item)

    if not data:
        await interaction.response.send_message(
            "Unknown item. Use `/shop` to see item keys.",
            ephemeral=True,
        )
        return

    total = data["price"] * amount
    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        interaction.user.id,
    )

    if account["wallet"] < total:
        await interaction.response.send_message(
            f"You need {format_coins(total)}.",
            ephemeral=True,
        )
        return

    charged, wallet = await asyncio.to_thread(
        change_wallet,
        interaction.guild.id,
        interaction.user.id,
        -total,
        "shop_purchase",
        f"item={item};amount={amount}",
    )

    if not charged:
        await interaction.response.send_message("Purchase failed.", ephemeral=True)
        return

    await asyncio.to_thread(
        change_inventory,
        interaction.guild.id,
        interaction.user.id,
        item,
        amount,
    )

    await interaction.response.send_message(
        f"{data['emoji']} Bought **{amount}× {data['name']}** for "
        f"**{format_coins(total)}**.\nWallet: **{format_coins(wallet)}**"
    )


@bot.tree.command(
    name="inventory",
    description="View your or another member's inventory.",
)
async def inventory(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    target = member or interaction.user
    rows = await asyncio.to_thread(
        get_inventory,
        interaction.guild.id,
        target.id,
    )

    if not rows:
        await interaction.response.send_message(
            f"{target.display_name}'s inventory is empty."
        )
        return

    lines = []
    for row in rows:
        item = FULL_ECONOMY_SHOP.get(row["item_key"])
        if item:
            lines.append(
                f"{item['emoji']} **{item['name']}** × `{row['quantity']}`"
            )

    embed = discord.Embed(
        title=f"🎒 {target.display_name}'s Inventory",
        description="\n".join(lines),
        colour=GOLD_COLOUR,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="use",
    description="Use a consumable inventory item.",
)
async def use_item(
    interaction: discord.Interaction,
    item: str,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    item = item.lower().strip()
    data = FULL_ECONOMY_SHOP.get(item)

    if not data or not data["consumable"]:
        await interaction.response.send_message(
            "That item cannot be used.",
            ephemeral=True,
        )
        return

    success, _ = await asyncio.to_thread(
        change_inventory,
        interaction.guild.id,
        interaction.user.id,
        item,
        -1,
    )

    if not success:
        await interaction.response.send_message(
            "You do not own that item.",
            ephemeral=True,
        )
        return

    if item == "golden_ticket":
        tier_roll = random.random()

        if tier_roll < 0.02:
            tier = "MYTHIC"
            reward = random.randint(10000, 25000)
        elif tier_roll < 0.12:
            tier = "LEGENDARY"
            reward = random.randint(3500, 9000)
        elif tier_roll < 0.40:
            tier = "RARE"
            reward = random.randint(1500, 3500)
        else:
            tier = "COMMON"
            reward = random.randint(500, 1500)

        _, wallet = await asyncio.to_thread(
            change_wallet,
            interaction.guild.id,
            interaction.user.id,
            reward,
            "golden_ticket",
            f"tier={tier}",
        )
        message = (
            f"🎟️ **{tier} Golden Ticket!** "
            f"You won **{format_coins(reward)}**. "
            f"Wallet: {format_coins(wallet)}"
        )

    elif item == "xp_boost":
        await asyncio.to_thread(
            set_effect,
            interaction.guild.id,
            interaction.user.id,
            "xp_multiplier",
            2.0,
            60 * 60,
        )
        message = (
            "⚡ **Double XP activated for one hour.**"
        )

    elif item == "mystery_crate":
        await interaction.response.defer()

        opening = discord.Embed(
            title="📦 Opening Mystery Crate...",
            description="`▰▱▱▱▱`",
            colour=GOLD_COLOUR,
        )
        await interaction.edit_original_response(embed=opening)

        for frame in [
            "`▰▰▱▱▱`",
            "`▰▰▰▱▱`",
            "`▰▰▰▰▱`",
            "`▰▰▰▰▰`",
        ]:
            await asyncio.sleep(0.45)
            opening.description = frame
            try:
                await interaction.edit_original_response(
                    embed=opening
                )
            except discord.HTTPException:
                pass

        roll = random.random()

        if roll < 0.03:
            reward = random.randint(15000, 40000)
            rarity = "MYTHIC"
        elif roll < 0.15:
            reward = random.randint(5000, 12000)
            rarity = "LEGENDARY"
        elif roll < 0.45:
            reward = random.randint(1800, 5000)
            rarity = "RARE"
        else:
            reward = random.randint(400, 1800)
            rarity = "COMMON"

        _, wallet = await asyncio.to_thread(
            change_wallet,
            interaction.guild.id,
            interaction.user.id,
            reward,
            "mystery_crate",
            f"rarity={rarity}",
        )

        result_embed = discord.Embed(
            title=f"📦 {rarity} Crate Reward",
            description=(
                f"You found **{format_coins(reward)}**!"
            ),
            colour=GOLD_COLOUR,
        )
        result_embed.add_field(
            name="Wallet",
            value=format_coins(wallet),
            inline=False,
        )
        await interaction.edit_original_response(
            embed=result_embed
        )
        return

    else:
        message = "Item used."

    await interaction.response.send_message(message)


@bot.tree.command(
    name="sell",
    description="Sell an inventory item back to the shop.",
)
async def sell(
    interaction: discord.Interaction,
    item: str,
    amount: app_commands.Range[int, 1, 100] = 1,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    item = item.lower().strip()
    data = FULL_ECONOMY_SHOP.get(item)

    if not data:
        await interaction.response.send_message("Unknown item.", ephemeral=True)
        return

    success, _ = await asyncio.to_thread(
        change_inventory,
        interaction.guild.id,
        interaction.user.id,
        item,
        -amount,
    )

    if not success:
        await interaction.response.send_message(
            "You do not own enough of that item.",
            ephemeral=True,
        )
        return

    payout = data["sell"] * amount
    _, wallet = await asyncio.to_thread(
        change_wallet,
        interaction.guild.id,
        interaction.user.id,
        payout,
        "shop_sale",
        f"item={item};amount={amount}",
    )

    await interaction.response.send_message(
        f"Sold **{amount}× {data['name']}** for "
        f"**{format_coins(payout)}**.\nWallet: **{format_coins(wallet)}**"
    )


async def run_minigame_command(
    interaction: discord.Interaction,
    action: str,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    success, description, reward, wallet = await asyncio.to_thread(
        perform_economy_minigame,
        interaction.guild.id,
        interaction.user.id,
        action,
    )

    if not success:
        await interaction.response.send_message(description, ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🎮 {action.title()} Result",
        description=description,
        colour=GOLD_COLOUR,
    )

    embed.add_field(
        name="Result",
        value=(
            f"+{format_coins(reward)}"
            if reward >= 0
            else f"-{format_coins(abs(reward))}"
        ),
        inline=True,
    )
    embed.add_field(name="Wallet", value=format_coins(wallet), inline=True)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="fish", description="Go fishing for coins.")
async def fish(interaction: discord.Interaction):
    await run_minigame_command(interaction, "fish")


@bot.tree.command(name="mine", description="Mine resources for coins.")
async def mine(interaction: discord.Interaction):
    await run_minigame_command(interaction, "mine")


@bot.tree.command(name="hunt", description="Go hunting for rewards.")
async def hunt(interaction: discord.Interaction):
    await run_minigame_command(interaction, "hunt")


@bot.tree.command(name="crime", description="Attempt a risky crime.")
async def crime(interaction: discord.Interaction):
    await run_minigame_command(interaction, "crime")


@bot.tree.command(name="heist", description="Attempt a high-risk heist.")
async def heist(interaction: discord.Interaction):
    await run_minigame_command(interaction, "heist")


@bot.tree.command(
    name="profile",
    description="View an economy profile.",
)
async def profile(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    target = member or interaction.user
    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        target.id,
    )
    stats = await asyncio.to_thread(
        get_user_stats,
        interaction.guild.id,
        target.id,
    )
    achievements = await asyncio.to_thread(
        get_achievements,
        interaction.guild.id,
        target.id,
    )

    embed = discord.Embed(
        title=f"👤 {target.display_name}'s Economy Profile",
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(
        name="Net Worth",
        value=format_coins(account["wallet"] + account["bank"]),
        inline=True,
    )
    embed.add_field(name="Level", value=str(stats["level"]), inline=True)
    embed.add_field(name="XP", value=f"{stats['xp']:,}", inline=True)
    embed.add_field(name="Prestige", value=str(stats["prestige"]), inline=True)
    embed.add_field(name="Wins", value=str(stats["games_won"]), inline=True)
    embed.add_field(name="Losses", value=str(stats["games_lost"]), inline=True)
    embed.add_field(
        name="Biggest Win",
        value=format_coins(stats["biggest_win"]),
        inline=True,
    )
    embed.add_field(
        name="Achievements",
        value=str(len(achievements)),
        inline=True,
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="stats",
    description="View detailed casino statistics.",
)
async def stats(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    stats_data = await asyncio.to_thread(
        get_user_stats,
        interaction.guild.id,
        interaction.user.id,
    )
    detailed_stats = await asyncio.to_thread(
        get_detailed_game_stats,
        interaction.guild.id,
        interaction.user.id,
    )

    embed = discord.Embed(
        title="📈 777 Casino Statistics",
        colour=GOLD_COLOUR,
    )
    embed.add_field(name="Total Wins", value=str(stats_data["games_won"]), inline=True)
    embed.add_field(name="Total Losses", value=str(stats_data["games_lost"]), inline=True)
    embed.add_field(name="Slots Wins", value=str(stats_data["slots_wins"]), inline=True)
    embed.add_field(name="Blackjack Wins", value=str(stats_data["blackjack_wins"]), inline=True)
    embed.add_field(name="Roulette Wins", value=str(stats_data["roulette_wins"]), inline=True)
    embed.add_field(name="Biggest Win", value=format_coins(stats_data["biggest_win"]), inline=True)
    embed.add_field(
        name="Total Wagered",
        value=format_coins(detailed_stats["total_wagered"]),
        inline=True,
    )
    embed.add_field(
        name="Total Payout",
        value=format_coins(detailed_stats["total_payout"]),
        inline=True,
    )
    net_casino = (
        detailed_stats["total_payout"]
        - detailed_stats["total_wagered"]
    )
    embed.add_field(
        name="Casino Profit/Loss",
        value=(
            f"+{format_coins(net_casino)}"
            if net_casino >= 0
            else f"-{format_coins(abs(net_casino))}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Biggest Bet",
        value=format_coins(detailed_stats["biggest_bet"]),
        inline=True,
    )
    embed.add_field(
        name="Biggest Jackpot",
        value=format_coins(detailed_stats["biggest_jackpot"]),
        inline=True,
    )
    embed.add_field(
        name="Games Played",
        value=(
            f"Slots: {detailed_stats['slots_spins']}\n"
            f"Blackjack: {detailed_stats['blackjack_games']}\n"
            f"Roulette: {detailed_stats['roulette_spins']}"
        ),
        inline=True,
    )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="achievements",
    description="View your unlocked achievements.",
)
async def achievements(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    newly_unlocked = await asyncio.to_thread(
        evaluate_achievements,
        interaction.guild.id,
        interaction.user.id,
    )
    keys = await asyncio.to_thread(
        get_achievements,
        interaction.guild.id,
        interaction.user.id,
    )

    if not keys:
        await interaction.response.send_message(
            "No achievements unlocked yet.",
            ephemeral=True,
        )
        return

    lines = []
    for key in keys:
        name, description = ACHIEVEMENT_DEFINITIONS[key]
        lines.append(f"🏆 **{name}** — {description}")

    embed = discord.Embed(
        title="🏆 Achievements",
        description="\n".join(lines),
        colour=GOLD_COLOUR,
    )

    if newly_unlocked:
        embed.set_footer(text=f"Newly unlocked: {len(newly_unlocked)}")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="prestige",
    description="Reset your economy for a permanent prestige rank.",
)
async def prestige(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Server only.", ephemeral=True)
        return

    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        interaction.user.id,
    )

    required = 10_000_000
    net_worth = account["wallet"] + account["bank"]

    if net_worth < required:
        await interaction.response.send_message(
            f"You need a net worth of **{format_coins(required)}** to prestige.",
            ephemeral=True,
        )
        return

    with economy_lock, economy_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE economy_users
            SET wallet = ?, bank = 0
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                ECONOMY_STARTING_BALANCE,
                interaction.guild.id,
                interaction.user.id,
            ),
        )
        connection.execute(
            """
            UPDATE economy_stats
            SET prestige = prestige + 1
            WHERE guild_id = ? AND user_id = ?
            """,
            (interaction.guild.id, interaction.user.id),
        )
        connection.commit()

    await asyncio.to_thread(
        unlock_achievement,
        interaction.guild.id,
        interaction.user.id,
        "prestige_1",
    )

    await interaction.response.send_message(
        "✨ **Prestige complete!** Your balance was reset and your prestige rank increased."
    )


@bot.tree.command(
    name="help",
    description="Open the interactive 777 command guide.",
)
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="777 Help Centre",
        description=(
            "Choose a category below to browse commands.\n\n"
            "The bot includes community tools, economy, casino games, "
            "shop items, activities, giveaways, and staff controls."
        ),
        colour=GOLD_COLOUR,
    )
    embed.set_footer(text="777 • Interactive Help")

    await interaction.response.send_message(
        embed=embed,
        view=HelpView(interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(
    name="economy_add",
    description="Admin: add wallet coins to a member.",
)
async def economy_add(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 1, 1_000_000_000_000],
    reason: str = "Administrative adjustment",
):
    if interaction.guild is None or not await require_economy_admin(interaction):
        return

    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        member.id,
    )
    previous = account["wallet"]
    new_wallet = clamp_balance(previous + amount)

    previous, new_wallet = await asyncio.to_thread(
        admin_set_wallet,
        interaction.guild.id,
        member.id,
        new_wallet,
    )

    audit_id = await asyncio.to_thread(
        record_admin_audit,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "ADD",
        amount,
        previous,
        new_wallet,
        reason,
    )

    await interaction.response.send_message(
        f"Added **{format_coins(amount)}** to {member.mention}.\n"
        f"Wallet: **{format_coins(new_wallet)}**\n"
        f"Audit ID: `{audit_id}`"
    )

    await send_economy_audit_embed(
        interaction.guild,
        interaction.user,
        member,
        "ADD",
        amount,
        previous,
        new_wallet,
        reason,
        audit_id,
    )


@bot.tree.command(
    name="economy_remove",
    description="Admin: remove wallet coins from a member.",
)
async def economy_remove(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 1, 1_000_000_000_000],
    reason: str = "Administrative adjustment",
):
    if interaction.guild is None or not await require_economy_admin(interaction):
        return

    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        member.id,
    )
    previous = account["wallet"]
    new_wallet = max(0, previous - amount)

    previous, new_wallet = await asyncio.to_thread(
        admin_set_wallet,
        interaction.guild.id,
        member.id,
        new_wallet,
    )

    audit_id = await asyncio.to_thread(
        record_admin_audit,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "REMOVE",
        amount,
        previous,
        new_wallet,
        reason,
    )

    await interaction.response.send_message(
        f"Removed up to **{format_coins(amount)}** from {member.mention}.\n"
        f"Wallet: **{format_coins(new_wallet)}**\n"
        f"Audit ID: `{audit_id}`"
    )

    await send_economy_audit_embed(
        interaction.guild,
        interaction.user,
        member,
        "REMOVE",
        amount,
        previous,
        new_wallet,
        reason,
        audit_id,
    )


@bot.tree.command(
    name="economy_set",
    description="Admin: set a member's wallet balance.",
)
async def economy_set(
    interaction: discord.Interaction,
    member: discord.Member,
    amount: app_commands.Range[int, 0, 9_000_000_000_000_000],
    reason: str = "Administrative adjustment",
):
    if interaction.guild is None or not await require_economy_admin(interaction):
        return

    previous, new_wallet = await asyncio.to_thread(
        admin_set_wallet,
        interaction.guild.id,
        member.id,
        amount,
    )

    audit_id = await asyncio.to_thread(
        record_admin_audit,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "SET",
        amount,
        previous,
        new_wallet,
        reason,
    )

    await interaction.response.send_message(
        f"Set {member.mention}'s wallet to "
        f"**{format_coins(new_wallet)}**.\n"
        f"Audit ID: `{audit_id}`"
    )

    await send_economy_audit_embed(
        interaction.guild,
        interaction.user,
        member,
        "SET",
        amount,
        previous,
        new_wallet,
        reason,
        audit_id,
    )


@bot.tree.command(
    name="economy_reset",
    description="Admin: reset a member's wallet and bank.",
)
async def economy_reset(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "Administrative reset",
):
    if interaction.guild is None or not await require_economy_admin(interaction):
        return

    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        member.id,
    )
    previous = account["wallet"]

    with economy_lock, economy_connection() as connection:
        connection.execute(
            """
            UPDATE economy_users
            SET wallet = ?, bank = 0
            WHERE guild_id = ? AND user_id = ?
            """,
            (
                ECONOMY_STARTING_BALANCE,
                interaction.guild.id,
                member.id,
            ),
        )
        connection.commit()

    audit_id = await asyncio.to_thread(
        record_admin_audit,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "RESET",
        None,
        previous,
        ECONOMY_STARTING_BALANCE,
        reason,
    )

    await interaction.response.send_message(
        f"Reset {member.mention}'s economy account.\n"
        f"Audit ID: `{audit_id}`"
    )


@bot.tree.command(
    name="economy_lookup",
    description="Admin: inspect a member's economy account.",
)
async def economy_lookup(
    interaction: discord.Interaction,
    member: discord.Member,
):
    if interaction.guild is None or not await require_economy_admin(interaction):
        return

    account = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        member.id,
    )
    stats_data = await asyncio.to_thread(
        get_user_stats,
        interaction.guild.id,
        member.id,
    )
    detailed = await asyncio.to_thread(
        get_detailed_game_stats,
        interaction.guild.id,
        member.id,
    )

    embed = discord.Embed(
        title=f"🔎 Economy Lookup • {member.display_name}",
        colour=GOLD_COLOUR,
    )
    embed.add_field(name="Wallet", value=format_coins(account["wallet"]), inline=True)
    embed.add_field(name="Bank", value=format_coins(account["bank"]), inline=True)
    embed.add_field(
        name="Net Worth",
        value=format_coins(account["wallet"] + account["bank"]),
        inline=True,
    )
    embed.add_field(name="Level", value=str(stats_data["level"]), inline=True)
    embed.add_field(name="Prestige", value=str(stats_data["prestige"]), inline=True)
    embed.add_field(
        name="Total Wagered",
        value=format_coins(detailed["total_wagered"]),
        inline=True,
    )
    embed.add_field(
        name="Total Payout",
        value=format_coins(detailed["total_payout"]),
        inline=True,
    )
    embed.add_field(
        name="Biggest Bet",
        value=format_coins(detailed["biggest_bet"]),
        inline=True,
    )
    embed.add_field(
        name="Biggest Jackpot",
        value=format_coins(detailed["biggest_jackpot"]),
        inline=True,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="economy_rollback",
    description="Admin: restore the wallet from an audit entry.",
)
async def economy_rollback(
    interaction: discord.Interaction,
    audit_id: int,
    reason: str = "Audit rollback",
):
    if interaction.guild is None or not await require_economy_admin(interaction):
        return

    entry = await asyncio.to_thread(
        get_audit_entry,
        interaction.guild.id,
        audit_id,
    )

    if not entry:
        await interaction.response.send_message(
            "Audit entry not found.",
            ephemeral=True,
        )
        return

    if entry["previous_wallet"] is None:
        await interaction.response.send_message(
            "That audit entry cannot be rolled back.",
            ephemeral=True,
        )
        return

    member = interaction.guild.get_member(entry["target_id"])

    if member is None:
        await interaction.response.send_message(
            "The target member is no longer in the server.",
            ephemeral=True,
        )
        return

    current = await asyncio.to_thread(
        get_economy_account,
        interaction.guild.id,
        member.id,
    )

    previous, restored = await asyncio.to_thread(
        admin_set_wallet,
        interaction.guild.id,
        member.id,
        int(entry["previous_wallet"]),
    )

    new_audit_id = await asyncio.to_thread(
        record_admin_audit,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "ROLLBACK",
        None,
        current["wallet"],
        restored,
        f"{reason}; source_audit={audit_id}",
    )

    await interaction.response.send_message(
        f"Rolled back audit `{audit_id}` for {member.mention}.\n"
        f"Wallet restored to **{format_coins(restored)}**.\n"
        f"New audit ID: `{new_audit_id}`"
    )


@bot.tree.command(
    name="ban",
    description="Ban a member from the server.",
)
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(
    member="Member to ban.",
    reason="Reason for the ban.",
    delete_message_days="Delete up to seven days of their messages.",
)
async def ban_member(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
    delete_message_days: app_commands.Range[int, 0, 7] = 0,
):
    if not await moderation_precheck(interaction, member):
        return

    await interaction.response.defer(ephemeral=True)

    try:
        await interaction.guild.ban(
            member,
            reason=(
                f"{reason} | Moderator: "
                f"{interaction.user} ({interaction.user.id})"
            ),
            delete_message_seconds=delete_message_days * 86400,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I do not have permission to ban that member.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"The ban failed: {error}",
            ephemeral=True,
        )
        return

    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "BAN",
        reason,
    )

    await interaction.followup.send(
        f"🔨 Banned **{member}**. Case `{case_id}`.",
        ephemeral=True,
    )
    await send_mod_log(
        interaction.guild,
        "Member Banned",
        interaction.user,
        member,
        reason,
        case_id,
        extra=f"Deleted message history: {delete_message_days} day(s)",
    )


@bot.tree.command(
    name="unban",
    description="Unban a user by Discord user ID.",
)
@app_commands.checks.has_permissions(ban_members=True)
async def unban_user(
    interaction: discord.Interaction,
    user_id: str,
    reason: str = "No reason provided",
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Server only.",
            ephemeral=True,
        )
        return

    try:
        parsed_id = int(user_id)
        user = await bot.fetch_user(parsed_id)
    except (ValueError, discord.NotFound, discord.HTTPException):
        await interaction.response.send_message(
            "Enter a valid Discord user ID.",
            ephemeral=True,
        )
        return

    try:
        await interaction.guild.unban(
            user,
            reason=(
                f"{reason} | Moderator: "
                f"{interaction.user} ({interaction.user.id})"
            ),
        )
    except discord.NotFound:
        await interaction.response.send_message(
            "That user is not currently banned.",
            ephemeral=True,
        )
        return
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to unban users.",
            ephemeral=True,
        )
        return

    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        user.id,
        "UNBAN",
        reason,
    )

    await interaction.response.send_message(
        f"✅ Unbanned **{user}**. Case `{case_id}`.",
        ephemeral=True,
    )
    await send_mod_log(
        interaction.guild,
        "User Unbanned",
        interaction.user,
        user,
        reason,
        case_id,
    )


@bot.tree.command(
    name="kick",
    description="Kick a member from the server.",
)
@app_commands.checks.has_permissions(kick_members=True)
async def kick_member(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided",
):
    if not await moderation_precheck(interaction, member):
        return

    await interaction.response.defer(ephemeral=True)

    try:
        await member.kick(
            reason=(
                f"{reason} | Moderator: "
                f"{interaction.user} ({interaction.user.id})"
            )
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I do not have permission to kick that member.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"The kick failed: {error}",
            ephemeral=True,
        )
        return

    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "KICK",
        reason,
    )

    await interaction.followup.send(
        f"👢 Kicked **{member}**. Case `{case_id}`.",
        ephemeral=True,
    )
    await send_mod_log(
        interaction.guild,
        "Member Kicked",
        interaction.user,
        member,
        reason,
        case_id,
    )


@bot.tree.command(
    name="timeout",
    description="Temporarily timeout a member.",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout_member(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "No reason provided",
):
    if not await moderation_precheck(interaction, member):
        return

    until = discord.utils.utcnow() + timedelta(
        minutes=minutes
    )

    try:
        await member.timeout(
            until,
            reason=(
                f"{reason} | Moderator: "
                f"{interaction.user} ({interaction.user.id})"
            ),
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to timeout that member.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as error:
        await interaction.response.send_message(
            f"The timeout failed: {error}",
            ephemeral=True,
        )
        return

    duration_seconds = minutes * 60
    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "TIMEOUT",
        reason,
        duration_seconds,
    )

    await interaction.response.send_message(
        f"⏳ Timed out {member.mention} for **{minutes} minute(s)**. "
        f"Case `{case_id}`.",
        ephemeral=True,
    )
    await send_mod_log(
        interaction.guild,
        "Member Timed Out",
        interaction.user,
        member,
        reason,
        case_id,
        duration=f"{minutes} minute(s)",
    )


@bot.tree.command(
    name="untimeout",
    description="Remove a member's timeout.",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout_member(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "Timeout removed",
):
    if not await moderation_precheck(interaction, member):
        return

    try:
        await member.timeout(
            None,
            reason=(
                f"{reason} | Moderator: "
                f"{interaction.user} ({interaction.user.id})"
            ),
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to remove that timeout.",
            ephemeral=True,
        )
        return

    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "UNTIMEOUT",
        reason,
    )

    await interaction.response.send_message(
        f"✅ Removed {member.mention}'s timeout. Case `{case_id}`.",
        ephemeral=True,
    )
    await send_mod_log(
        interaction.guild,
        "Timeout Removed",
        interaction.user,
        member,
        reason,
        case_id,
    )


@bot.tree.command(
    name="warn",
    description="Add a persistent warning to a member.",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def warn_member(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str,
):
    if not await moderation_precheck(interaction, member):
        return

    warning_id = await asyncio.to_thread(
        add_warning,
        interaction.guild.id,
        member.id,
        interaction.user.id,
        reason,
    )

    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "WARN",
        reason,
    )

    try:
        await member.send(
            f"You were warned in **{interaction.guild.name}**.\n"
            f"Reason: **{reason}**\nWarning ID: `{warning_id}`"
        )
    except (discord.Forbidden, discord.HTTPException):
        pass

    await interaction.response.send_message(
        f"⚠️ Warned {member.mention}. Warning `{warning_id}`, "
        f"case `{case_id}`.",
        ephemeral=True,
    )
    await send_mod_log(
        interaction.guild,
        "Member Warned",
        interaction.user,
        member,
        reason,
        case_id,
        extra=f"Warning ID: {warning_id}",
    )


@bot.tree.command(
    name="warnings",
    description="View a member's active warnings.",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def view_warnings(
    interaction: discord.Interaction,
    member: discord.Member,
):
    if interaction.guild is None:
        return

    warnings_data = await asyncio.to_thread(
        get_active_warnings,
        interaction.guild.id,
        member.id,
    )

    if not warnings_data:
        await interaction.response.send_message(
            f"{member.mention} has no active warnings.",
            ephemeral=True,
        )
        return

    lines = []
    for warning in warnings_data:
        timestamp = datetime.fromtimestamp(
            warning["created_at"],
            tz=timezone.utc,
        )
        lines.append(
            f"`#{warning['id']}` • "
            f"{discord.utils.format_dt(timestamp, style='d')} • "
            f"{warning['reason'][:180]}"
        )

    embed = discord.Embed(
        title=f"⚠️ Warnings • {member}",
        description="\n".join(lines),
        colour=GOLD_COLOUR,
    )
    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="clear_warnings",
    description="Clear all active warnings for a member.",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def clear_warnings_command(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "Warnings cleared",
):
    if interaction.guild is None:
        return

    cleared = await asyncio.to_thread(
        clear_active_warnings,
        interaction.guild.id,
        member.id,
    )

    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        member.id,
        "CLEAR_WARNINGS",
        reason,
    )

    await interaction.response.send_message(
        f"Cleared **{cleared}** warning(s) for {member.mention}. "
        f"Case `{case_id}`.",
        ephemeral=True,
    )
    await send_mod_log(
        interaction.guild,
        "Warnings Cleared",
        interaction.user,
        member,
        reason,
        case_id,
        extra=f"Warnings cleared: {cleared}",
    )


@bot.tree.command(
    name="purge",
    description="Delete recent messages from the current channel.",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def purge_messages(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 500],
    member: discord.Member | None = None,
):
    if not isinstance(
        interaction.channel,
        discord.TextChannel,
    ):
        await interaction.response.send_message(
            "This command requires a text channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    check = (
        (lambda message: message.author.id == member.id)
        if member
        else None
    )

    try:
        deleted = await interaction.channel.purge(
            limit=amount,
            check=check,
            reason=f"Purge by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I do not have permission to delete messages here.",
            ephemeral=True,
        )
        return

    reason = (
        f"Purged {len(deleted)} messages"
        + (f" from {member}" if member else "")
    )
    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        member.id if member else None,
        "PURGE",
        reason,
    )

    await interaction.followup.send(
        f"🧹 Deleted **{len(deleted)}** message(s). "
        f"Case `{case_id}`.",
        ephemeral=True,
    )
    await send_mod_log(
        interaction.guild,
        "Messages Purged",
        interaction.user,
        member,
        reason,
        case_id,
        extra=f"Channel: {interaction.channel.mention}",
    )


@bot.tree.command(
    name="lock",
    description="Lock the current text channel.",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def lock_channel(
    interaction: discord.Interaction,
    reason: str = "Channel locked",
):
    if not isinstance(
        interaction.channel,
        discord.TextChannel,
    ):
        await interaction.response.send_message(
            "This command requires a text channel.",
            ephemeral=True,
        )
        return

    overwrite = interaction.channel.overwrites_for(
        interaction.guild.default_role
    )
    overwrite.send_messages = False

    try:
        await interaction.channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason=f"{reason} | {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to lock this channel.",
            ephemeral=True,
        )
        return

    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        None,
        "LOCK",
        reason,
    )

    await interaction.response.send_message(
        f"🔒 {interaction.channel.mention} is now locked. "
        f"Case `{case_id}`."
    )
    await send_mod_log(
        interaction.guild,
        "Channel Locked",
        interaction.user,
        None,
        reason,
        case_id,
        extra=f"Channel: {interaction.channel.mention}",
    )


@bot.tree.command(
    name="unlock",
    description="Unlock the current text channel.",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock_channel(
    interaction: discord.Interaction,
    reason: str = "Channel unlocked",
):
    if not isinstance(
        interaction.channel,
        discord.TextChannel,
    ):
        await interaction.response.send_message(
            "This command requires a text channel.",
            ephemeral=True,
        )
        return

    overwrite = interaction.channel.overwrites_for(
        interaction.guild.default_role
    )
    overwrite.send_messages = None

    try:
        await interaction.channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason=f"{reason} | {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to unlock this channel.",
            ephemeral=True,
        )
        return

    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        None,
        "UNLOCK",
        reason,
    )

    await interaction.response.send_message(
        f"🔓 {interaction.channel.mention} is now unlocked. "
        f"Case `{case_id}`."
    )
    await send_mod_log(
        interaction.guild,
        "Channel Unlocked",
        interaction.user,
        None,
        reason,
        case_id,
        extra=f"Channel: {interaction.channel.mention}",
    )


@bot.tree.command(
    name="slowmode",
    description="Set the current channel's slowmode.",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def set_slowmode(
    interaction: discord.Interaction,
    seconds: app_commands.Range[int, 0, 21600],
    reason: str = "Slowmode updated",
):
    if not isinstance(
        interaction.channel,
        discord.TextChannel,
    ):
        await interaction.response.send_message(
            "This command requires a text channel.",
            ephemeral=True,
        )
        return

    try:
        await interaction.channel.edit(
            slowmode_delay=seconds,
            reason=f"{reason} | {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to change slowmode.",
            ephemeral=True,
        )
        return

    case_id = await asyncio.to_thread(
        record_moderation_action,
        interaction.guild.id,
        interaction.user.id,
        None,
        "SLOWMODE",
        reason,
        seconds,
    )

    await interaction.response.send_message(
        (
            "🚦 Slowmode disabled."
            if seconds == 0
            else f"🚦 Slowmode set to **{seconds} seconds**."
        )
        + f" Case `{case_id}`."
    )


@bot.tree.command(
    name="security_status",
    description="View the bot's active security configuration.",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def security_status(
    interaction: discord.Interaction,
):
    embed = discord.Embed(
        title="🛡️ 777 Security Status",
        colour=GOLD_COLOUR,
    )
    embed.add_field(
        name="Anti-Spam",
        value=(
            f"{'Enabled' if ANTI_SPAM_ENABLED else 'Disabled'}\n"
            f"{ANTI_SPAM_MESSAGE_LIMIT} messages / "
            f"{ANTI_SPAM_WINDOW_SECONDS}s\n"
            f"Timeout: {ANTI_SPAM_TIMEOUT_MINUTES}m"
        ),
        inline=True,
    )
    embed.add_field(
        name="Anti-Raid",
        value=(
            f"{'Enabled' if ANTI_RAID_ENABLED else 'Disabled'}\n"
            f"{ANTI_RAID_JOIN_LIMIT} joins / "
            f"{ANTI_RAID_WINDOW_SECONDS}s\n"
            f"New-account threshold: "
            f"{ANTI_RAID_ACCOUNT_AGE_HOURS}h"
        ),
        inline=True,
    )
    embed.add_field(
        name="Automatic New-Account Timeout",
        value=(
            "Enabled"
            if ANTI_RAID_AUTO_TIMEOUT
            else "Disabled (alert only)"
        ),
        inline=True,
    )
    embed.add_field(
        name="Moderation Log",
        value=(
            f"<#{MOD_LOG_CHANNEL_ID}>"
            if MOD_LOG_CHANNEL_ID
            else "Not configured"
        ),
        inline=True,
    )
    embed.add_field(
        name="Security Alerts",
        value=(
            f"<#{SECURITY_ALERT_CHANNEL_ID}>"
            if SECURITY_ALERT_CHANNEL_ID
            else "Not configured"
        ),
        inline=True,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="giveaway",
    description="Create a timed giveaway with automatic winners.",
)
@app_commands.describe(
    prize="The prize being given away.",
    winners="How many winners to select.",
    duration="How long the giveaway lasts, from 15 to 86400 seconds.",
    required_role="Optional role members must have to enter.",
)
async def giveaway(
    interaction: discord.Interaction,
    prize: app_commands.Range[str, 1, 200],
    winners: app_commands.Range[int, 1, 20] = 1,
    duration: app_commands.Range[int, 15, 86400] = 300,
    required_role: discord.Role | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    permissions = getattr(
        interaction.user,
        "guild_permissions",
        None,
    )

    if not permissions or not permissions.manage_messages:
        await interaction.response.send_message(
            "You need **Manage Messages** to create giveaways.",
            ephemeral=True,
        )
        return

    if required_role is not None and required_role.is_default():
        required_role = None

    view = GiveawayView(
        prize=prize.strip(),
        creator=interaction.user,
        winner_count=winners,
        duration_seconds=duration,
        required_role=required_role,
    )

    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view,
    )

    view.message = await interaction.original_response()
    await view.start_countdown()


@bot.tree.command(
    name="suggest",
    description="Submit a suggestion for the server.",
)
@app_commands.describe(
    idea="The suggestion you want to submit."
)
async def suggest(
    interaction: discord.Interaction,
    idea: app_commands.Range[str, 5, 1500],
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    suggestions_channel = get_suggestions_channel(
        interaction.guild
    )

    if suggestions_channel is None:
        await interaction.response.send_message(
            "The suggestions channel has not been configured. "
            "Add `SUGGESTIONS_CHANNEL_ID` in Render.",
            ephemeral=True,
        )
        return

    view = SuggestionView(
        author=interaction.user,
        suggestion_text=idea.strip(),
    )

    try:
        suggestion_message = await suggestions_channel.send(
            embed=view.build_embed(),
            view=view,
        )

        view.message = suggestion_message

        try:
            await suggestion_message.create_thread(
                name=f"Suggestion by {interaction.user.display_name}"[:100],
                auto_archive_duration=1440,
                reason="Discussion thread for a 777 suggestion.",
            )
        except (
            discord.Forbidden,
            discord.HTTPException,
        ):
            pass

    except discord.Forbidden:
        await interaction.response.send_message(
            "I cannot post in the suggestions channel. "
            "Check my permissions there.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Your suggestion was posted: {suggestion_message.jump_url}",
        ephemeral=True,
    )


@bot.tree.command(
    name="poll",
    description="Create a button poll with up to five options.",
)
@app_commands.describe(
    question="The question people will vote on.",
    option1="The first option.",
    option2="The second option.",
    option3="Optional third option.",
    option4="Optional fourth option.",
    option5="Optional fifth option.",
    duration="How long voting stays open, from 15 to 3600 seconds.",
    anonymous="Whether voter choices should remain anonymous.",
)
async def poll(
    interaction: discord.Interaction,
    question: app_commands.Range[str, 1, 200],
    option1: app_commands.Range[str, 1, 80],
    option2: app_commands.Range[str, 1, 80],
    option3: app_commands.Range[str, 1, 80] | None = None,
    option4: app_commands.Range[str, 1, 80] | None = None,
    option5: app_commands.Range[str, 1, 80] | None = None,
    duration: app_commands.Range[int, 15, 3600] = 60,
    anonymous: bool = True,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    options = [
        option
        for option in [
            option1,
            option2,
            option3,
            option4,
            option5,
        ]
        if option is not None
    ]

    normalized_options = [
        option.strip().casefold()
        for option in options
    ]

    if len(set(normalized_options)) != len(options):
        await interaction.response.send_message(
            "Every poll option must be different.",
            ephemeral=True,
        )
        return

    view = PollView(
        question=question.strip(),
        options=[option.strip() for option in options],
        creator=interaction.user,
        duration_seconds=duration,
        anonymous=anonymous,
    )

    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view,
    )

    view.message = await interaction.original_response()
    await view.start_countdown()


@bot.tree.command(
    name="smash_or_pass",
    description="Start a Smash or Pass vote for a server member.",
)
@app_commands.describe(
    member="The member everyone will vote on.",
    duration="How long voting stays open, from 15 to 300 seconds.",
)
async def smash_or_pass(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: app_commands.Range[int, 15, 300] = 60,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    if member.bot:
        await interaction.response.send_message(
            "You cannot start Smash or Pass for a bot.",
            ephemeral=True,
        )
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You cannot start Smash or Pass for yourself.",
            ephemeral=True,
        )
        return

    view = SmashOrPassView(
        target=member,
        creator=interaction.user,
        duration_seconds=duration,
    )

    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view,
    )

    view.message = await interaction.original_response()
    await view.start_countdown()


@bot.tree.command(
    name="ping",
    description="Check whether 777 is online.",
)
async def ping(
    interaction: discord.Interaction,
):
    latency = round(bot.latency * 1000)

    embed = discord.Embed(
        title="🏓 777 is online",
        description=f"Current latency: `{latency}ms`",
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

    await interaction.response.send_message(embed=embed)



@bot.tree.command(
    name="counting",
    description="View the current counting streak and record.",
)
async def counting_status(
    interaction: discord.Interaction,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    state = get_counting_state(interaction.guild.id)
    current = int(state["current"])
    highest = int(state["highest"])

    next_number = current + 1

    counting_channel_id = configured_counting_channel_id()
    channel_text = (
        f"<#{counting_channel_id}>"
        if counting_channel_id
        else "Not configured"
    )

    embed = discord.Embed(
        title="🔢 777 Counting",
        description=(
            f"The next number is **{next_number}**."
        ),
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )

    embed.add_field(
        name="Current streak",
        value=f"`{current}`",
        inline=True,
    )

    embed.add_field(
        name="Highest streak",
        value=f"`{highest}`",
        inline=True,
    )

    embed.add_field(
        name="Channel",
        value=channel_text,
        inline=False,
    )

    embed.add_field(
        name="Rules",
        value=(
            "Count one number at a time, and do not "
            "count twice in a row."
        ),
        inline=False,
    )

    embed.set_footer(text="777 • Counting")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="counting_reset",
    description="Reset the counting streak. Administrators only.",
)
@app_commands.checks.has_permissions(administrator=True)
async def counting_reset_command(
    interaction: discord.Interaction,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used in a server.",
            ephemeral=True,
        )
        return

    state = get_counting_state(interaction.guild.id)
    state["current"] = 0
    state["last_user_id"] = None
    state["last_message_id"] = None

    save_counting_states(counting_states)

    embed = discord.Embed(
        title="🔄 Counting Reset",
        description=(
            f"{interaction.user.mention} reset the counting streak.\n"
            "The next number is **1**."
        ),
        colour=GOLD_COLOUR,
        timestamp=datetime.now(timezone.utc),
    )

    embed.set_footer(text="777 • Counting")

    await interaction.response.send_message(embed=embed)


@counting_reset_command.error
async def counting_reset_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "Only server administrators can reset counting.",
            ephemeral=True,
        )
        return

    logger.exception("Counting reset command failed: %s", error)

    if not interaction.response.is_done():
        await interaction.response.send_message(
            "Something went wrong while resetting counting.",
            ephemeral=True,
        )


@bot.tree.command(
    name="clip",
    description="Save a Discord message in the clips channel.",
)
@app_commands.describe(
    message_link="Paste the Discord message link you want to clip."
)
async def clip_slash(
    interaction: discord.Interaction,
    message_link: str,
):
    await interaction.response.defer(
        thinking=True,
        ephemeral=True,
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

    guild_id = int(match.group(1))
    channel_id = int(match.group(2))
    message_id = int(match.group(3))

    if (
        interaction.guild is None
        or interaction.guild.id != guild_id
    ):
        await interaction.followup.send(
            "That message must be from this server.",
            ephemeral=True,
        )
        return

    channel = interaction.guild.get_channel(channel_id)

    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send(
            "I could not find that text channel.",
            ephemeral=True,
        )
        return

    try:
        message = await channel.fetch_message(message_id)

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

    await send_clipped_message(interaction, message)


@bot.tree.command(
    name="quote",
    description="Turn a Discord message into a quote image.",
)
@app_commands.describe(
    message_link="Paste the Discord message link you want to quote."
)
async def quote_slash(
    interaction: discord.Interaction,
    message_link: str,
):
    await interaction.response.defer(thinking=True)

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

    guild_id = int(match.group(1))
    channel_id = int(match.group(2))
    message_id = int(match.group(3))

    if (
        interaction.guild is None
        or interaction.guild.id != guild_id
    ):
        await interaction.followup.send(
            "That message must be from this server.",
            ephemeral=True,
        )
        return

    channel = interaction.guild.get_channel(channel_id)

    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send(
            "I could not find that text channel.",
            ephemeral=True,
        )
        return

    try:
        message = await channel.fetch_message(message_id)

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

    await send_quote_result(interaction, message)


# =========================================================
# PREFIX QUOTE COMMAND
# =========================================================

@bot.command(name="quote")
@commands.guild_only()
async def quote_reply(
    context: commands.Context,
):
    if not context.message.reference:
        await context.reply(
            "Reply to a message with `!quote` "
            "to turn it into a quote image.",
            mention_author=False,
        )
        return

    resolved = context.message.reference.resolved

    if isinstance(resolved, discord.Message):
        target_message = resolved

    else:
        try:
            target_message = await context.channel.fetch_message(
                context.message.reference.message_id
            )

        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
        ):
            await context.reply(
                "I could not access the message you replied to.",
                mention_author=False,
            )
            return

    if target_message.author.bot:
        await context.reply(
            "I cannot quote another bot's message.",
            mention_author=False,
        )
        return

    quote_text = clean_message_content(target_message)

    if not quote_text:
        await context.reply(
            "That message does not contain any text.",
            mention_author=False,
        )
        return

    async with context.typing():
        try:
            image = await create_quote_image(target_message)
            filename = f"777_quote_{target_message.id}.png"

            file = discord.File(
                image,
                filename=filename,
            )

            embed = discord.Embed(
                title="✦ Make it a Quote ✦",
                description=(
                    f"Quoted **{target_message.author.display_name}**\n"
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
                "Something went wrong while creating the quote.",
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
    if isinstance(error, commands.CommandNotFound):
        return

    logger.error(
        "Prefix command error: %s",
        error,
        exc_info=error,
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
