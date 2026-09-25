import discord
from discord.ext import commands, tasks
from datetime import timezone, timedelta
import asyncio
import logging
import os
import json

TOKEN = os.environ["DISCORD_BOT_TOKEN"] 
OUTPUT_CHANNEL_ID = 1459041848273273024

OLD_TRACKED_MESSAGE_ID = 1467648354535342145


CODE_REVIEWER_ROLE = 532970804456062988
SET_DESIGNER_ROLE = 1499469606094377161

IGNORED_THREAD_ID = 1233558070781804585
NO_REPLY_TAG_ID = 1233559692513382502

FORUMS = [
    {"forum_id": 1233558070781804585, "tag_id": 1233559692513382502, "resolved_tag_ids": [1429745050287345794, 1429745106382094387], "name": "Jr Dev Application", "icon": "🎓️"},
    {"forum_id": 1538855563708997712, "tag_id": 1538856693595774996, "resolved_tag_ids": [1538856741813620806, 1538856775099355146], "name": "Set Plan Review(SDT)", "icon": "🖍️"},
    {"forum_id": 1038471602901368952, "tag_id": 1311106891526701100, "resolved_tag_ids": [1038479950413574274, 1063506394545913986], "name": "Set Plan Review", "icon": "🔎"},
    {"forum_id": 1038471602901368952, "tag_id": 1038479228351545495, "resolved_tag_ids": [1038479950413574274, 1063506394545913986], "name": "Ready for Review", "icon": "🗒️"},
]

STATE_FILE = "thread_tracker_state.json"

def load_message_ids():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.error(f"Could not read {STATE_FILE}, starting fresh: {e}")
    return {}

def save_message_ids(mapping):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(mapping, f)
    except OSError as e:
        log.error(f"Could not save {STATE_FILE}: {e}")

MAX_MESSAGE_LENGTH = 1990

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("thread-tracker")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

_refresh_pending = False
_refresh_lock = asyncio.Lock()

async def request_refresh():
    """Coalesce bursts of events (many messages in a short time) into a single refresh."""
    global _refresh_pending
    if _refresh_pending:
        return
    _refresh_pending = True
    async with _refresh_lock:
        await asyncio.sleep(15)
        _refresh_pending = False
        await build_and_post_list()

