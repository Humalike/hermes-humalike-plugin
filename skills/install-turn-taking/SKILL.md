---
name: install-turn-taking
description: Install and configure the Humalike turn-taking Hermes plugin — clone it into a Hermes agent, set the two env vars (HUMALIKE_API_URL, HUMALIKE_API_KEY) and the required config (streaming off, group sessions off, the turn_taking soul settings with a discovered soul_path). Use when a user wants to add turn-taking / persona (/soul enhance) / Humalike behavior to a Hermes Agent.
---

# Install the turn-taking plugin

Humalike plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent):
turn-taking, persona (`/soul enhance`), theory of mind, social learning.
API docs: <https://docs.humalike.com>.

The normal flow is just **clone → enable → start `hermes`**: starting the
gateway is what applies the required config and pops the login, so steps 2–3
below are mostly what that first start does for you, plus the manual fallbacks.
No env setup is required — the API URL defaults to `https://api.humalike.com`
and the API key comes from the device login. Only ask for a key if the user
says they already have one. Do the steps in order.

## 1. Clone and enable

```bash
git clone https://github.com/Humalike/hermes-humalike-plugin ~/.hermes/plugins/humalike

# `hermes` must be the Hermes CLI from its install — activate the Hermes
# virtualenv first (or call the hermes CLI by its full path)
hermes plugins enable humalike
```

## 2. Start Hermes — this applies the config and pops the login

`hermes plugins enable` only records the plugin; nothing runs until the gateway
starts. Start it (activate the Hermes virtualenv first, or use your gateway
start command):

```bash
hermes
```

On this first start the plugin self-configures (step 3) and prints a device
login URL — opening a browser tab when the machine has one. The user approves
on any device (a phone works) and the key is saved to `~/.hermes/.env`.

Two ways to skip that login:

- **Already have a key** — put it in `~/.hermes/.env` (create if missing)
  *before* starting, and the login isn't shown:
  ```bash
  HUMALIKE_API_KEY=their-api-key
  ```
- **Headless / can't keep the terminal open** — run the same login by hand:
  ```bash
  python3 ~/.hermes/plugins/humalike/login.py
  ```

If the login is dismissed, `/connect` in chat (step 4) links an account later
without a restart.

## 3. Required config

The plugin applies the settings below AUTOMATICALLY on its first boot (and
prompts Telegram's manual steps) — treat this section as verify/override, or
apply it by hand when the gateway can't be restarted twice.

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
  tool_progress: "off"        # hide tool-call chatter (Browsing/Clicking/…) so replies read as human
  busy_ack_enabled: false     # no deterministic "⚡ Interrupting current task…" acks
  memory_notifications: "off" # no "💾 Self-improvement review…" posts — MUST be the quoted
                              # string; bare YAML `off` parses as false and turns back into "on"
  platforms:
    telegram:
      streaming: false        # Telegram draft streaming shows the raw draft before naturalization
agent:
  disabled_toolsets: [clarify] # clarify's numbered menus bypass naturalization; without the
                               # tool the bot asks in plain text
slack:
  reply_in_thread: false # only if Slack is used — one shared conversation per channel

turn_taking:
  soul_path: "<the SOUL.md path found above>"
  soul_auto_enhance: true  # one-shot persona enhance on first startup
```

## 4. Restart and verify

Restart the Hermes gateway, then send the bot a message. A `tt inbound: chat=…`
line in `~/.hermes/logs/gateway.log` confirms turn-taking is intercepting
messages. If none appears, the plugin didn't load — check it shows as enabled
in `hermes plugins list` (or `HUMALIKE_API_URL` was explicitly set empty,
which disables turn-taking; `/soul` still works).

No API key set in step 2? Have the user send the bot `/connect` (from a DM,
not a group): it replies with a login link they approve in a browser on any
device — the key is then saved to `~/.hermes/.env` and goes live without
another restart.

## Platform notes

WhatsApp and Slack respond-to-everyone settings are auto-applied on the
plugin's first boot for platforms already connected (only filling values the
operator hasn't set). The skills below remain for Telegram (always manual),
later-added platforms, and overrides:

- **WhatsApp groups** — respond to everyone: `configure-whatsapp-group` skill.
- **Telegram groups** — privacy mode + chat authorization: `configure-telegram-group` skill.
- **Slack unmentioned channel messages** (fail-open caveat): `configure-slack-group` skill.
- `/soul enhance` (and first-startup auto-enhance) needs a real persona seed in
  `SOUL.md` — a bare template is skipped. Disable auto-enhance with
  `soul_auto_enhance: false` or `HERMES_SOUL_AUTO_ENHANCE=false`.
