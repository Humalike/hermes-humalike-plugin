---
name: install-turn-taking
description: Install and configure the Humalike turn-taking Hermes plugin — clone it into a Hermes agent, set the required config (streaming off, group sessions off, service URL), and wire up the API key. Use when a user wants to add turn-taking / persona (/soul enhance) / Humalike behavior to a Hermes Agent.
---

# Install the turn-taking plugin

This skill installs the [turn-taking](https://github.com/NousResearch/hermes-agent)
plugin for a [Hermes Agent](https://github.com/NousResearch/hermes-agent). It gives
the agent the Humalike behaviors: turn-taking (when to speak vs stay silent + a
naturalized reply), persona via `/soul enhance`, theory of mind, and social
learning. Full API docs: <https://docs.humalike.com>.

Do the steps in order. Stop and ask the user only for the two values marked
**ask**: the turn-taking `service_url` and the API key.

## 1. Clone the plugin

```bash
git clone <repo-url> ~/.hermes/plugins/turn-taking
hermes plugins enable turn-taking
```

## 2. Configure `~/.hermes/config.yaml`

Merge this into the user's `~/.hermes/config.yaml` (create the file if missing).
The two `streaming`/`group_sessions_per_user` settings are **required** — the
plugin replaces the final reply text and needs to own it.

```yaml
turn_taking:
  service_url: "https://your-service-host"      # ask: turn-taking service endpoint
  personas_api_url: "https://api.humalike.com"  # optional, this is the default
  system_prompt: "You are ..."                  # optional: agent identity
  soul_path: "~/.hermes/SOUL.md"                # optional; where the persona lives
  soul_grounding: "off"                          # off | web | research
  soul_auto_enhance: true                        # optional; default true

# Required:
streaming: false
group_sessions_per_user: false
```

## 3. Set the API key

Sent as `Authorization: Bearer`. The Humalike persona/ToM/social APIs reuse this
unless `HUMALIKE_API_KEY` is also set.

```bash
export TURN_TAKING_API_KEY="your-api-key"   # ask: or add to ~/.hermes/.env
```

## 4. Restart the gateway

Restart the Hermes gateway so the plugin and its `/soul enhance` command register.

## Notes

- A real persona seed in `SOUL.md` is needed before `/soul enhance` (or the
  first-startup auto-enhance) does anything — a bare template is skipped.
- Disable the one-time auto-enhance with `soul_auto_enhance: false` or env
  `HERMES_SOUL_AUTO_ENHANCE=false`.
