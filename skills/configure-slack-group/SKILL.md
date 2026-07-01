---
name: configure-slack-group
description: Configure Hermes on Slack to see every message in a channel, not just @mentions/DMs — the authorization and mention gates, and the fail-open tradeoff. Use when a user wants the bot to respond in a Slack channel without being @mentioned, or asks why it isn't seeing unmentioned messages.
---

# Hermes on Slack in a channel — checklist

How to make the bot (turn-taking) see EVERY message in a channel, not just
ones with an @mention. Settings go in `~/.hermes/config.yaml` (the `slack:`
section) or `~/.hermes/.env`; **restart the gateway** after changing them.

## Important: this is a config-only workaround, not a real "observe"

Unlike Telegram, Slack has **no** separate "overhear but don't reply" path
(`_observe_unmentioned_group_message`). Slack only has one gate: a message
either gets processed and reaches `handle_message` (i.e. our turn-taking gate,
`_handle_inbound`), or no `MessageEvent` is ever built for it at all.

Our turn-taking gate (`_handle_inbound`) is **fail-open** — designed for
messages explicitly directed at the bot (DM / @mention): if the turn-taking
service goes down, the bot replies anyway (the safe default when someone is
addressing it directly). Enabling the config below means EVERY message in the
channel (even ones never meant for the bot) goes through that same fail-open
gate.

**Consequence:** if the turn-taking service (`service_url`) becomes
unreachable or errors out, the bot will start replying to EVERYTHING in that
channel, not just messages directed at it. That's a deliberate tradeoff — a
real fail-closed "observe" (like Telegram has) would require a host-side
change (`plugins/platforms/slack/adapter.py`), see the `hermes-agent`
conversation from 2026-07-01. For a small, trusted channel this risk is
usually acceptable.

## 1. Two SEPARATE gates — both must be opened for "everyone, every channel"

Slack authorization is checked BEFORE the mention gate, in a different layer
(`gateway/authz_mixin.py`). A message from an unauthorized user never reaches
turn-taking at all — it's dropped with `Unauthorized user: <id> on slack` in
the logs, before `handle_message` is even called. Opening only the mention
gate (below) still leaves the bot deaf to anyone not explicitly authorized.

**Gate A — authorization (who is allowed to talk to the bot at all):**
```
SLACK_ALLOWED_USERS=<user_id1>,<user_id2>   # default: only these users
# or, to let EVERYONE in the workspace through:
SLACK_ALLOW_ALL_USERS=true
```

**Gate B — the @mention requirement (only applies to channels, not DMs):**

Option A — whole bot, all channels:
```
SLACK_REQUIRE_MENTION=false
```

Option B — only specific channels (safer):
```
SLACK_FREE_RESPONSE_CHANNELS=<channel_id1>,<channel_id2>
```
`require_mention` stays enabled everywhere else; only the listed channels get
"free response". Find the channel ID in the gateway logs after the first
message from that channel, or in Slack's URL (`.../C0123456789/...`).

Equivalent in `~/.hermes/config.yaml`:
```yaml
slack:
  require_mention: false          # gate B, option A
  # or:
  free_response_channels:         # gate B, option B
    - "C0123456789"
```

**For "every message from everyone, bot decides" (no scoping):**
```
SLACK_ALLOW_ALL_USERS=true
SLACK_REQUIRE_MENTION=false
```
Both env vars, no channel/user whitelist. This is genuinely open — any Slack
user, in any channel the bot is in, on every message — combined with the
fail-open turn-taking gate (see "Important" above), so weigh that before
flipping it bot-wide on a busy/public workspace.

## 2. (Optional) restrict to trusted channels

If using option A (global disable), consider also adding a whitelist so the
bot doesn't start free-responding in every channel it gets added to:
```
SLACK_ALLOWED_CHANNELS=<channel_id1>,<channel_id2>
```

## 3. Restart the gateway
```
cd ~/repos/hermes-agent
.venv/bin/hermes gateway run -v
```
Check the logs for: `[Slack] Socket Mode connected` and `tt inbound / tt decide / tt forward`
for messages without an @mention in the configured channel.

## Quick diagnosis: "bot doesn't see messages without a mention"

0. **`Unauthorized user: <id> on slack` in the logs** → gate A (above), not
   gate B — the message never reached turn-taking at all. Add the user to
   `SLACK_ALLOWED_USERS` or set `SLACK_ALLOW_ALL_USERS=true`.
1. **No `tt inbound` for an unmentioned message (and no "Unauthorized user"
   line either)** → gate B — `require_mention` is still `true` for that
   channel (check option A/B above), or the channel isn't in
   `free_response_channels`.
2. **`Ignoring message in non-allowed channel` in the logs** → the channel
   isn't in `SLACK_ALLOWED_CHANNELS` (the whitelist blocks it even with
   `require_mention` disabled).
3. **`tt decide ... stay_silent` shows up** → turn-taking is deliberately
   staying quiet (expected — it decides selectively, same as Telegram groups).
4. **Bot replies to everything when the turn-taking service is down** → this
   is the expected behavior of this workaround (see "Important" above), not
   a bug.
