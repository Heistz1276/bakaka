"""URA | Alliances - free alliance bot (discord.py 2.6+, Components V2, SQLite).

Setup in your server (owner / Manage Server):
  /request-channel #channel   -> where the Partnership Request panel lives
  /alliance-channel #channel  -> where finished partnerships are posted
  /unally [server]            -> cancel a partnership (staff can pick any ally)
"""
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
DB_PATH = os.getenv("DB_PATH", "alliances.sqlite3")
MIN_MEMBERS = 50
ACCENT = discord.Colour(0x000000)  # black border (0x1E1F22 gives a subtle visible edge on dark theme)
SHOW_SERVER_ICON = True  # set False to remove the allied server's icon from listings too

DEFAULT_BIOS = {
    "clan": (
        "{name} is a TSB clan built on teamwork, discipline and steady improvement. "
        "We train together, back each other up in fights, and push for the top as one squad.",
        "Join for active members, fair leadership and people who actually show up. "
        "New or experienced, you'll find a squad to grow with and allies who have your back.",
    ),
    "community": (
        "{name} is a friendly TSB community where players meet, hang out and find teammates. "
        "It's a relaxed place to talk strategy, share plays and join events.",
        "Come for the good company and stay for the events, helpful members and welcoming vibe. "
        "Everyone is welcome to learn, play and grow with us.",
    ),
}

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
                kind       TEXT NOT NULL DEFAULT 'clan',
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
        try:
            self.db.execute("ALTER TABLE alliances ADD COLUMN kind TEXT NOT NULL DEFAULT 'clan'")
        except sqlite3.OperationalError:
            pass
        self.db.commit()

    def by_owner(self, owner_id: int):
        return self.db.execute("SELECT * FROM alliances WHERE owner_id=?", (owner_id,)).fetchone()

    def by_guild(self, guild_id: int):
        return self.db.execute("SELECT * FROM alliances WHERE guild_id=?", (guild_id,)).fetchone()

    def by_message(self, message_id: int):
        return self.db.execute("SELECT * FROM alliances WHERE message_id=?", (message_id,)).fetchone()

    def all(self):
        return self.db.execute("SELECT guild_id, name FROM alliances ORDER BY name").fetchall()

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
        self.ready_once = False

    async def setup_hook(self):
        self.add_view(build_hub())
        await sync_if_changed()

    async def on_ready(self):
        print(f"Logged in as {self.user} ({self.user.id})")
        if not self.ready_once:
            self.ready_once = True
            try:
                await ensure_panel()
            except Exception as e:
                print(f"Could not refresh the panel: {e!r}")


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


async def sync_if_changed():
    """Only call Discord's sync endpoint when the command list actually changed."""
    signature = ",".join(sorted(c.qualified_name for c in bot.tree.get_commands()))
    # include option names so renamed options trigger a re-sync
    signature += "|" + ",".join(
        sorted(f"{c.qualified_name}:{p.name}" for c in bot.tree.get_commands() for p in c.parameters)
    )
    if store.get_meta("command_signature") != signature:
        await bot.tree.sync()
        store.set_meta("command_signature", signature)


def get_channel_id(key: str) -> int | None:
    value = store.get_meta(key)
    return int(value) if value else None


async def get_channel(channel_id: int):
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)


def can_post(channel: discord.abc.GuildChannel) -> bool:
    perms = channel.permissions_for(channel.guild.me)
    return perms.view_channel and perms.send_messages and perms.read_message_history


async def verify(link: str) -> dict:
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
    members = invite.approximate_member_count
    if not members:
        raise Problem("Couldn't read your server's member count from that invite. Try another link.")
    if members < MIN_MEMBERS:
        raise Problem(f"URA only allies with servers that have {MIN_MEMBERS}+ members. Your server shows {members}.")
    icon = invite.guild.icon
    return {
        "code": invite.code,
        "guild_id": invite.guild.id,
        "name": clean(invite.guild.name)[:80] or "Unnamed server",
        "members": members,
        "online": invite.approximate_presence_count or 0,
        "icon_url": icon.replace(size=256, static_format="png").url if (icon and SHOW_SERVER_ICON) else "",
    }


