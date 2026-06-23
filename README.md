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
your bot feel like a person in the chat instead of a bot. It plugs into the
[Humalike](https://docs.humalike.com) APIs so the agent knows *when* to speak,
*how* to say it, *who* it is, and *who it's talking to*.

## What it does (from the chat's side)

- **Turn-taking** — the agent doesn't reply to every message the instant it
  lands. It reads the rhythm of the conversation and decides whether to jump in
  now or stay silent, then naturalizes its reply to make it read like a person
  actually wrote it instead of one wall of bot text.
  ([Turn-Taking API](https://docs.humalike.com/api-reference/turn-taking/overview))
- **Persona** — `/soul enhance` turns a one-line description of your bot into a
  fully fleshed-out character with a real voice, so replies sound like *someone*
  rather than a generic assistant.
  ([Persona API](https://docs.humalike.com/api-reference/personas))
- **Theory of mind** — before sending, the agent considers how the message is
  likely to land for the person on the other end, and adjusts so it reads the
  room instead of steamrolling it.
  ([Theory of Mind API](https://docs.humalike.com/api-reference/foresee))
- **Social learning** — the agent picks up the group's actual voice from the
  conversation — its slang, formatting, running jokes, level of formality — and
  starts sounding like a member of the chat rather than a visitor.
  ([Social Learning API](https://docs.humalike.com/api-reference/extract))

Full API reference: <https://docs.humalike.com>.

## Install

> Using an agent harness (Claude Code, etc.)? Point it at
> [`skills/install-turn-taking/SKILL.md`](skills/install-turn-taking/SKILL.md) and it
> can do the install and config below for you.

```bash
git clone <repo-url> ~/.hermes/plugins/turn-taking
hermes plugins enable turn-taking
```

Configure in `~/.hermes/config.yaml`:

```yaml
turn_taking:
  service_url: "https://your-service-host"  # POSTs to {url}/v1/turn-taking/actions/*
  personas_api_url: "https://api.humalike.com"  # optional, this is the default
  system_prompt: "You are ..."             # optional: agent identity
  soul_path: "~/.hermes/SOUL.md"           # optional; where the persona lives
  soul_grounding: "off"                     # off | web | research
  soul_auto_enhance: true                   # optional; default true

# Required for this plugin:
streaming: false                            # reply replace must own the final text
group_sessions_per_user: false              # one thread per group (group chats)
```

Set the API key (sent as `Authorization: Bearer`):

```bash
export TURN_TAKING_API_KEY="your-api-key"   # or add to ~/.hermes/.env
```

The Humalike APIs reuse `TURN_TAKING_API_KEY` unless you set `HUMALIKE_API_KEY`.

Restart the gateway so the plugin loads.

## Persona: `/soul enhance`

Send the bot `/soul enhance` to deepen its persona via the
[Personas API](https://docs.humalike.com/api-reference/personas). The plugin
reads the current `SOUL.md`, enhances it, backs the old file up to `SOUL.md.bak`,
and writes the new persona back — it takes effect on the next message. If
`SOUL.md` only has the template, add a seed description first. On the first
gateway startup the plugin also runs this once automatically (in the background);
disable with `soul_auto_enhance: false`.

## License

MIT.
