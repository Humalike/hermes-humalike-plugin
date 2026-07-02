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

- **[Turn-taking](https://docs.humalike.com/api-reference/turn-taking/overview)** —
  decides when to jump in vs stay silent, and naturalizes replies so they read
  like a person wrote them, not one wall of bot text.
- **[Persona](https://docs.humalike.com/api-reference/personas)** —
  `/soul enhance` turns a one-line description into a fleshed-out character
  with a real voice.
- **[Theory of mind](https://docs.humalike.com/api-reference/foresee)** —
  considers how a message will land for the reader and adjusts before sending.
- **[Social learning](https://docs.humalike.com/api-reference/extract)** —
  picks up the group's slang, formatting, and jokes, and starts sounding like
  a member rather than a visitor.

## Install

> Using an agent harness (Claude Code, etc.)? Point it at
> [`skills/install-turn-taking/SKILL.md`](skills/install-turn-taking/SKILL.md)
> and it can do all of the below for you.

```bash
git clone https://github.com/Humalike/hermes-humalike-plugin ~/.hermes/plugins/turn-taking

# run `hermes` from your Hermes install — activate its virtualenv first
# (or call the hermes CLI by its full path)
hermes plugins enable turn-taking
```

`~/.hermes/.env` — one URL + one key for all Humalike calls
(`Authorization: Bearer`):

```bash
HUMALIKE_API_URL=https://api.humalike.com
HUMALIKE_API_KEY=your-api-key
```

`~/.hermes/config.yaml` — all required:

```yaml
streaming: false                # the plugin replaces the final reply text
group_sessions_per_user: false  # one thread per group chat
display:
  tool_progress: "off"          # hide tool-call chatter (Browsing/Clicking/…) so replies read as human

turn_taking:
  soul_path: "~/.hermes/SOUL.md"  # where your SOUL.md actually lives — adjust if elsewhere (e.g. a Docker mount)
  soul_grounding: "off"           # off | web | research — real-world research when enhancing the persona
  soul_auto_enhance: true         # one-shot persona enhance on first startup
```

Restart the gateway and message the bot — it now reads the room and replies
when it has something to say. Done.

## Platforms

| Platform | Extra setup |
|---|---|
| WhatsApp | DMs work as-is. Groups: [`skills/configure-whatsapp-group`](skills/configure-whatsapp-group/SKILL.md) |
| Telegram | DMs work as-is. Groups: [`skills/configure-telegram-group`](skills/configure-telegram-group/SKILL.md) |
| Slack | DMs/@mentions work as-is. Unmentioned channel messages: [`skills/configure-slack-group`](skills/configure-slack-group/SKILL.md) |

## Persona: `/soul enhance`

Send the bot `/soul enhance` to deepen its persona: reads `SOUL.md`, enhances
it via the [Personas API](https://docs.humalike.com/api-reference/personas),
backs up the old file to `SOUL.md.bak`, writes the result — effective on the
next message. Needs a seed description first; a bare template is skipped. Runs
once automatically on first startup (disable: `soul_auto_enhance: false`).

## License

MIT.