# --------------------------------------------------------------------------- views
def guide_button() -> discord.ui.Button:
    b = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Guide", emoji="📖", custom_id="ura:guide")
    b.callback = on_guide
    return b


def build_hub() -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    c = discord.ui.Container(accent_colour=ACCENT)
    c.add_item(discord.ui.Section(text("# Partnership Request"), accessory=guide_button()))
    c.add_item(
        text(
            "Wanna shake hands with a community like URA? We're a raiding community that "
            "backs its allies when a fight turns unfair, and we're always open to clans "
            "that fight the same way."
        )
    )
    c.add_item(
        text(
            "If you want to ally with URA, make sure your server has **50+ members** "
            "and is a **TSB related** server."
        )
    )
    c.add_item(sep())
    c.add_item(
        text(
            "## How can I obtain a Partnership?\n"
            "Press **Create Partnership**, paste your server link, and choose how your listing "
            "should read. Once you confirm the preview, your partnership goes live."
        )
    )
    c.add_item(
        text(
            "### Requirements\n"
            "> • A customized bio (or use our default one)\n"
            "> • A **never-expiring** server link\n"
            "> • A TSB related server"
        )
    )
    c.add_item(sep())
    create = discord.ui.Button(
        style=discord.ButtonStyle.secondary, label="Create Partnership", emoji="🤝", custom_id="ura:create"
    )
    edit = discord.ui.Button(
        style=discord.ButtonStyle.secondary, label="Edit My Partnership", emoji="🎨", custom_id="ura:edit"
    )
    create.callback = on_create
    edit.callback = on_edit
    c.add_item(discord.ui.ActionRow(create, edit))
    view.add_item(c)
    return view


def simple_view(message: str, *, accessory=None) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    c = discord.ui.Container(accent_colour=ACCENT)
    if accessory:
        c.add_item(discord.ui.Section(text(message), accessory=accessory))
    else:
        c.add_item(text(message))
    view.add_item(c)
    return view


def ally_view(rec: dict, *, timeout=None, top=(), bottom=()) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=timeout)
    for item in top:
        view.add_item(item)
    c = discord.ui.Container(accent_colour=ACCENT)
    head = text(
        f"-# 🤝 URA's New Alliance!\n## {rec['name']}\n"
        f"URA now stands beside **{rec['name']}**. Backup flows both ways from today."
    )
    details = text(
        f"**Owner:** <@{rec['owner_id']}>\n"
        f"**Type:** {rec['kind'].title()}\n"
        f"**Server ID:** `{rec['guild_id']}`\n"
        f"**Members:** `{rec['members']:,} • {rec['online']:,} online`\n"
        f"**Alliance ID:** `{rec['code']}`\n"
        f"**Allied since:** <t:{rec['created_at']}:D>"
    )
    if rec.get("icon_url"):
        c.add_item(discord.ui.Section(head, details, accessory=discord.ui.Thumbnail(rec["icon_url"])))
    else:
        c.add_item(head)
        c.add_item(details)
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
async def publish(rec: dict, editing: bool):
    """Post or update the partnership message. Returns the message (or partial message)."""
    no_pings = discord.AllowedMentions.none()
    view = ally_view(rec)
    if editing and rec.get("message_id") and rec.get("channel_id"):
        try:
            channel = await get_channel(rec["channel_id"])
            msg = channel.get_partial_message(rec["message_id"])
            await msg.edit(view=view, allowed_mentions=no_pings)
            return msg
        except (discord.NotFound, discord.Forbidden):
            pass
    channel_id = get_channel_id("alliance_channel_id")
    if not channel_id:
        raise Problem("The alliance channel isn't set up yet.")
    channel = await get_channel(channel_id)
    rec["channel_id"] = channel.id
    return await channel.send(view=view, allowed_mentions=no_pings)


async def post_panel(channel: discord.TextChannel):
    msg = await channel.send(view=build_hub())
    store.set_meta("request_channel_id", str(channel.id))
    store.set_meta("panel_channel_id", str(channel.id))
    store.set_meta("panel_message_id", str(msg.id))
    return msg


