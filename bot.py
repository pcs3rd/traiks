"""
Tracker Watcher Discord Bot

Monitors r/trackers and r/opensignups for open invite/signup posts.
For each match: posts an embed, creates a thread with full details,
adds a 🟢/🔴 reaction based on open/closed status, and flips
reactions automatically when detected end dates pass.

Environment variables:
    DISCORD_TOKEN       Bot token (required)
    DISCORD_CHANNEL_ID  Channel ID to post notifications to (required)
    CHECK_INTERVAL      Seconds between checks (default: 900)
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands, tasks

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 900))
STATE_FILE = Path("/data/seen_posts.json")
THREADS_FILE = Path("/data/threads.json")
REDDIT_USER_AGENT = "Mozilla/5.0 (compatible; traiks-bot/1.0; +https://github.com/pcs3rd/traiks)"


SUBREDDITS = ["trackers", "opensignups"]
KEYWORDS = [
    "open signup", "open signups", "open registration", "now open",
    "invites", "invite", "recruiting", "recruitment", "free invite",
    "open applications", "applications open",
]
CLOSED_KEYWORDS = ["closed", "full", "ended", "over", "no longer", "not accepting"]

OPEN_EMOJI  = "🟢"
CLOSED_EMOJI = "🔴"

# Patterns to extract end dates from post text.
# Tries to find phrases like "closes June 5", "until 2026-06-10", "deadline: June 5th", etc.
DATE_PATTERNS = [
    r"(?:clos(?:es?|ing)|ends?|until|deadline[:\s]+|open\s+until|expire[sd]?)\s+(?:on\s+)?([A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)",
    r"(?:clos(?:es?|ing)|ends?|until|deadline[:\s]+|open\s+until|expire[sd]?)\s+(?:on\s+)?(\d{4}-\d{2}-\d{2})",
    r"(?:clos(?:es?|ing)|ends?|until|deadline[:\s]+|open\s+until|expire[sd]?)\s+(?:on\s+)?(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)",
]


def load_seen() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(list(seen)))


def load_threads() -> dict:
    """Maps reddit post_id -> {message_id, thread_id, end_date, closed}"""
    if THREADS_FILE.exists():
        return json.loads(THREADS_FILE.read_text())
    return {}


def save_threads(threads: dict):
    THREADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    THREADS_FILE.write_text(json.dumps(threads))


def is_relevant(title: str) -> bool:
    return any(kw in title.lower() for kw in KEYWORDS)


def is_closed(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in CLOSED_KEYWORDS)


def extract_end_date(text: str) -> datetime | None:
    """Try to parse an end/closing date from post text."""
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw = match.group(1).strip().rstrip(".")
            # Try a few parse strategies
            for fmt in ("%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y",
                        "%B %dst %Y", "%B %dnd %Y", "%B %drd %Y", "%B %dth %Y",
                        "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
                try:
                    # strip ordinal suffixes
                    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
                    # if no year, append current year
                    if not re.search(r"\d{4}", cleaned):
                        cleaned = f"{cleaned} {datetime.now().year}"
                    return datetime.strptime(cleaned.strip(), fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return None


def format_end_date(dt: datetime | None) -> str:
    if dt is None:
        return "Not specified"
    delta = dt - datetime.now(timezone.utc)
    if delta.total_seconds() < 0:
        return f"~~{dt.strftime('%b %d, %Y')}~~ (expired)"
    days = delta.days
    if days == 0:
        return f"{dt.strftime('%b %d, %Y')} (today!)"
    return f"{dt.strftime('%b %d, %Y')} ({days}d remaining)"


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

last_check: float = 0
last_found: int = 0


async def fetch_posts(session: aiohttp.ClientSession, subreddit: str) -> list:
    url = f"https://arctic-shift.photon-reddit.com/api/posts/search?subreddit={subreddit}&limit=25&sort=desc"
    try:
        headers = {"User-Agent": REDDIT_USER_AGENT}
        async with session.get(url, headers=headers) as r:
            data = await r.json()
        return [{"data": p} for p in data.get("data", [])]
    except Exception as e:
        print(f"[error] fetching r/{subreddit}: {e}")
        return []


async def post_notification(channel: discord.TextChannel, data: dict):
    """Post embed + thread + reaction for a matched post."""
    title = data["title"]
    body = data.get("selftext") or ""
    url = f"https://reddit.com{data['permalink']}"
    combined = f"{title}\n{body}"

    closed = is_closed(combined)
    end_date = extract_end_date(combined)
    if end_date and end_date < datetime.now(timezone.utc):
        closed = True

    color = 0x57f287 if not closed else 0xed4245  # green / red

    embed = discord.Embed(title=title, url=url, color=color)
    embed.add_field(name="Status", value="🔴 Closed" if closed else "🟢 Open", inline=True)
    embed.add_field(name="Closes", value=format_end_date(end_date), inline=True)
    embed.set_footer(text=f"r/{data['subreddit']} • u/{data['author']}")

    msg = await channel.send(embed=embed)
    await msg.add_reaction(CLOSED_EMOJI if closed else OPEN_EMOJI)

    # Create a thread for full details and discussion
    thread = await msg.create_thread(
        name=title[:100],
        auto_archive_duration=10080,  # 7 days
    )

    thread_body = body[:3800] if body else "*No body text.*"
    detail_embed = discord.Embed(title="Full post", url=url, color=color, description=thread_body)
    detail_embed.add_field(name="Closes", value=format_end_date(end_date), inline=True)
    detail_embed.add_field(name="Subreddit", value=f"r/{data['subreddit']}", inline=True)
    detail_embed.add_field(name="Posted by", value=f"u/{data['author']}", inline=True)
    await thread.send(embed=detail_embed)

    # Persist so we can flip reactions when status changes
    threads = load_threads()
    threads[data["id"]] = {
        "message_id": msg.id,
        "thread_id": thread.id,
        "channel_id": channel.id,
        "end_date": end_date.isoformat() if end_date else None,
        "closed": closed,
    }
    save_threads(threads)


async def run_check(notify: bool = True) -> tuple[int, int]:
    global last_check, last_found

    seen = load_seen()
    new_seen = set()
    found = 0
    checked = 0

    channel = bot.get_channel(DISCORD_CHANNEL_ID)

    async with aiohttp.ClientSession() as session:
        for subreddit in SUBREDDITS:
            posts = await fetch_posts(session, subreddit)
            for post in posts:
                data = post["data"]
                post_id = data["id"]
                new_seen.add(post_id)
                checked += 1

                if post_id in seen:
                    continue

                if is_relevant(data["title"]):
                    found += 1
                    if notify and channel:
                        await post_notification(channel, data)
                        await asyncio.sleep(1)

    save_seen(seen | new_seen)
    last_check = time.time()
    last_found = found
    return checked, found


async def update_expired_reactions():
    """Flip 🟢 → 🔴 on any tracked posts whose end date has passed."""
    threads = load_threads()
    changed = False

    for post_id, info in threads.items():
        if info["closed"]:
            continue
        if not info["end_date"]:
            continue

        end_dt = datetime.fromisoformat(info["end_date"])
        if end_dt > datetime.now(timezone.utc):
            continue

        # End date passed — flip reaction
        try:
            channel = bot.get_channel(info["channel_id"])
            msg = await channel.fetch_message(info["message_id"])
            await msg.clear_reaction(OPEN_EMOJI)
            await msg.add_reaction(CLOSED_EMOJI)

            # Update embed color
            embed = msg.embeds[0]
            embed.color = discord.Color(0xed4245)
            for i, field in enumerate(embed.fields):
                if field.name == "Status":
                    embed.set_field_at(i, name="Status", value="🔴 Closed (expired)", inline=True)
            await msg.edit(embed=embed)

            info["closed"] = True
            changed = True
            print(f"[expired] flipped post {post_id} to closed")
        except Exception as e:
            print(f"[error] updating expired post {post_id}: {e}")

    if changed:
        save_threads(threads)


@tasks.loop(seconds=CHECK_INTERVAL)
async def scheduled_check():
    checked, found = await run_check(notify=True)
    await update_expired_reactions()
    print(f"[check] {checked} posts scanned, {found} matches")


@bot.event
async def on_ready():
    print(f"[ready] logged in as {bot.user}")
    scheduled_check.start()


@bot.command(name="check")
async def cmd_check(ctx):
    """Manually trigger a check right now."""
    msg = await ctx.send("Checking subreddits...")
    checked, found = await run_check(notify=True)
    await update_expired_reactions()
    await msg.edit(content=f"Done — scanned {checked} posts, found {found} new matches.")


@bot.command(name="status")
async def cmd_status(ctx):
    """Show bot status."""
    ago = f"{int(time.time() - last_check)}s ago" if last_check else "never"
    threads = load_threads()
    open_count = sum(1 for t in threads.values() if not t["closed"])
    embed = discord.Embed(title="Tracker Watcher", color=0x00b0f4)
    embed.add_field(name="Last check", value=ago, inline=True)
    embed.add_field(name="Interval", value=f"{CHECK_INTERVAL}s", inline=True)
    embed.add_field(name="Posts seen", value=str(len(load_seen())), inline=True)
    embed.add_field(name="Open trackers", value=str(open_count), inline=True)
    embed.add_field(name="Subreddits", value=" ".join(f"r/{s}" for s in SUBREDDITS), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="open")
async def cmd_open(ctx):
    """List currently open trackers."""
    threads = load_threads()
    open_items = [(pid, t) for pid, t in threads.items() if not t["closed"]]
    if not open_items:
        await ctx.send("No open trackers tracked right now.")
        return
    lines = []
    for _, t in open_items[:20]:
        ch = bot.get_channel(t["channel_id"])
        try:
            msg = await ch.fetch_message(t["message_id"])
            title = msg.embeds[0].title if msg.embeds else "Unknown"
            closes = format_end_date(
                datetime.fromisoformat(t["end_date"]) if t["end_date"] else None
            )
            lines.append(f"🟢 [{title}]({msg.jump_url}) — closes {closes}")
        except Exception:
            continue
    embed = discord.Embed(title="Open trackers", description="\n".join(lines), color=0x57f287)
    await ctx.send(embed=embed)


@bot.command(name="keywords")
async def cmd_keywords(ctx):
    """List watched keywords."""
    await ctx.send("Watching for: " + ", ".join(f"`{k}`" for k in KEYWORDS))


@bot.command(name="interval")
async def cmd_interval(ctx, seconds: int):
    """Change check interval. Usage: !interval 600"""
    global CHECK_INTERVAL
    if seconds < 60:
        await ctx.send("Minimum interval is 60 seconds.")
        return
    CHECK_INTERVAL = seconds
    scheduled_check.change_interval(seconds=seconds)
    await ctx.send(f"Check interval updated to {seconds}s.")


bot.run(DISCORD_TOKEN)
