"""URA | Alliances - free alliance bot (discord.py 2.6+, Components V2, SQLite)."""
import os
import random
import re
import sqlite3
import string
import time

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
REQUEST_CHANNEL_ID = int(os.getenv("REQUEST_CHANNEL_ID", "1569683342729674783"))
ALLIANCE_CHANNEL_ID = int(os.getenv("ALLIANCE_CHANNEL_ID", "1569683363625832459"))
DB_PATH = os.getenv("DB_PATH", "alliances.sqlite3")
HANDSHAKE = os.getenv(
    "HANDSHAKE_IMAGE",
    "https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/1f91d.png",
)
MIN_MEMBERS = 50
ACCENT = discord.Colour(0xC23B4E)
BLOCKED_ALLIES = "Void, Endurals, Lucent, Tenyoku, Last Stand and UNL"

DEFAULT_WHAT = (
    "The clan you've been searching for. {name} is an active and competitive clan "
    "focused on growth, teamwork, and reaching the top together."
)
DEFAULT_WHY = (
    "{name} stands out through its competitive mindset, fair leadership, and "
    "commitment to helping every member improve. Our goal is to grow stronger "
    "together and become one of the best clans globally."
)

INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)",
    re.I,
)


# --------------------------------------------------------------------------- storage
class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS alliances (
                owner_id   INTEGER PRIMARY KEY,
                code       TEXT NOT NULL UNIQUE,
                guild_id   INTEGER NOT NULL UNIQUE,
                name       TEXT NOT NULL,
                invite_url TEXT NOT NULL,
                members    INTEGER NOT NULL,
                online     INTEGER NOT NULL,
                bio_type   TEXT NOT NULL,
                bio_what   TEXT NOT NULL,
                bio_why    TEXT NOT NULL,
                icon_url   TEXT NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            """
        )
        self.db.commit()

    def by_owner(self, owner_id: int):
        return self.db.execute("SELECT * FROM alliances WHERE owner_id=?", (owner_id,)).fetchone()

    def by_guild(self, guild_id: int):
        return self.db.execute("SELECT * FROM alliances WHERE guild_id=?", (guild_id,)).fetchone()

    def code_taken(self, code: str) -> bool:
        return self.db.execute("SELECT 1 FROM alliances WHERE code=?", (code,)).fetchone() is not None

    def save(self, rec: dict):
        cols = ",".join(rec)
        marks = ",".join("?" for _ in rec)
        self.db.execute(f"INSERT OR REPLACE INTO alliances ({cols}) VALUES ({marks})", tuple(rec.values()))
        self.db.commit()

    def delete(self, owner_id: int):
        self.db.execute("DELETE FROM alliances WHERE owner_id=?", (owner_id,))
        self.db.commit()

    def get_meta(self, key: str):
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))
        self.db.commit()


store = Store(DB_PATH)


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.panel_checked = False

    async def setup_hook(self):
        self.add_view(build_hub())
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")
        if not self.panel_checked:
            self.panel_checked = True
            try:
                await ensure_panel()
            except discord.HTTPException as e:
                print(f"Could not post the panel: {e}")


bot = Bot()


# --------------------------------------------------------------------------- helpers
class Problem(Exception):
    """A user-facing validation error."""


def sep() -> discord.ui.Separator:
    return discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)


def text(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content)


def clean(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value.strip()))


def new_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(alphabet, k=5))
        if not store.code_taken(code):
            return code


def split_bio(raw: str):
    parts = re.split(r"\n\s*\n", raw.strip(), maxsplit=1)
    what = discord.utils.escape_mentions(parts[0].strip())
    why = discord.utils.escape_mentions(parts[1].strip()) if len(parts) > 1 else ""
    return what, why


async def verify(link: str, members_raw: str) -> dict:
    match = INVITE_RE.search(link)
    if not match:
        raise Problem("That doesn't look like a Discord invite link.")
    try:
        invite = await bot.fetch_invite(match.group(1), with_counts=True, with_expiration=True)
    except discord.NotFound:
        raise Problem("That invite is invalid or has expired. Make a never-expiring one and try again.")
    except discord.HTTPException:
        raise Problem("Couldn't check that invite right now. Try again in a moment.")
    if invite.guild is None:
        raise Problem("That invite doesn't lead to a server.")
    if invite.expires_at is not None:
        raise Problem("Your invite link expires. Create a never-expiring link and try again.")
    try:
        claimed = int(re.sub(r"[,.\s]", "", members_raw))
    except ValueError:
        raise Problem("Member count must be a number.")
    real = invite.approximate_member_count or claimed
    if real < MIN_MEMBERS:
        raise Problem(f"URA only allies with servers that have {MIN_MEMBERS}+ members. Your invite shows {real}.")
    icon = invite.guild.icon
    return {
        "code": invite.code,
        "guild_id": invite.guild.id,
        "members": real,
        "online": invite.approximate_presence_count or 0,
        "icon_url": icon.replace(size=256, static_format="png").url if icon else None,
    }


# --------------------------------------------------------------------------- views
def build_hub() -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    c = discord.ui.Container(accent_colour=ACCENT)
    c.add_item(
        discord.ui.Section(
            text("# Partnership Request"),
            text(
                "Wanna shake hands with a community like URA? We're a raiding community that "
                "backs its allies when a fight turns unfair, and we're always open to clans "
                "that fight the same way."
            ),
            text(
                "-# If you want to ally with URA, make sure your server has **50+ members** "
                "and is a **TSB related** server."
            ),
            accessory=discord.ui.Thumbnail(HANDSHAKE),
        )
    )
    c.add_item(sep())
    c.add_item(
        text(
            "### Rules\n"
            "> • **50+ members** required\n"
            "> • **No fake alliances**\n"
            f"> • Not an ally of **{BLOCKED_ALLIES}**"
        )
    )
    c.add_item(sep())
    c.add_item(
        text(
            "## How can I obtain a Partnership?\n"
            "Press **Create Partnership**, fill in your clan details, and pick a bio style. "
            f"Confirm the preview and your alliance gets announced in <#{ALLIANCE_CHANNEL_ID}>."
        )
    )
    c.add_item(
        text(
            "### Requirements\n"
            "> • A customized bio (or use our default one)\n"
            "> • A **never-expiring** server link\n"
            "> • Your server's real member count\n"
            "> • A TSB related server"
        )
    )
    c.add_item(sep())

    guide = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Guide", emoji="📖", custom_id="ura:guide")
    create = discord.ui.Button(
        style=discord.ButtonStyle.secondary, label="Create Partnership", emoji="🤝", custom_id="ura:create"
    )
    edit = discord.ui.Button(
        style=discord.ButtonStyle.secondary, label="Edit My Partnership", emoji="🎨", custom_id="ura:edit"
    )
    guide.callback = on_guide
    create.callback = on_create
    edit.callback = on_edit
    c.add_item(discord.ui.ActionRow(guide, create, edit))
    view.add_item(c)
    return view


def simple_view(message: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(text(message), accent_colour=ACCENT))
    return view


def ally_view(rec: dict, *, timeout=None, top=(), bottom=()) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    for item in top:
        view.add_item(item)
    c = discord.ui.Container(accent_colour=ACCENT)
    c.add_item(
        discord.ui.Section(
            text(
                f"-# 🤝 URA's New Alliance!\n## {rec['name']}\n"
                f"URA now stands beside **{rec['name']}**. Backup flows both ways from today."
            ),
            text(
                f"**Owner:** <@{rec['owner_id']}>\n"
                f"**Server ID:** `{rec['guild_id']}`\n"
                f"**Members:** `{rec['members']:,} • {rec['online']:,} online`\n"
                f"**Alliance ID:** `{rec['code']}`\n"
                f"**Allied since:** <t:{rec['created_at']}:D>"
            ),
            accessory=discord.ui.Thumbnail(rec["icon_url"]),
        )
    )
    c.add_item(sep())
    c.add_item(text(f"## What is {rec['name']}?\n{rec['bio_what']}"))
    c.add_item(sep())
    c.add_item(text(f"## ❓ Why Should You Join?\n{rec['bio_why']}"))
    c.add_item(sep())
    join = discord.ui.Button(
        style=discord.ButtonStyle.link, label="Click Here To Join", emoji="🔗", url=rec["invite_url"]
    )
    c.add_item(discord.ui.Section(text("## Join now !"), accessory=join))
    view.add_item(c)
    for item in bottom:
        view.add_item(item)
    return view


# --------------------------------------------------------------------------- posting
async def get_channel(channel_id: int):
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)


async def publish(rec: dict, editing: bool) -> int:
    channel = await get_channel(ALLIANCE_CHANNEL_ID)
    view = ally_view(rec)
    no_pings = discord.AllowedMentions.none()
    if editing and rec.get("message_id"):
        try:
            await channel.get_partial_message(rec["message_id"]).edit(view=view, allowed_mentions=no_pings)
            return rec["message_id"]
        except discord.NotFound:
            pass
    msg = await channel.send(view=view, allowed_mentions=no_pings)
    return msg.id


async def ensure_panel():
    channel = await get_channel(REQUEST_CHANNEL_ID)
    stored = store.get_meta("panel_message_id")
    if stored:
        try:
            await channel.get_partial_message(int(stored)).edit(view=build_hub())
            return int(stored)
        except discord.NotFound:
            pass
    msg = await channel.send(view=build_hub())
    store.set_meta("panel_message_id", str(msg.id))
    return msg.id


# --------------------------------------------------------------------------- buttons
async def on_guide(interaction: discord.Interaction):
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.Container(
            text(
                "## 📖 Alliance guide\n"
                "**1.** Make a **never-expiring** invite to your server.\n"
                "**2.** Press **Create Partnership** and fill in your server name, link and member count.\n"
                "**3.** Pick **Default bio** and we write it for you, or **Customized bio** and write your own. "
                "For a custom bio, write what your clan is, leave a blank line, then write why people should join.\n"
                "**4.** Check the preview, confirm, and your alliance is announced.\n"
                "**5.** Need changes later? Press **Edit My Partnership**."
            ),
            accent_colour=ACCENT,
        )
    )
    await interaction.response.send_message(view=view, ephemeral=True)


async def on_create(interaction: discord.Interaction):
    if store.by_owner(interaction.user.id):
        return await interaction.response.send_message(
            "You already have an alliance with URA. Use **Edit My Partnership** to change it.", ephemeral=True
        )
    await interaction.response.send_modal(RequestModal())


async def on_edit(interaction: discord.Interaction):
    existing = store.by_owner(interaction.user.id)
    if not existing:
        return await interaction.response.send_message(
            "You don't have an alliance yet. Press **Create Partnership** first.", ephemeral=True
        )
    await interaction.response.send_modal(RequestModal(dict(existing)))


# --------------------------------------------------------------------------- modal
class RequestModal(discord.ui.Modal, title="Partnership Request"):
    server_name = discord.ui.TextInput(label="Server name", placeholder="Your clan or server name", max_length=60)
    invite = discord.ui.TextInput(
        label="Server link (never expiring)", placeholder="https://discord.gg/yourclan", max_length=100
    )
    members = discord.ui.TextInput(label="Member count", placeholder="e.g. 120", max_length=7)
    bio_type = discord.ui.Label(
        text="Bio type",
        component=discord.ui.Select(
            options=[
                discord.SelectOption(label="Default bio", value="default", description="We write it for you"),
                discord.SelectOption(label="Customized bio", value="custom", description="Write your own below"),
            ]
        ),
    )
    bio = discord.ui.TextInput(
        label="Customized bio (only for Customized)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=900,
        placeholder="What your clan is.\n\n(blank line)\n\nWhy people should join.",
    )

    def __init__(self, existing: dict | None = None):
        super().__init__()
        self.existing = existing
        if existing:
            self.server_name.default = existing["name"]
            self.invite.default = existing["invite_url"]
            self.members.default = str(existing["members"])
            for option in self.bio_type.component.options:
                option.default = option.value == existing["bio_type"]
            if existing["bio_type"] == "custom":
                self.bio.default = f"{existing['bio_what']}\n\n{existing['bio_why']}"[:900]

    async def on_submit(self, interaction: discord.Interaction):
        values = self.bio_type.component.values
        await handle_submit(
            interaction,
            self.existing,
            name=self.server_name.value,
            link=self.invite.value,
            members_raw=self.members.value,
            bio_type=values[0] if values else "default",
            bio_text=self.bio.value or "",
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"Modal error: {error!r}")
        if interaction.response.is_done():
            await interaction.followup.send("Something went wrong. Try again.", ephemeral=True)
        else:
            await interaction.response.send_message("Something went wrong. Try again.", ephemeral=True)


async def handle_submit(interaction, existing, *, name, link, members_raw, bio_type, bio_text):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        info = await verify(link, members_raw)
        other = store.by_guild(info["guild_id"])
        if other and other["owner_id"] != interaction.user.id:
            raise Problem("That server already has an alliance with URA.")
        clean_name = clean(name)
        if not clean_name:
            raise Problem("Enter your server name.")
        if bio_type == "custom":
            if not bio_text.strip():
                raise Problem("You picked Customized bio, so write your bio. Or switch to Default bio.")
            what, why = split_bio(bio_text)
        else:
            what, why = "", ""
        what = what or DEFAULT_WHAT.replace("{name}", clean_name)
        why = why or DEFAULT_WHY.replace("{name}", clean_name)
    except Problem as problem:
        return await interaction.followup.send(f"❌ {problem}", ephemeral=True)

    rec = {
        "owner_id": interaction.user.id,
        "code": existing["code"] if existing else new_code(),
        "guild_id": info["guild_id"],
        "name": clean_name,
        "invite_url": f"https://discord.gg/{info['code']}",
        "members": info["members"],
        "online": info["online"],
        "bio_type": bio_type,
        "bio_what": what,
        "bio_why": why,
        "icon_url": info["icon_url"] or HANDSHAKE,
        "channel_id": ALLIANCE_CHANNEL_ID,
        "message_id": existing["message_id"] if existing else None,
        "created_at": existing["created_at"] if existing else int(time.time()),
    }

    async def confirm(i: discord.Interaction):
        await i.response.defer()
        try:
            rec["message_id"] = await publish(rec, editing=bool(existing))
        except discord.HTTPException:
            return await i.followup.send("❌ Couldn't post in the alliances channel. Tell a staff member.", ephemeral=True)
        store.save(rec)
        link_url = f"https://discord.com/channels/{i.guild_id}/{ALLIANCE_CHANNEL_ID}/{rec['message_id']}"
        await i.edit_original_response(
            view=simple_view(f"✅ {'Updated' if existing else 'Your alliance is live'}! [View it in <#{ALLIANCE_CHANNEL_ID}>]({link_url})")
        )

    async def cancel(i: discord.Interaction):
        await i.response.edit_message(view=simple_view("Cancelled. Nothing was posted."))

    ok = discord.ui.Button(style=discord.ButtonStyle.success, label="Post alliance", emoji="✅")
    no = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Cancel")
    ok.callback, no.callback = confirm, cancel

    notice = discord.ui.Container(
        text(
            "### 👀 Preview\n"
            "This is how your alliance will be announced. By posting, you confirm this alliance is real "
            f"and that you are **not** an ally of {BLOCKED_ALLIES}."
        ),
        accent_colour=discord.Colour(0xE0B457),
    )
    view = ally_view(rec, timeout=600, top=[notice], bottom=[discord.ui.ActionRow(ok, no)])
    await interaction.followup.send(view=view, ephemeral=True)


# --------------------------------------------------------------------------- commands
@bot.tree.command(name="panel", description="Post or refresh the Partnership Request panel.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def panel_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await ensure_panel()
    except discord.HTTPException as e:
        return await interaction.followup.send(f"Couldn't post the panel: {e}", ephemeral=True)
    await interaction.followup.send(f"Panel is up in <#{REQUEST_CHANNEL_ID}>.", ephemeral=True)


@bot.tree.command(name="unally", description="Remove an alliance by server ID.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.describe(server_id="The allied server's ID (shown on the alliance post)")
async def unally_cmd(interaction: discord.Interaction, server_id: str):
    await interaction.response.defer(ephemeral=True)
    if not server_id.isdigit():
        return await interaction.followup.send("Server ID must be a number.", ephemeral=True)
    rec = store.by_guild(int(server_id))
    if not rec:
        return await interaction.followup.send("No alliance found for that server ID.", ephemeral=True)
    if rec["message_id"]:
        try:
            channel = await get_channel(rec["channel_id"] or ALLIANCE_CHANNEL_ID)
            await channel.get_partial_message(rec["message_id"]).delete()
        except discord.HTTPException:
            pass
    store.delete(rec["owner_id"])
    await interaction.followup.send(f"Removed the alliance with **{rec['name']}**.", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    bot.run(TOKEN)