async def ensure_panel():
    channel_id = get_channel_id("request_channel_id")
    if not channel_id:
        return
    channel = await get_channel(channel_id)
    stored_msg = get_channel_id("panel_message_id")
    stored_ch = get_channel_id("panel_channel_id")
    if stored_msg and stored_ch == channel_id:
        try:
            # attachments=[] also clears any old banner file from a previous panel message
            await channel.get_partial_message(stored_msg).edit(view=build_hub(), attachments=[])
            return
        except discord.NotFound:
            pass
    await post_panel(channel)


# --------------------------------------------------------------------------- buttons
async def on_guide(interaction: discord.Interaction):
    await interaction.response.send_message(
        view=simple_view(
            "## 📖 Partnership guide\n"
            "**1.** Create a **never-expiring** invite for your server.\n"
            "**2.** Press **Create Partnership**, paste the link, and choose **Clan** or **Community**.\n"
            "**3.** Your server name and member count are read from the invite automatically.\n"
            "**4.** Pick **Default** and we write your bio, or **Customized** and you'll write your own next.\n"
            "**5.** Check the preview and post it. Your partnership goes live.\n"
            "**6.** Need changes? Press **Edit My Partnership**. To end it, use **/unally**."
        ),
        ephemeral=True,
    )


async def on_create(interaction: discord.Interaction):
    if not get_channel_id("alliance_channel_id"):
        return await interaction.response.send_message(
            "Partnerships aren't open yet. Staff still need to set the alliance channel.", ephemeral=True
        )
    if store.by_owner(interaction.user.id):
        return await interaction.response.send_message(
            "You already have a partnership with URA. Use **Edit My Partnership** to change it.", ephemeral=True
        )
    await interaction.response.send_modal(LinkModal())


async def on_edit(interaction: discord.Interaction):
    existing = store.by_owner(interaction.user.id)
    if not existing:
        return await interaction.response.send_message(
            "You don't have a partnership yet. Press **Create Partnership** first.", ephemeral=True
        )
    await interaction.response.send_modal(LinkModal(dict(existing)))


# --------------------------------------------------------------------------- modals
class LinkModal(discord.ui.Modal, title="Create Partnership"):
    invite = discord.ui.TextInput(
        label="Server link (never expiring)", placeholder="https://discord.gg/yourserver", max_length=100
    )
    kind = discord.ui.Label(
        text="Is your server a clan or a community?",
        component=discord.ui.Select(
            options=[
                discord.SelectOption(label="Clan", value="clan"),
                discord.SelectOption(label="Community", value="community"),
            ]
        ),
    )
    bio_style = discord.ui.Label(
        text="Bio style",
        component=discord.ui.Select(
            options=[
                discord.SelectOption(label="Default bio", value="default", description="We write it for you"),
                discord.SelectOption(label="Customized bio", value="custom", description="You'll write it next"),
            ]
        ),
    )

    def __init__(self, existing: dict | None = None):
        super().__init__()
        self.existing = existing
        if existing:
            self.title = "Edit Partnership"
            self.invite.default = existing["invite_url"]
            for o in self.kind.component.options:
                o.default = o.value == existing["kind"]
            for o in self.bio_style.component.options:
                o.default = o.value == existing["bio_type"]

    async def on_submit(self, interaction: discord.Interaction):
        kind = (self.kind.component.values or ["clan"])[0]
        style = (self.bio_style.component.values or ["default"])[0]
        await handle_link(interaction, self.existing, self.invite.value, kind, style)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"Modal error: {error!r}")
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Something went wrong. Try again.", ephemeral=True)


class BioModal(discord.ui.Modal, title="Customized bio"):
    what = discord.ui.TextInput(
        label="What is your server?",
        style=discord.TextStyle.paragraph,
        max_length=450,
        placeholder="Tell people who you are and what you do.",
    )
    why = discord.ui.TextInput(
        label="Why should people join?",
        style=discord.TextStyle.paragraph,
        max_length=450,
        placeholder="Give them a reason to join.",
    )

    def __init__(self, on_done, what: str = "", why: str = ""):
        super().__init__()
        self.on_done = on_done
        self.what.default = what or None
        self.why.default = why or None

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.on_done(
            interaction,
            discord.utils.escape_mentions(self.what.value.strip()),
            discord.utils.escape_mentions(self.why.value.strip()),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"Bio modal error: {error!r}")
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Something went wrong. Try again.", ephemeral=True)


