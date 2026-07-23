<p align="center">
  <a href="https://humalike.ai/"><img src="assets/humalike_x_hermes_banner.png" alt="Humalike × Hermes" width="100%"></a>
</p>

<p align="center">
  <a href="https://github.com/Humalike/hermes-humalike-plugin/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Humalike/hermes-humalike-plugin" alt="License"></a>
  <a href="https://github.com/Humalike/hermes-humalike-plugin/stargazers"><img src="https://img.shields.io/github/stars/Humalike/hermes-humalike-plugin" alt="Stars"></a>
  <a href="https://github.com/Humalike/hermes-humalike-plugin/issues"><img src="https://img.shields.io/github/issues/Humalike/hermes-humalike-plugin" alt="Issues"></a>
  <a href="https://humalike.ai/"><img src="https://img.shields.io/badge/website-humalike.ai-1f6feb" alt="Website"></a>
</p>

# Humalike Plugin

For any questions regarding Humalike, go to https://docs.humalike.com/.

One command gives your Hermes agent social intelligence. It decides when to speak, adapts to your group's tone and remembers who said what. Works in group chats on Slack, Telegram, WhatsApp and Discord, powered by the [Humalike](https://docs.humalike.com) APIs:

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

First update Hermes to the latest version - the plugin patches into Hermes
internals, so an out-of-date Hermes can leave turn-taking silently unhooked:

```bash
hermes update
```

Then clone and enable:

```bash
git clone https://github.com/Humalike/hermes-humalike-plugin ~/.hermes/plugins/humalike

# run `hermes` from your Hermes install - activate its virtualenv first
# (or call the hermes CLI by its full path)
hermes plugins enable humalike
```

Then start Hermes so the plugin loads and runs its first-start setup:

```bash
hermes
```

(`hermes plugins enable` only records the plugin; nothing happens until Hermes
actually starts. Use whatever command runs your bot - `hermes`, or your gateway
start command.)

On first start the plugin applies every required config
change itself and prints what it changed, where, and why. For authentication it
prints a login URL (and opens a browser tab when the machine has one) - approve
on any device and the key is saved to `~/.hermes/.env`.

You can also send the bot `/connect` at any time to link an account from chat;
the key goes live without a restart.

### Configured for you on the first start

Everything turn-taking needs is applied automatically the first time the plugin
runs - it writes these settings itself and prints exactly what it changed. You
don't set any of them by hand; they're listed here so you know what and why:

| Setting (file) | Set to | Why |
|---|---|---|
| `streaming` (config.yaml) | `false` | the plugin rewrites the final reply into human-style messages, so Hermes must not also stream its own raw draft over the top |
| `group_sessions_per_user` (config.yaml) | `false` | everyone in a group shares one conversation, so the bot follows the whole room instead of a separate thread per person |
| `display.tool_progress` (config.yaml) | `off` | hides "Browsing…/Clicking…" tool chatter so replies read as human |
| `display.busy_ack_enabled` (config.yaml) | `false` | suppresses the deterministic "⚡ Interrupting current task…" busy acks - a human doesn't announce that |
| `display.memory_notifications` (config.yaml) | `off` | hides the "💾 Self-improvement review…" background-memory posts - a human doesn't announce memory updates |
| `display.platforms.telegram.streaming` (config.yaml) | `false` | Telegram's native draft streaming would show the raw draft before naturalization |
| `agent.disabled_toolsets` (config.yaml) | `+ clarify` | the clarify tool's numbered-option menus are sent by the gateway directly, bypassing naturalization; without it the bot asks in plain text |
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
- **Discord bot setup** - steps that can't be automated. Create the bot in the
  [Developer Portal](https://discord.com/developers/applications), enable
  **Message Content Intent** and **Server Members Intent** (Bot → Privileged
  Gateway Intents - without the first the bot receives empty messages), invite
  it with
  `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot+applications.commands&permissions=274878286912`,
  then add to `~/.hermes/.env`:
  ```bash
  DISCORD_BOT_TOKEN=...                # from the Developer Portal
  DISCORD_ALLOWED_USERS=<user-id,...>  # without this the gateway denies everyone
  DISCORD_REQUIRE_MENTION=false        # let turn-taking see unmentioned channel messages
  #  (or per-channel: DISCORD_FREE_RESPONSE_CHANNELS=<channel-id,...>)
  DISCORD_AUTO_THREAD=false            # reply inline; auto-threads would fragment the room
  DISCORD_REACTIONS=false              # no 👀/✅ ack reactions
  ```

### Overrides

`~/.hermes/.env` (`Authorization: Bearer`):

```bash
HUMALIKE_API_KEY=your-api-key   # skips the login prompt
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
| Discord | DMs work as-is. Channels: enable both privileged intents, set `DISCORD_ALLOWED_USERS`, and `DISCORD_REQUIRE_MENTION=false` (or `DISCORD_FREE_RESPONSE_CHANNELS=<channel-id,...>`) - see **Discord bot setup** above |

### Discord notes

- **Typing indicator** - Discord's typing is a persistent refresh loop, so the
  plugin mutes the host's think-time typing and shows typing only while a reply
  bubble is being paced, with an explicit stop after the last bubble.
- **Captionless media** - images/files sent without text reach the service as a
  clean `[image]`/`[media]` marker (with `has_media`), not the host's
  placeholder sentence, so turn-taking knows media arrived.
- **Fast message bursts** - a follow-up that arrives while the bot is composing
  is merged into the in-flight turn and the reply is re-stamped with the newest
  turn epoch, so quick "hermes" / "are you here?" bursts get an answer instead
  of a silently superseded one.
- **Mentions** - `<@id>`/`<@!id>` tokens are resolved for the service: the bot's
  own mention becomes `@you`, other people become `@DisplayName`.
- Slash commands (`/new`, `/sethome`, …) bypass the turn-taking gate.

## Persona: `/soul enhance`

Send the bot `/soul enhance` to deepen its persona: reads `SOUL.md`, enhances
it via the [Personas API](https://docs.humalike.com/api-reference/personas),
backs up the old file to `SOUL.md.bak`, writes the result - effective on the
next message. Needs a seed description first; a bare template is skipped. Runs
once automatically on first startup (disable: `soul_auto_enhance: false`).

## License

MIT.
