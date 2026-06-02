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


DEFAULT_SUBREDDITS = ["trackers", "opensignups"]
SUBREDDITS_FILE = Path("/data/subreddits.json")
RSS_FILE = Path("/data/rss_feeds.json")


def load_subreddits() -> list[str]:
    if SUBREDDITS_FILE.exists():
        return json.loads(SUBREDDITS_FILE.read_text())
    return list(DEFAULT_SUBREDDITS)


def save_subreddits(subs: list[str]):
    SUBREDDITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SUBREDDITS_FILE.write_text(json.dumps(subs))


def load_rss_feeds() -> list[str]:
    if RSS_FILE.exists():
        return json.loads(RSS_FILE.read_text())
    return []


def save_rss_feeds(feeds: list[str]):
    RSS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RSS_FILE.write_text(json.dumps(feeds))
KEYWORDS = [
    "open signup", "open signups", "open registration", "now open",
    "invites", "invite", "recruiting", "recruitment", "free invite",
    "open applications", "applications open",
]
CLOSED_KEYWORDS = ["closed", "full", "ended", "over", "no longer", "not accepting"]

OPEN_EMOJI  = "🟢"
CLOSED_EMOJI = "🔴"
SUBS_FILE = Path("/data/subscriptions.json")

# Category keywords — maps category name -> terms to match in post title/body
CATEGORIES: dict[str, list[str]] = {
    "movies":  ["movie", "film", "cinema", "blu-ray", "bluray", "hdr", "remux"],
    "tv":      ["tv", "television", "series", "shows", "episodes"],
    "music":   ["music", "audio", "flac", "lossless", "discography", "albums"],
    "books":   ["books", "ebooks", "epub", "comics", "manga"],
    "games":   ["games", "gaming", "console", "pc games"],
    "general": ["general", "ratio-free", "freeleech"],
}

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


def load_subs() -> dict:
    """Returns {user_id_str: [category, ...]}"""
    if SUBS_FILE.exists():
        return json.loads(SUBS_FILE.read_text())
    return {}


def save_subs(subs: dict):
    SUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SUBS_FILE.write_text(json.dumps(subs))


def match_categories(text: str) -> list[str]:
    """Return which categories the post matches."""
    lower = text.lower()
    return [cat for cat, terms in CATEGORIES.items() if any(t in lower for t in terms)]


def extract_links(text: str) -> list[str]:
    """Extract all URLs from post body."""
    return re.findall(r'https?://[^\s\)\]\"]+', text)


