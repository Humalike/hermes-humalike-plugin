---
name: configure-telegram-group
description: Configure Hermes on Telegram so the bot sees and answers every message in a group — BotFather privacy mode, chat/user authorization, and the "bot doesn't respond in the group" diagnosis. Use when a user wants the turn-taking bot working in a Telegram group, or asks why it isn't replying there.
---

# Hermes on Telegram in a group — checklist

Everything needed for the (turn-taking) bot to work and reply to people in a
group. Env settings go in `~/.hermes/.env`; **restart the gateway** after
changes.

## 1. Bot token

1. In Telegram, message **@BotFather** → `/newbot` (or `/token` for an
   existing bot).
2. Copy the token into `~/.hermes/.env`:
   ```
   TELEGRAM_BOT_TOKEN=<token>
   ```

## 2. Disable privacy mode (so the bot sees ALL group messages)

By default a bot in a group only receives messages that @mention it.
Turn-taking must see everything to decide when to speak.

1. @BotFather → `/setprivacy` → pick the bot → **Disable**.
2. **Remove the bot from the group and re-add it** — the privacy change only
   takes effect on a fresh membership.

## 3. Authorization — who gets replies

This is a SEPARATE layer from turn-taking. Turn-taking decides *whether* to
speak, but Hermes still rejects unauthorized senders (`Unauthorized user` in
the logs) — the bot "sees" the message but never replies.

- **DM:** each user must be paired. When someone messages the bot they get a
  code; approve it:
  ```
  hermes pairing approve telegram <CODE>
  ```
  (list: `hermes pairing list`, revoke: `hermes pairing revoke`).

- **Group (without pairing every member):** authorize the **whole chat by
  ID** — then every group member works automatically:
  ```
  TELEGRAM_GROUP_ALLOWED_CHATS=<chat_id1>,<chat_id2>   # comma-separated; * = all groups
  ```
  Find the group's chat_id in the gateway logs: `tt inbound: chat=<chat_id>`
  (Telegram groups have negative IDs).

  ⚠️ **Every group has its OWN chat_id** — a group with topics (forum) is a
  different chat than a plain group, and converting a group to a forum changes
  its ID. Adding one doesn't authorize the other; each new group must be
  appended to the list (comma-separated) and the gateway restarted. Symptom of
  a missing entry: `Unauthorized user` even though "the other group works".

  Alternatives: `TELEGRAM_GROUP_ALLOWED_USERS=<id1>,<id2>` (only the listed
  people), or pairing each person individually.

## 4. Restart the gateway

```
cd ~/repos/hermes-agent
.venv/bin/hermes gateway run -v
```

Check the logs for `✓ telegram connected` and `tt inbound / tt decide /
tt forward`.

## Quick diagnosis: "the bot doesn't reply in the group"

1. **No `tt inbound` for the message** → privacy mode is still on (step 2) or
   the bot isn't in the group.
2. **`tt decide ... stay_silent` shows up** → turn-taking is deliberately
   staying quiet (correct — it speaks selectively).
3. **`Unauthorized user: <id>` in the logs** → the sender isn't authorized
   (step 3).
4. **`tt respond ... DROPPED (superseded)`** → a rapid burst of messages; only
   the reply to the newest one is delivered.