async def build_forum_section_text(cfg, resolved_users_role):
    forum = bot.get_channel(cfg["forum_id"])
    if not forum:
        return None

    now = discord.utils.utcnow()
    limit_72h = timedelta(hours=72)

    matched_data = []
    for t in forum.threads:
        if t.id == IGNORED_THREAD_ID:
            continue

        tag_ids = {tag.id for tag in t.applied_tags}
        if cfg["tag_id"] not in tag_ids:
            continue
        if any(r_id in tag_ids for r_id in cfg["resolved_tag_ids"]):
            continue

        staff_has_replied = False
        last_msg_staff_role = None
        last_msg_time = None
        is_first_msg = True

        async for msg in t.history(limit=50):
            if msg.author.bot:
                continue

            author_id = msg.author.id

            if author_id in resolved_users_role:
                user_role = resolved_users_role[author_id]
            else:
                member = t.guild.get_member(author_id)
                if not member:
                    try:
                        member = await t.guild.fetch_member(author_id)
                        await asyncio.sleep(0.05)
                    except discord.HTTPException:
                        pass

                if member:
                    role_ids = {r.id for r in member.roles}
                    if CODE_REVIEWER_ROLE in role_ids:
                        user_role = "CR"
                    elif SET_DESIGNER_ROLE in role_ids:
                        user_role = "SDT"
                    else:
                        user_role = "NONE"
                else:
                    user_role = "NONE"

                resolved_users_role[author_id] = user_role

            is_staff = user_role in ("CR", "SDT")

            if is_first_msg:
                last_msg_staff_role = user_role if is_staff else None
                last_msg_time = msg.created_at
                is_first_msg = False

            if is_staff:
                staff_has_replied = True
                if not is_first_msg:
                    break

        priority = 0 if not staff_has_replied else 1
        matched_data.append({
            "thread": t,
            "priority": priority,
            "is_new": not staff_has_replied,
            "last_msg_time": last_msg_time,
            "last_msg_staff_role": last_msg_staff_role
        })

    if not matched_data:
        return f"**{cfg['icon']} {cfg['name']}**\n_No active threads._"

    matched_data.sort(key=lambda x: (x["priority"], x["thread"].created_at))

    lines = [f"**{cfg['icon']} {cfg['name']}**"]
    for item in matched_data:
        t = item["thread"]
        ts = int(t.created_at.timestamp())
        marker = "🆕 " if item["is_new"] else ""

        extra = ""
        if not item["is_new"] and item["last_msg_time"] and (now - item["last_msg_time"]) < limit_72h:
            delta = now - item["last_msg_time"]
            mins = int(delta.total_seconds() // 60)
            ago = f"{mins // 60}h" if mins >= 60 else f"{mins}m"

            role_label = item["last_msg_staff_role"]
            if role_label:
                extra = f" | `Awaiting Jr response, last {role_label} message ({ago})`"
            else:
                extra = f" | `Awaiting response, last Jr message ({ago})`"

        lines.append(f"{marker}- <t:{ts}:f> - {t.mention}{extra}")

    text = "\n".join(lines)

    if len(text) > MAX_MESSAGE_LENGTH:
        header_line = lines[0]
        body_lines = lines[1:]
        kept = []
        current_len = len(header_line)
        for line in body_lines:
            if current_len + len(line) + 1 > MAX_MESSAGE_LENGTH - 40:
                break
            kept.append(line)
            current_len += len(line) + 1

        cut_count = len(body_lines) - len(kept)
        text = "\n".join([header_line] + kept)
        text += f"\n_...and {cut_count} more thread(s) not shown (list too long)._"
        log.warning(f"Truncated section '{cfg['name']}': {cut_count} thread(s) omitted to stay under the message limit.")

    return text

async def build_and_post_list():
    log.info("Refreshing thread list...")
    output = bot.get_channel(OUTPUT_CHANNEL_ID)
    if not output:
        return

    resolved_users_role = {}

    for cfg in FORUMS:
        try:
            section_text = await build_forum_section_text(cfg, resolved_users_role)
            if section_text is None:
                continue
            msg = await output.fetch_message(cfg["message_id"])
            await msg.edit(content=section_text)
        except discord.HTTPException as e:
            log.error(f"Failed to update section '{cfg['name']}': {e}")
        except Exception as e:
            log.error(f"Unexpected error updating section '{cfg['name']}': {e}")

@tasks.loop(minutes=10)
async def auto_refresh():
    await build_and_post_list()

@tasks.loop(hours=144)
async def keep_channel_alive():
    log.info("Performing 6-day channel keep-alive...")
    output = bot.get_channel(OUTPUT_CHANNEL_ID)
    if output:
        try:
            temp_msg = await output.send("Refreshing Thread...")
            await asyncio.sleep(5)
            await temp_msg.delete()
        except Exception as e:
            log.error(f"Failed to send keep-alive message: {e}")

@bot.event
async def on_ready():
    log.info(f"Bot online as {bot.user}")

    output = bot.get_channel(OUTPUT_CHANNEL_ID)
    if output and OLD_TRACKED_MESSAGE_ID:
        try:
            old_msg = await output.fetch_message(OLD_TRACKED_MESSAGE_ID)
            await old_msg.delete()
            log.info(f"Deleted old tracked message {OLD_TRACKED_MESSAGE_ID}.")
        except discord.NotFound:
            pass  
        except discord.HTTPException as e:
            log.error(f"Could not delete old tracked message: {e}")

    if output:
        saved_ids = load_message_ids()
        changed = False
        for cfg in FORUMS:
            saved_id = saved_ids.get(cfg["name"])
            valid = False
            if saved_id:
                try:
                    await output.fetch_message(saved_id)
                    cfg["message_id"] = saved_id
                    valid = True
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    log.error(f"Could not verify saved message for '{cfg['name']}': {e}")

            if not valid:
                sent = await output.send(f"⏳ Setting up: {cfg['name']}...")
                cfg["message_id"] = sent.id
                saved_ids[cfg["name"]] = sent.id
                changed = True
                log.info(f"Created new message for '{cfg['name']}' -> {sent.id}")

        if changed:
            save_message_ids(saved_ids)

    if not auto_refresh.is_running():
        auto_refresh.start()
    if not keep_channel_alive.is_running():
        keep_channel_alive.start()
    await build_and_post_list()

@bot.event
async def on_thread_create(thread):
    await asyncio.sleep(1.5)

    now = discord.utils.utcnow()
    if (now - thread.created_at) > timedelta(minutes=2):
        return

    thread_tag_ids = {tag.id for tag in thread.applied_tags}

    is_tracked = False
    for cfg in FORUMS:
        if thread.parent_id == cfg["forum_id"] and cfg["tag_id"] in thread_tag_ids:
            is_tracked = True
            break

    if is_tracked and NO_REPLY_TAG_ID not in thread_tag_ids:
        try:
            already_sent = False
            async for msg in thread.history(limit=10):
                if msg.author == bot.user and "Added to the tracking list." in msg.content:
                    already_sent = True
                    break

            if not already_sent:
                await thread.send("Added to the tracking list.")
        except Exception as e:
            log.error(f"Failed to send message in thread {thread.id}: {e}")

    if is_tracked:
        await request_refresh()

@bot.event
async def on_thread_update(before, after):
    if before.applied_tags != after.applied_tags or before.archived != after.archived:
        await request_refresh()

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.Thread):
        if any(c["forum_id"] == message.channel.parent_id for c in FORUMS):
            await request_refresh()

bot.run(TOKEN)