# --------------------------------------------------------------------------- flow
async def handle_link(interaction, existing, link, kind, style):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        info = await verify(link)
        other = store.by_guild(info["guild_id"])
        if other and other["owner_id"] != interaction.user.id:
            raise Problem("That server already has a partnership with URA.")
    except Problem as problem:
        return await interaction.followup.send(f"❌ {problem}", ephemeral=True)

    if style == "default":
        what, why = (t.replace("{name}", info["name"]) for t in DEFAULT_BIOS[kind])
        return await show_preview(interaction, existing, info, kind, style, what, why)

    old_what = existing["bio_what"] if existing and existing["bio_type"] == "custom" else ""
    old_why = existing["bio_why"] if existing and existing["bio_type"] == "custom" else ""

    async def bio_done(i, what, why):
        await show_preview(i, existing, info, kind, style, what, why)

    async def open_bio(i: discord.Interaction):
        await i.response.send_modal(BioModal(bio_done, old_what, old_why))

    write = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Write my bio", emoji="✍️")
    write.callback = open_bio
    view = discord.ui.LayoutView(timeout=600)
    view.add_item(
        discord.ui.Container(
            text(f"### Link checked: **{info['name']}**\nNow write your customized bio."),
            discord.ui.ActionRow(write),
            accent_colour=ACCENT,
        )
    )
    await interaction.followup.send(view=view, ephemeral=True)


async def show_preview(interaction, existing, info, kind, style, what, why):
    rec = {
        "owner_id": interaction.user.id,
        "code": existing["code"] if existing else new_code(),
        "guild_id": info["guild_id"],
        "name": info["name"],
        "kind": kind,
        "invite_url": f"https://discord.gg/{info['code']}",
        "members": info["members"],
        "online": info["online"],
        "bio_type": style,
        "bio_what": what,
        "bio_why": why,
        "icon_url": info["icon_url"],
        "channel_id": existing["channel_id"] if existing else None,
        "message_id": existing["message_id"] if existing else None,
        "created_at": existing["created_at"] if existing else int(time.time()),
    }

    async def confirm(i: discord.Interaction):
        await i.response.defer()
        try:
            msg = await publish(rec, editing=bool(existing))
        except Problem as problem:
            return await i.followup.send(f"❌ {problem}", ephemeral=True)
        except discord.HTTPException:
            return await i.followup.send(
                "❌ Couldn't post the partnership. Tell a staff member to check the alliance channel.", ephemeral=True
            )
        rec["message_id"] = msg.id
        store.save(rec)
        open_btn = discord.ui.Button(style=discord.ButtonStyle.link, label="View my partnership", url=msg.jump_url)
        await i.edit_original_response(
            view=simple_view(
                "✅ Partnership updated." if existing else "✅ Your partnership is live.", accessory=open_btn
            )
        )

    async def cancel(i: discord.Interaction):
        await i.response.edit_message(view=simple_view("Cancelled. Nothing was posted."))

    ok = discord.ui.Button(style=discord.ButtonStyle.success, label="Post partnership", emoji="✅")
    no = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Cancel")
    ok.callback, no.callback = confirm, cancel
    notice = discord.ui.Container(
        text("### 👀 Preview\nThis is how your partnership will be announced."),
        accent_colour=ACCENT,
    )
    view = ally_view(rec, timeout=600, top=[notice], bottom=[discord.ui.ActionRow(ok, no)])
    await interaction.followup.send(view=view, ephemeral=True)


# --------------------------------------------------------------------------- commands
def is_staff(interaction: discord.Interaction) -> bool:
    return interaction.user.guild_permissions.manage_guild


