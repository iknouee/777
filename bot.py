import asyncio
import io
import json
import logging
import os
import re
import random
import time
import textwrap
import threading
from datetime import datetime, timezone
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
# SLASH COMMANDS
# =========================================================

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
