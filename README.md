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
  service_url: "https://api.example.com"   # POSTs to {url}/v1/turn-taking/actions/*
  system_prompt: "You are ..."             # optional: agent identity for decide/respond
  log_requests: false                       # optional: dump request/response JSONL

# Required for this plugin:
streaming: false                            # reply replace must own the final text
group_sessions_per_user: false              # one thread per group (group chats)
```

Set the Clerk API key (sent as `Authorization: Bearer`):

```bash
export TURN_TAKING_API_KEY="your-clerk-api-key"   # or add to ~/.hermes/.env
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
