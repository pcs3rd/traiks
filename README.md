# traiks

A Discord bot that watches r/trackers and r/opensignups for open invite and signup posts, creates threads per tracker, and automatically flips reactions when invites expire.

![icon](icon.png)

## Features

- Polls r/trackers and r/opensignups on a configurable interval
- Creates a Discord thread for each matched post with full details
- 🟢/🔴 reaction indicating open/closed status
- Extracts closing dates from post text and auto-expires reactions
- Bot commands for manual checks, status, and configuration

## Quick start

```yaml
services:
  traiks:
    image: ghcr.io/pcs3rd/traiks:latest
    restart: unless-stopped
    environment:
      - DISCORD_TOKEN=your_bot_token
      - DISCORD_CHANNEL_ID=123456789012345678
      - CHECK_INTERVAL=900
    volumes:
      - state:/data

volumes:
  state:
```

See `compose.example.yaml` for a ready-to-use file.

## Bot setup

1. Go to [discord.com/developers](https://discord.com/developers/applications) → New Application → Bot
2. Enable **Message Content Intent** under Bot → Privileged Gateway Intents
3. Copy the token → set as `DISCORD_TOKEN`
4. Invite the bot to your server with `Send Messages`, `Create Public Threads`, `Add Reactions`, and `Read Message History` permissions

## Commands

| Command | Description |
|---|---|
| `!check` | Trigger a manual check now |
| `!status` | Show last check time, open tracker count, etc. |
| `!open` | List all currently open trackers |
| `!keywords` | Show the keywords being watched |
| `!interval <seconds>` | Change the polling interval |

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Discord bot token |
| `DISCORD_CHANNEL_ID` | Yes | — | Channel to post notifications in |
| `CHECK_INTERVAL` | No | `900` | Seconds between Reddit polls |

## Self-hosting (build from source)

```bash
git clone https://github.com/pcs3rd/traiks
cd traiks
docker build -t traiks .
```