@bot.tree.command(name="request-channel", description="Set the channel for the Partnership Request panel.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.describe(channel="Where members press Create Partnership")
async def request_channel_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not (is_staff(interaction) or await bot.is_owner(interaction.user)):
        return await interaction.response.send_message("You need Manage Server for this.", ephemeral=True)
    if not can_post(channel):
        return await interaction.response.send_message(
            f"I can't post in {channel.mention}. Give me View Channel, Send Messages and Read Message History there.",
            ephemeral=True,
        )
    await interaction.response.defer(ephemeral=True)
    old_ch, old_msg = get_channel_id("panel_channel_id"), get_channel_id("panel_message_id")
    if old_ch and old_msg:
        try:
            await (await get_channel(old_ch)).get_partial_message(old_msg).delete()
        except discord.HTTPException:
            pass
    try:
        msg = await post_panel(channel)
    except discord.Forbidden:
        return await interaction.followup.send(
            f"❌ Discord blocked me from posting in {channel.mention}. Check my channel permissions "
            "(View Channel, Send Messages, Read Message History) and that no channel override denies them.",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        print(f"Panel post failed: {e!r}")
        return await interaction.followup.send(f"❌ Couldn't post the panel: `{e.text or e}`", ephemeral=True)
    except Exception as e:
        print(f"Panel post failed: {e!r}")
        return await interaction.followup.send(f"❌ Unexpected error: `{e!r}`", ephemeral=True)
    await interaction.followup.send(f"Panel posted in {channel.mention}. [Open it]({msg.jump_url})", ephemeral=True)


@bot.tree.command(name="alliance-channel", description="Set the channel where partnerships are posted.")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
@app_commands.describe(channel="Where finished partnerships are announced")
async def alliance_channel_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not (is_staff(interaction) or await bot.is_owner(interaction.user)):
        return await interaction.response.send_message("You need Manage Server for this.", ephemeral=True)
    if not can_post(channel):
        return await interaction.response.send_message(
            f"I can't post in {channel.mention}. Give me View Channel, Send Messages and Read Message History there.",
            ephemeral=True,
        )
    store.set_meta("alliance_channel_id", str(channel.id))
    await interaction.response.send_message(f"New partnerships will be posted in {channel.mention}.", ephemeral=True)


async def allied_autocomplete(interaction: discord.Interaction, current: str):
    if not (is_staff(interaction) or await bot.is_owner(interaction.user)):
        return []
    cur = current.lower()
    return [
        app_commands.Choice(name=r["name"][:100], value=str(r["guild_id"]))
        for r in store.all()
        if cur in r["name"].lower() or cur in str(r["guild_id"])
    ][:25]


@bot.tree.command(name="unally", description="Cancel a partnership.")
@app_commands.guild_only()
@app_commands.describe(server="Allied server to un-ally (staff only). Leave empty to cancel your own partnership.")
@app_commands.autocomplete(server=allied_autocomplete)
async def unally_cmd(interaction: discord.Interaction, server: str | None = None):
    can_manage = is_staff(interaction) or await bot.is_owner(interaction.user)
    if server:
        if not can_manage:
            return await interaction.response.send_message(
                "You need Manage Server to cancel another server's partnership.", ephemeral=True
            )
        if not server.isdigit():
            return await interaction.response.send_message(
                "Pick a server from the list, or paste its server ID.", ephemeral=True
            )
        rec = store.by_guild(int(server))
    else:
        rec = store.by_owner(interaction.user.id)
    if not rec:
        return await interaction.response.send_message("No partnership found.", ephemeral=True)

    async def yes(i: discord.Interaction):
        await i.response.defer()
        try:
            channel = await get_channel(rec["channel_id"])
            await channel.get_partial_message(rec["message_id"]).delete()
        except (discord.HTTPException, TypeError):
            pass
        store.delete(rec["owner_id"])
        await i.edit_original_response(view=simple_view(f"Partnership with **{rec['name']}** cancelled."))

    async def no(i: discord.Interaction):
        await i.response.edit_message(view=simple_view("Kept. Nothing changed."))

    y = discord.ui.Button(style=discord.ButtonStyle.danger, label="Yes, cancel it")
    n = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Keep it")
    y.callback, n.callback = yes, no
    view = discord.ui.LayoutView(timeout=120)
    view.add_item(
        discord.ui.Container(
            text(f"### Cancel this partnership?\nThis removes the partnership with **{rec['name']}**."),
            discord.ui.ActionRow(y, n),
            accent_colour=ACCENT,
        )
    )
    await interaction.response.send_message(view=view, ephemeral=True)


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Command error: {error!r}")
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    try:
        await send("Something went wrong running that command.", ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    try:
        bot.run(TOKEN)
    except discord.HTTPException as e:
        if e.status == 429 or "1015" in str(e):
            print("Rate limited by Discord (Cloudflare 1015). Sleeping 30 minutes before exiting.")
            time.sleep(1800)
        raise