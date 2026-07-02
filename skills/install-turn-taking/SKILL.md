---
name: install-turn-taking
description: Install and configure the Humalike turn-taking Hermes plugin — clone it into a Hermes agent, set the two env vars (HUMALIKE_API_URL, HUMALIKE_API_KEY) and the two required config keys (streaming off, group sessions off). Use when a user wants to add turn-taking / persona (/soul enhance) / Humalike behavior to a Hermes Agent.
---

# Install the turn-taking plugin

Installs the Humalike plugin for a [Hermes Agent](https://github.com/NousResearch/hermes-agent):
turn-taking (when to speak vs stay silent + a naturalized reply), persona via
`/soul enhance`, theory of mind, and social learning. Full API docs:
<https://docs.humalike.com>.

Do the steps in order. Stop and ask the user only for the two values marked
**ask**: the Humalike API URL and the API key.

## 1. Clone and enable

```bash
git clone https://github.com/Humalike/hermes-humalike-plugin ~/.hermes/plugins/turn-taking
hermes plugins enable turn-taking
```

## 2. Set the env vars

Add to `~/.hermes/.env` (create it if missing). One URL + one key covers every
Humalike call (turn-taking, persona, theory of mind, social learning), sent as
`Authorization: Bearer`:

```bash
HUMALIKE_API_URL=https://your-humalike-host   # ask (e.g. https://api.humalike.com)
HUMALIKE_API_KEY=your-api-key                 # ask
```

## 3. Set the required config

Merge into `~/.hermes/config.yaml` (create if missing). Both are **required** —
the plugin replaces the final reply text and needs to own it:

```yaml
streaming: false
group_sessions_per_user: false
```

## 4. Restart and verify

Restart the Hermes gateway, then check its logs for `turn-taking registered`
(and `registered /soul command`). If the logs say `turn-taking idle` instead,
`HUMALIKE_API_URL` isn't set in the gateway's environment.

## Optional settings

All optional, under `turn_taking:` in `~/.hermes/config.yaml` — only add what
the user asks for:

```yaml
turn_taking:
  system_prompt: "You are ..."                  # agent identity
  soul_path: "~/.hermes/SOUL.md"                # where the persona lives
  soul_grounding: "off"                         # off | web | research
  soul_auto_enhance: true                       # one-shot enhance on first startup
  personas_api_url: "https://api.humalike.com"  # persona API override
```

## Platform notes

- **WhatsApp** — nothing extra.
- **Telegram** — DMs work as-is. For group chats (privacy mode, chat
  authorization), use the `configure-telegram-group` skill.
- **Slack** — DMs/@mentions work as-is. To respond to unmentioned channel
  messages (with its fail-open caveat), use the `configure-slack-group` skill.
- A real persona seed in `SOUL.md` is needed before `/soul enhance` (or the
  first-startup auto-enhance) does anything — a bare template is skipped.
  Disable the one-time auto-enhance with `soul_auto_enhance: false` or env
  `HERMES_SOUL_AUTO_ENHANCE=false`.
