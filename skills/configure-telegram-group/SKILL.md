---
name: configure-telegram-group
description: Configure Hermes on Telegram so the bot sees and answers every message in a group — BotFather privacy mode, chat/user authorization, and the "bot doesn't respond in the group" diagnosis. Use when a user wants the turn-taking bot working in a Telegram group, or asks why it isn't replying there.
---

# Hermes on Telegram in a group

Env settings go in `~/.hermes/.env`; **restart the gateway** after changes.

## 1. Bot token

@BotFather → `/newbot` (or `/token` for an existing bot), then:
```
TELEGRAM_BOT_TOKEN=<token>
```

## 2. Disable privacy mode

By default a bot only receives @mentions in groups; turn-taking must see
everything. @BotFather → `/setprivacy` → pick the bot → **Disable**, then
**remove the bot from the group and re-add it** (the change only applies to a
fresh membership).

## 3. Authorization — who gets replies

Separate layer from turn-taking: unauthorized senders are rejected
(`Unauthorized user` in the logs) even though the bot sees the message.

- **DM** (e.g. testing the bot 1:1 first): the user messages the bot, gets a
  code, approve it with `hermes pairing approve telegram <CODE>`.
- **Group:** authorize the whole chat instead of pairing every member:
  ```
  TELEGRAM_GROUP_ALLOWED_CHATS=<chat_id1>,<chat_id2>   # * = all groups
  ```
  Find the chat_id in the logs: `tt inbound: chat=<chat_id>` (groups are
  negative). ⚠️ **Each group has its own chat_id** — a forum (topics) group is
  a different chat than a plain one, and converting changes the ID. Append each
  new group and restart; symptom of a missing entry: `Unauthorized user` even
  though "the other group works".

## 4. Restart and verify

Restart the Hermes gateway (however you run it). Look for `✓ telegram connected`
and `tt inbound / tt decide / tt forward`.

## Diagnosis: "the bot doesn't reply in the group"

| Log symptom | Cause |
|---|---|
| No `tt inbound` for the message | Privacy mode still on (step 2), or bot not in the group |
| `Unauthorized user: <id>` | Sender/chat not authorized (step 3) |
| `tt decide ... stay_silent` | Expected — turn-taking speaks selectively |
| `tt respond ... DROPPED (superseded)` | Message burst; only the newest gets a reply |
