<p align="center">
  <a href="https://humalike.ai/"><img src="assets/wordmark.png" alt="Humalike" width="50%"></a>
</p>

<p align="center">
  <a href="https://github.com/Humalike/hermes-humalike-plugin/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Humalike/hermes-humalike-plugin" alt="License"></a>
  <a href="https://github.com/Humalike/hermes-humalike-plugin/stargazers"><img src="https://img.shields.io/github/stars/Humalike/hermes-humalike-plugin" alt="Stars"></a>
  <a href="https://github.com/Humalike/hermes-humalike-plugin/issues"><img src="https://img.shields.io/github/issues/Humalike/hermes-humalike-plugin" alt="Issues"></a>
  <a href="https://humalike.ai/"><img src="https://img.shields.io/badge/website-humalike.ai-1f6feb" alt="Website"></a>
</p>

# Humalike Plugin

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that makes
your bot feel like a person in the chat, powered by the
[Humalike](https://docs.humalike.com) APIs:

- **[Turn-taking](https://docs.humalike.com/api-reference/turn-taking/overview)** -
  decides when to jump in vs stay silent, and naturalizes replies so they read
  like a person wrote them, not one wall of bot text.
- **[Persona](https://docs.humalike.com/api-reference/personas)** -
  `/soul enhance` turns a one-line description into a fleshed-out character
  with a real voice.
- **[Theory of mind](https://docs.humalike.com/api-reference/foresee)** -
  considers how a message will land for the reader and adjusts before sending.
- **[Social learning](https://docs.humalike.com/api-reference/extract)** -
  picks up the group's slang, formatting, and jokes, and starts sounding like
  a member rather than a visitor.

## Install

```bash
git clone https://github.com/Humalike/hermes-humalike-plugin ~/.hermes/plugins/humalike

# run `hermes` from your Hermes install - activate its virtualenv first
# (or call the hermes CLI by its full path)
hermes plugins enable humalike
```

No env setup needed: the API URL defaults to `https://api.humalike.com`, and
the API key comes from a device login the plugin runs for you:

- **Automatic**: the first start without a key prints a login link on the
  console (and opens a browser tab when the machine has one). Approve it on
  any device - your phone works - and the key is saved to `~/.hermes/.env`.
- **At install time**: `python3 ~/.hermes/plugins/humalike/login.py` - the
  same flow in the terminal (stdlib-only, no Hermes venv needed).
- **From chat**: send the bot `/connect`. Same flow, and the key goes live
  without a restart.

### Configured for you on the first start

Everything turn-taking needs is applied automatically the first time the plugin
runs - it writes these settings itself and prints exactly what it changed. You
don't set any of them by hand; they're listed here so you know what and why:

| Setting (file) | Set to | Why |
|---|---|---|
| `streaming` (config.yaml) | `false` | the plugin rewrites the final reply into human-style messages, so Hermes must not also stream its own raw draft over the top |
| `group_sessions_per_user` (config.yaml) | `false` | everyone in a group shares one conversation, so the bot follows the whole room instead of a separate thread per person |
| `display.tool_progress` (config.yaml) | `off` | hides "Browsing…/Clicking…" tool chatter so replies read as human |
| `slack.reply_in_thread` (config.yaml, only if Slack is connected) | `false` | one shared conversation per channel; the default starts a fresh thread + session for **every** message, so the bot would answer each one separately |
| `WHATSAPP_*` / `SLACK_*` respond-to-everyone (`.env`, only for a connected platform) | open | reply to everyone in DMs and groups without needing an @mention |

**Restart the gateway once after this first boot** so the new values load - the
plugin's report ends with that reminder.

### Action required - the few things it can't do for you

- **Persona file location** - the plugin reads your persona from
  `~/.hermes/SOUL.md`. If yours lives somewhere else (e.g. a Docker mount),
  point it there in `~/.hermes/config.yaml`:
  ```yaml
  turn_taking:
    soul_path: "/path/to/SOUL.md"
  ```
- **Telegram groups** - two steps that genuinely can't be automated: disable
  privacy mode in @BotFather, and add the group's chat id to
  `TELEGRAM_GROUP_ALLOWED_CHATS`. The plugin prints both when Telegram is in use.
- **A personal WhatsApp number** - the respond-to-everyone setting means the bot
  replies to *everyone* who messages that number, in DMs and all groups. If it's
  your personal number, tighten it (the plugin warns about this at setup):
  `WHATSAPP_ALLOW_ALL_USERS=false` and/or `WHATSAPP_GROUP_POLICY=allowlist`.

### Overrides

`~/.hermes/.env` (`Authorization: Bearer`):

```bash
HUMALIKE_API_KEY=your-api-key   # skips the device login
HUMALIKE_API_URL=…              # non-default environment; set EMPTY to disable turn-taking
```

Optional persona knob in `~/.hermes/config.yaml` under `turn_taking:` -
`soul_auto_enhance` (`true` by default: the one-shot persona pass on first
startup; set `false` to skip it).

That's it - restart the gateway and message the bot. It now reads the room and
replies when it has something to say.

## Platforms

| Platform | Extra setup |
|---|---|
| WhatsApp | DMs work as-is. Groups: [`skills/configure-whatsapp-group`](skills/configure-whatsapp-group/SKILL.md) |
| Telegram | DMs work as-is. Groups: [`skills/configure-telegram-group`](skills/configure-telegram-group/SKILL.md) |
| Slack | DMs/@mentions work as-is. Unmentioned channel messages: [`skills/configure-slack-group`](skills/configure-slack-group/SKILL.md) |

## Persona: `/soul enhance`

Send the bot `/soul enhance` to deepen its persona: reads `SOUL.md`, enhances
it via the [Personas API](https://docs.humalike.com/api-reference/personas),
backs up the old file to `SOUL.md.bak`, writes the result - effective on the
next message. Needs a seed description first; a bare template is skipped. Runs
once automatically on first startup (disable: `soul_auto_enhance: false`).

## License

MIT.