def extract_open_date(text: str) -> datetime | None:
    """Try to parse an open/start date from post text."""
    patterns = [
        r"(?:open(?:s|ing)?|start(?:s|ing)?|begin(?:s|ning)?|launch(?:es|ing)?)\s+(?:on\s+)?([A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)",
        r"(?:open(?:s|ing)?|start(?:s|ing)?)\s+(?:on\s+)?(\d{4}-\d{2}-\d{2})",
        r"(?:open(?:s|ing)?|start(?:s|ing)?)\s+(?:on\s+)?(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw = match.group(1).strip().rstrip(".")
            for fmt in ("%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y",
                        "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                try:
                    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw)
                    if not re.search(r"\d{4}", cleaned):
                        cleaned = f"{cleaned} {datetime.now().year}"
                    return datetime.strptime(cleaned.strip(), fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return None


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


async def fetch_rss(session: aiohttp.ClientSession, feed_url: str) -> list:
    """Fetch an arbitrary RSS/Atom feed and normalise entries to the same dict shape."""
    try:
        from xml.etree import ElementTree as ET
        headers = {"User-Agent": REDDIT_USER_AGENT}
        async with session.get(feed_url, headers=headers) as r:
            text = await r.text()
        root = ET.fromstring(text)
        entries = []
        # Atom
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns) or root.findall("entry"):
            title = entry.findtext("atom:title", "", ns) or entry.findtext("title", "")
            link_el = entry.find("atom:link", ns) or entry.find("link")
            link = (link_el.get("href") or link_el.text or "") if link_el is not None else ""
            body = entry.findtext("atom:content", "", ns) or entry.findtext("atom:summary", "", ns) or entry.findtext("description", "")
            entry_id = entry.findtext("atom:id", "", ns) or entry.findtext("id", link)
            entries.append({"data": {
                "id": f"rss_{hash(entry_id) & 0xffffffff:08x}",
                "title": title,
                "permalink": link,
                "selftext": body or "",
                "author": feed_url,
                "subreddit": feed_url,
            }})
        # RSS 2.0
        if not entries:
            for item in root.findall(".//item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                body = item.findtext("description", "")
                guid = item.findtext("guid", link)
                entries.append({"data": {
                    "id": f"rss_{hash(guid) & 0xffffffff:08x}",
                    "title": title,
                    "permalink": link,
                    "selftext": body or "",
                    "author": feed_url,
                    "subreddit": feed_url,
                }})
        return entries
    except Exception as e:
        print(f"[error] fetching RSS {feed_url}: {e}")
        return []


async def post_notification(channel: discord.TextChannel, data: dict):
    """Post embed + thread + reaction for a matched post."""
    title = data["title"]
    body = data.get("selftext") or ""
    url = f"https://reddit.com{data['permalink']}"
    combined = f"{title}\n{body}"

    closed = is_closed(combined)
    end_date = extract_end_date(combined)
    open_date = extract_open_date(combined)
    links = extract_links(body)
    matched_cats = match_categories(combined)
    if end_date and end_date < datetime.now(timezone.utc):
        closed = True

    color = 0x57f287 if not closed else 0xed4245

    # Build mention string for subscribers of matched categories
    subs = load_subs()
    mention_ids = set()
    for uid, cats in subs.items():
        if any(c in matched_cats for c in cats) or "general" in cats:
            mention_ids.add(int(uid))
    mentions = " ".join(f"<@{uid}>" for uid in mention_ids) if mention_ids else ""

    # Channel embed (compact summary)
    embed = discord.Embed(title=title, url=url, color=color)
    embed.add_field(name="Status", value="🔴 Closed" if closed else "🟢 Open", inline=True)
    if open_date:
        embed.add_field(name="Opens", value=format_end_date(open_date), inline=True)
    embed.add_field(name="Closes", value=format_end_date(end_date), inline=True)
    if matched_cats:
        embed.add_field(name="Categories", value=" ".join(f"`{c}`" for c in matched_cats), inline=False)
    embed.set_footer(text=f"r/{data['subreddit']} • u/{data['author']}")

    msg = await channel.send(content=mentions or None, embed=embed)
    await msg.add_reaction(CLOSED_EMOJI if closed else OPEN_EMOJI)

    thread = await msg.create_thread(
        name=title[:100],
        auto_archive_duration=10080,  # 7 days
    )

    # Thread header — dates + links first, clean and scannable
    header_lines = [
        f"**[View on Reddit]({url})**",
        f"**Status:** {'🔴 Closed' if closed else '🟢 Open'}",
    ]
    if open_date:
        header_lines.append(f"**Opens:** {format_end_date(open_date)}")
    header_lines.append(f"**Closes:** {format_end_date(end_date)}")
    if links:
        header_lines.append("**Links:**")
        for link in links[:5]:  # cap at 5
            header_lines.append(f"• <{link}>")
    header_lines += [
        f"**Posted by:** u/{data['author']} in r/{data['subreddit']}",
        "─" * 30,
    ]
    await thread.send("\n".join(header_lines))

    # Body in a separate message so it doesn't clutter the header
    if body:
        chunks = [body[i:i+1900] for i in range(0, min(len(body), 3800), 1900)]
        for chunk in chunks:
            await thread.send(chunk)

    # Persist for reaction flipping
    threads = load_threads()
    threads[data["id"]] = {
        "message_id": msg.id,
        "thread_id": thread.id,
        "channel_id": channel.id,
        "open_date": open_date.isoformat() if open_date else None,
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
        for feed_url in load_rss_feeds():
            posts = await fetch_rss(session, feed_url)
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

        for subreddit in load_subreddits():
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


@bot.command(name="notify")
async def cmd_notify(ctx, *categories: str):
    """Subscribe to notifications for categories. Usage: !notify movies tv music
    Available: movies, tv, music, books, games, general"""
    valid = [c.lower() for c in categories if c.lower() in CATEGORIES]
    invalid = [c for c in categories if c.lower() not in CATEGORIES]
    if not valid:
        cats = ", ".join(f"`{c}`" for c in CATEGORIES)
        await ctx.send(f"No valid categories. Available: {cats}")
        return
    subs = load_subs()
    uid = str(ctx.author.id)
    existing = set(subs.get(uid, []))
    existing.update(valid)
    subs[uid] = list(existing)
    save_subs(subs)
    msg = f"You'll be notified for: {', '.join(f'`{c}`' for c in sorted(existing))}"
    if invalid:
        msg += f"\nUnknown categories ignored: {', '.join(f'`{c}`' for c in invalid)}"
    await ctx.send(msg)


@bot.command(name="unnotify")
async def cmd_unnotify(ctx, *categories: str):
    """Unsubscribe from categories. Usage: !unnotify movies
    Use !unnotify all to remove all subscriptions."""
    subs = load_subs()
    uid = str(ctx.author.id)
    if not categories or "all" in categories:
        subs.pop(uid, None)
        save_subs(subs)
        await ctx.send("Removed all your notification subscriptions.")
        return
    existing = set(subs.get(uid, []))
    removed = [c.lower() for c in categories if c.lower() in existing]
    existing -= set(removed)
    if existing:
        subs[uid] = list(existing)
    else:
        subs.pop(uid, None)
    save_subs(subs)
    await ctx.send(f"Removed: {', '.join(f'`{c}`' for c in removed) or 'nothing'}. Still subscribed to: {', '.join(f'`{c}`' for c in sorted(existing)) or 'nothing'}")


@bot.command(name="mynotify")
async def cmd_mynotify(ctx):
    """Show your current notification subscriptions."""
    subs = load_subs()
    cats = subs.get(str(ctx.author.id), [])
    if not cats:
        await ctx.send("You have no notification subscriptions. Use `!notify <category>` to subscribe.")
    else:
        await ctx.send(f"You're subscribed to: {', '.join(f'`{c}`' for c in sorted(cats))}")


@bot.command(name="subreddits")
async def cmd_subreddits(ctx):
    """List watched subreddits."""
    subs = load_subreddits()
    await ctx.send("Watching: " + ", ".join(f"`r/{s}`" for s in subs))


@bot.command(name="addsub")
async def cmd_addsub(ctx, subreddit: str):
    """Add a subreddit to watch. Usage: !addsub torrents"""
    subreddit = subreddit.lstrip("r/").lower()
    subs = load_subreddits()
    if subreddit in subs:
        await ctx.send(f"`r/{subreddit}` is already being watched.")
        return
    subs.append(subreddit)
    save_subreddits(subs)
    await ctx.send(f"Added `r/{subreddit}` to the watch list.")


@bot.command(name="removesub")
async def cmd_removesub(ctx, subreddit: str):
    """Remove a subreddit. Usage: !removesub torrents"""
    subreddit = subreddit.lstrip("r/").lower()
    subs = load_subreddits()
    if subreddit not in subs:
        await ctx.send(f"`r/{subreddit}` is not being watched.")
        return
    subs.remove(subreddit)
    save_subreddits(subs)
    await ctx.send(f"Removed `r/{subreddit}` from the watch list.")


@bot.command(name="rss")
async def cmd_rss(ctx):
    """List watched RSS feeds."""
    feeds = load_rss_feeds()
    if not feeds:
        await ctx.send("No RSS feeds configured. Use `!addrss <url>` to add one.")
        return
    await ctx.send("RSS feeds:\n" + "\n".join(f"• <{f}>" for f in feeds))


@bot.command(name="addrss")
async def cmd_addrss(ctx, url: str):
    """Add an RSS feed to watch. Usage: !addrss https://example.com/feed.xml"""
    feeds = load_rss_feeds()
    if url in feeds:
        await ctx.send("That feed is already being watched.")
        return
    feeds.append(url)
    save_rss_feeds(feeds)
    await ctx.send(f"Added RSS feed: <{url}>")


@bot.command(name="removerss")
async def cmd_removerss(ctx, url: str):
    """Remove an RSS feed. Usage: !removerss https://example.com/feed.xml"""
    feeds = load_rss_feeds()
    if url not in feeds:
        await ctx.send("That feed isn't being watched.")
        return
    feeds.remove(url)
    save_rss_feeds(feeds)
    await ctx.send(f"Removed RSS feed: <{url}>")


@bot.command(name="categories")
async def cmd_categories(ctx):
    """List available notification categories."""
    embed = discord.Embed(title="Notification categories", color=0x00b0f4)
    for cat, terms in CATEGORIES.items():
        embed.add_field(name=f"`{cat}`", value=", ".join(terms[:5]), inline=False)
    await ctx.send(embed=embed)


bot.run(DISCORD_TOKEN)
