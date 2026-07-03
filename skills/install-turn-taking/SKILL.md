---
name: install-turn-taking
description: Install and configure the Humalike turn-taking Hermes plugin — clone it into a Hermes agent, set the two env vars (HUMALIKE_API_URL, HUMALIKE_API_KEY) and the required config (streaming off, group sessions off, the turn_taking soul settings with a discovered soul_path). Use when a user wants to add turn-taking / persona (/soul enhance) / Humalike behavior to a Hermes Agent.
---

# Install the turn-taking plugin

Humalike plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
turn-taking, persona (`/soul enhance`), theory of mind, social learning.
API docs: <https://docs.humalike.com>.

Do the steps in order. The URL defaults to `https://api.humalike.com`. For the
API key, ask whether the user already has one: if yes, set it in step 2; if
not, skip it there — after the restart in step 4 they link their account by
sending the bot `/connect` (a login link, approved in any browser).

## 1. Clone and enable

```bash
git clone https://github.com/Humalike/hermes-humalike-plugin ~/.hermes/plugins/humalike

# `hermes` must be the Hermes CLI from its install — activate the Hermes
# virtualenv first (or call the hermes CLI by its full path)
hermes plugins enable humalike
```

## 2. Env vars

In `~/.hermes/.env` (create if missing) — one URL + one key covers all
Humalike calls, sent as `Authorization: Bearer`:

```bash
HUMALIKE_API_URL=https://api.humalike.com
HUMALIKE_API_KEY=your-api-key   # ask — omit if the user will link via login.py or /connect
HUMALIKE_CLI_GATEWAY_KEY=…      # only for the login/connect flow — the plugin's
                                # public client id (see the Humalike docs)
```

No API key? Run the device login right now, in the terminal:

```bash
python3 ~/.hermes/plugins/humalike/login.py
```

It prints the approval URL and opens a browser tab when the install machine
has one; the user approves on any device (phone works) and the key lands in
`.env` before first boot. Requires `HUMALIKE_CLI_GATEWAY_KEY` above. If you
skip this, the first gateway start pops the same login automatically, and
`/connect` in chat (step 4) is the fallback.

## 3. Required config

First find where SOUL.md actually lives: check `~/.hermes/SOUL.md`; if it's not
there, search the Hermes install for an existing one (e.g. a `docker/SOUL.md`
or a path mounted into the gateway). If none exists anywhere, use
`~/.hermes/SOUL.md` — the plugin creates it on first startup.

Then merge into `~/.hermes/config.yaml` (create if missing) — all **required**
(the plugin replaces the final reply text and needs to own it):

```yaml
streaming: false
group_sessions_per_user: false
display:
  tool_progress: "off"   # hide tool-call chatter (Browsing/Clicking/…) so replies read as human

turn_taking:
  soul_path: "<the SOUL.md path found above>"
  soul_grounding: "off"    # off | web | research — real-world research on enhance
  soul_auto_enhance: true  # one-shot persona enhance on first startup
```

## 4. Restart and verify

Restart the Hermes gateway, then send the bot a message. A `tt inbound: chat=…`
line in `~/.hermes/logs/gateway.log` confirms turn-taking is intercepting
messages. If none appears, `HUMALIKE_API_URL` isn't set in the gateway's
environment — turn-taking stays disabled (`/soul` still works).

No API key set in step 2? Have the user send the bot `/connect` (from a DM,
not a group): it replies with a login link they approve in a browser on any
device — the key is then saved to `~/.hermes/.env` and goes live without
another restart.

## Platform notes

- **WhatsApp groups** — respond to everyone: `configure-whatsapp-group` skill.
- **Telegram groups** — privacy mode + chat authorization: `configure-telegram-group` skill.
- **Slack unmentioned channel messages** (fail-open caveat): `configure-slack-group` skill.
- `/soul enhance` (and first-startup auto-enhance) needs a real persona seed in
  `SOUL.md` — a bare template is skipped. Disable auto-enhance with
  `soul_auto_enhance: false` or `HERMES_SOUL_AUTO_ENHANCE=false`.
