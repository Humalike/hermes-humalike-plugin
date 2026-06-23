# turn-taking (Hermes plugin)

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that uses
the **turn-taking service** to (1) decide when the agent should speak vs **stay
silent**, and (2) **naturalize** each reply into 1–5 short chat messages.

v1 joins the naturalized messages into one bubble (no human-paced delivery — that
is v2). It does no analysis itself: the service decides and naturalizes; the
plugin only observes, calls, and applies the result.

## How it works

Two hooks, sharing a per-session cache:

- **`pre_gateway_dispatch`** (before the agent runs): POSTs the inbound message to
  `submit_messages`. On `stay_silent` it skips the reply entirely (no LLM cost);
  on `speak` it stashes the returned `turn_epoch`.
- **`transform_llm_output`** (after the draft is produced): POSTs the draft to
  `respond` with that epoch and replaces the draft with the joined messages.

The two hooks are correlated through Hermes's `session_store`, so the decide and
naturalize calls share one `session_id` and one turn epoch. The service stores
the transcript itself, so the plugin never rebuilds conversation history.

## Install (drop-in)

```bash
git clone <repo-url> ~/.hermes/plugins/turn-taking
hermes plugins enable turn-taking
```

Configure in `~/.hermes/config.yaml`:

```yaml
turn_taking:
  service_url: "https://your-service-host"  # POSTs to {url}/v1/turn-taking/actions/*
  system_prompt: "You are ..."             # optional: agent identity for decide/respond
  log_requests: false                       # optional: dump request/response JSONL

# Required for this plugin:
streaming: false                            # reply replace must own the final text
group_sessions_per_user: false              # one thread per group (group chats)
```

Set the API key (sent as `Authorization: Bearer`):

```bash
export TURN_TAKING_API_KEY="your-api-key"   # or add to ~/.hermes/.env
```

Restart the gateway so the plugin loads.

## Verifying it works (logging)

Every step logs at INFO to `~/.hermes/logs/agent.log`. Watch it live:

```bash
tail -f ~/.hermes/logs/agent.log | grep turn-taking
```

Expected lines:

```
turn-taking: registered decide + naturalize hooks
turn-taking: opened thread <id> for session <sid>
turn-taking: decision=speak epoch=N session=<sid> sender=<name>
turn-taking: speaking; stashed epoch N for session <sid>
turn-taking: naturalized session <sid> into K message(s) (X -> Y chars)
```

When the agent stays quiet you'll see `decision=stay_silent` → `staying silent`.

For exact request/response payloads, set `turn_taking.log_requests: true` and read
`~/.hermes/logs/turn-taking-requests.jsonl` and `turn-taking-responses.jsonl`.

## Persona: `/soul enhance`

Send the bot `/soul enhance` to deepen your agent's `SOUL.md` persona via the
[Humalike Personas API](https://docs.humalike.com). It's a registered slash command
(shows up in `/commands`). The plugin reads the current `SOUL.md`, sends it to
`actions/enhance` (async: it polls until the persona is rendered), backs the old
file up to `SOUL.md.bak`, and writes the enhanced persona back — it takes effect on
the next message. If `SOUL.md` has no persona yet (just the template), it tells you
to add a seed first — generating one from scratch is a later addition.

Note: plugin command handlers receive only the args, not the sender, so this can't
be restricted to DMs — anyone in a chat with the bot can run it. Restart the gateway
after installing/updating so the command registers.

```yaml
turn_taking:
  personas_api_url: "https://api.humalike.com"  # optional, this is the default
  soul_path: "~/.hermes/SOUL.md"                # optional; point at docker/SOUL.md to edit the repo copy
  soul_grounding: "off"                          # off | web | research (research can take minutes)
  soul_auto_enhance: true                        # optional; default true (see below)
```

The Personas API reuses `TURN_TAKING_API_KEY` unless you set `HUMALIKE_API_KEY`.

### Auto-enhance on first startup

On the **first gateway startup**, the plugin runs `/soul enhance` once automatically
(in the background, so it never blocks boot), then writes a marker
(`~/.hermes/.soul_auto_enhanced`) so it never runs again. It only fires when `SOUL.md`
already has a real persona seed and the API key is set — otherwise it harmlessly
retries on a later boot. Disable with `soul_auto_enhance: false` (or env
`HERMES_SOUL_AUTO_ENHANCE=false`). To force a re-run, delete the marker file.

## Failure behavior

Fail-open everywhere: if the service is unreachable, errors, or returns
`superseded`, the agent replies normally with its original draft. The plugin
never blocks or silently drops a reply on a bug — it only stays silent when the
service explicitly returns `stay_silent`.

## Limitations (v1)

- **No pacing** — `deliver_at` is ignored; messages join into one bubble.
- The service still schedules its own WebSocket delivery; we don't consume it.
- **No persistence** — thread/epoch caches are in-memory; lost on restart.
- **No Idempotency-Key retries.**
- Requires `streaming: false` on the platform.

## Testing

```bash
uv run --with pytest --with requests python -m pytest tests/ -q
```

## License

MIT.
