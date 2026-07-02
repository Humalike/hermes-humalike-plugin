---
name: configure-slack-group
description: Configure Hermes on Slack to see every message in a channel, not just @mentions/DMs — the authorization and mention gates, and the fail-open tradeoff. Use when a user wants the bot to respond in a Slack channel without being @mentioned, or asks why it isn't seeing unmentioned messages.
---

# Hermes on Slack in a channel

Settings go in `~/.hermes/.env` or the `slack:` section of
`~/.hermes/config.yaml`. **Restart the gateway** after changes.

## Caveat: fail-open

Slack has no fail-closed "observe" path like Telegram
(`_observe_unmentioned_group_message`) — every message either reaches the
turn-taking gate (`_handle_inbound`, via `handle_message`) or no event is built
for it at all. That gate is **fail-open** (built for DMs/@mentions:
if the turn-taking service is down, the bot replies anyway). So with the config
below, **if the turn-taking service goes down, the bot replies to EVERYTHING in
the channel.** Fine for a small trusted channel; a real fail-closed observe
would need a host-side change (`plugins/platforms/slack/adapter.py`).

## Two gates — open both

**Gate A — authorization** (checked first, in `gateway/authz_mixin.py`;
unauthorized messages never reach turn-taking):
```
SLACK_ALLOWED_USERS=<id1>,<id2>   # default: only these users
SLACK_ALLOW_ALL_USERS=true        # or: everyone in the workspace
```

**Gate B — the @mention requirement** (channels only, not DMs) — either:
```
SLACK_REQUIRE_MENTION=false                          # whole bot, all channels
SLACK_FREE_RESPONSE_CHANNELS=<chan_id1>,<chan_id2>   # or only these channels (safer)
```
Channel IDs: gateway logs after a message from that channel, or the Slack URL
(`.../C0123456789/...`). config.yaml equivalents: `require_mention: false` /
`free_response_channels: [...]` under `slack:`.

Fully open = `SLACK_ALLOW_ALL_USERS=true` + `SLACK_REQUIRE_MENTION=false` —
weigh the fail-open caveat before doing this on a busy/public workspace.
If disabling mentions globally, consider `SLACK_ALLOWED_CHANNELS=<ids>` so the
bot doesn't free-respond in every channel it gets added to.

## Verify

Restart the Hermes gateway, then look for `[Slack] Socket Mode connected` and
`tt inbound / tt decide / tt forward` on an unmentioned message.

## Diagnosis: "bot doesn't see unmentioned messages"

| Log symptom | Cause |
|---|---|
| `Unauthorized user: <id> on slack` | Gate A — add user or `SLACK_ALLOW_ALL_USERS=true` |
| No `tt inbound`, no unauthorized line | Gate B — mention still required for that channel |
| `Ignoring message in non-allowed channel` | Channel missing from `SLACK_ALLOWED_CHANNELS` |
| `tt decide ... stay_silent` | Expected — turn-taking speaks selectively |
| Replies to everything while service is down | Expected fail-open behavior (see caveat) |
