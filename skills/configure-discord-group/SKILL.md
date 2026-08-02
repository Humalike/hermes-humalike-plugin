---
name: configure-discord-group
description: Configure Hermes on Discord so the bot sees every message in a server channel, not just @mentions/DMs — Developer Portal bot creation, privileged intents, invite URL, and the authorization/mention gates. Use when a user wants the turn-taking bot working in a Discord server, or asks why it isn't replying to unmentioned channel messages.
---

# Hermes on Discord in a server

Env settings go in `~/.hermes/.env`; **restart the gateway** after changes.

## 1. Create the bot in the Developer Portal

https://discord.com/developers/applications → **New Application**, then
**Bot** → **Reset Token** (or create). Copy the token:
```bash
DISCORD_BOT_TOKEN=<token>
```

## 2. Enable the privileged intents

**Bot → Privileged Gateway Intents**, enable:
- **Message Content Intent** — required for server-channel message content;
  without it the bot sees only mentions/DMs (Discord delivers content there
  without the intent) and channel messages arrive effectively empty.
- **Server Members Intent** — required for role-based authorization
  (`DISCORD_ALLOWED_ROLES`) and member lookups.

## 3. Invite the bot to the server

OAuth2 → URL Generator, or use the ready-made invite:
```text
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot+applications.commands&permissions=274878286912
```
(`permissions=274878286912` = the bot+commands permission integer Hermes
expects. `applications.commands` enables slash commands.)

## 4. Authorization — who gets replies

Checked in the gateway before anything reaches turn-taking; unauthorized
senders are rejected even though the bot sees the message. Three models:

- **Allowlist (default):** only these user IDs may trigger the bot:
  ```
  DISCORD_ALLOWED_USERS=<user-id1>,<user-id2>
  ```
  Find user IDs in the gateway log line
  `Unauthorized user: <id> on discord` (unauthorized senders are rejected
  before turn-taking, so they never produce `tt inbound`), or Discord
  Developer Mode → right-click user → Copy User ID.
- **Role-based (recommended for servers):** grant via role instead of
  per-user — role grants propagate automatically when mod teams churn:
  ```
  DISCORD_ALLOWED_ROLES=<role-id1>,<role-id2>   # OR'd with DISCORD_ALLOWED_USERS
  ```
  Auto-enables the Members intent. Copy role IDs with Developer Mode →
  right-click role → Copy Role ID.
- **Open server (dev only):** every user can pass authorization:
  ```
  DISCORD_ALLOW_ALL_USERS=true
  ```
  This only bypasses the user/role allowlist — the @mention gate (section 5)
  still applies, so without `DISCORD_REQUIRE_MENTION=false` or
  `DISCORD_FREE_RESPONSE_CHANNELS` the bot still only answers when mentioned.
  Don't combine this with public channels unless you want a free-response bot
  in front of everyone.

## 5. The @mention gate (server channels only, not DMs)

Either:
```bash
DISCORD_REQUIRE_MENTION=false                          # whole bot, all channels
DISCORD_FREE_RESPONSE_CHANNELS=<chan_id1>,<chan_id2>   # or only these channels (safer)
```
Channel IDs: right-click the channel (Developer Mode) → Copy Channel ID, or
grab from the gateway logs after any message.

## 6. Turn-taking tuning (channels only)

```bash
DISCORD_AUTO_THREAD=false    # reply inline; auto-threads fragment the room into per-message sessions
DISCORD_REACTIONS=false      # no 👀/✅ ack reactions while processing
```
`DISCORD_REACTIONS` defaults to `true` — if you keep it, expect reaction acks
on each message. `DISCORD_AUTO_THREAD=false` matters for turn-taking: with
threads on, every top-level message becomes its own session and the bot can
never coalesce the conversation.

Fully open = `DISCORD_ALLOW_ALL_USERS=true` + `DISCORD_REQUIRE_MENTION=false` —
weigh it before doing this on a busy/public server. Prefer
`DISCORD_ALLOWED_USERS`/`DISCORD_ALLOWED_ROLES` + `DISCORD_FREE_RESPONSE_CHANNELS`.
To keep the bot out of specific channels entirely: `DISCORD_IGNORED_CHANNELS=<ids>`.

## Verify

Restart the Hermes gateway, then look for `✓ discord connected` and
`tt inbound` (message seen) on an unmentioned message. `tt decide` may
legitimately be `stay_silent` — that's turn-taking working, not a failure.

## Diagnosis: "the bot doesn't see unmentioned messages"

| Log symptom | Cause |
|---|---|
| `Unauthorized user: <id> on discord` | Gate A — add the user ID to `DISCORD_ALLOWED_USERS`/`DISCORD_ALLOWED_ROLES`, or `DISCORD_ALLOW_ALL_USERS=true` |
| No `tt inbound`, no unauthorized line | Gate B — mention still required for that channel (add it to `DISCORD_FREE_RESPONSE_CHANNELS`) |
| Message payload empty / no text | Message Content Intent not enabled (step 2) |
| `tt decide ... stay_silent` | Expected — turn-taking speaks selectively |
| `tt respond ... DROPPED (superseded)` | Message burst; only the newest gets a reply |
