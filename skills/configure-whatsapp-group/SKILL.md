---
name: configure-whatsapp-group
description: Configure Hermes on WhatsApp to respond to everyone in groups and DMs, not just paired users or @mentions. Use when the bot ignores unmentioned or unpaired WhatsApp messages, or won't respond in groups.
---

# Hermes on WhatsApp — respond to everyone

Add to `~/.hermes/.env`, then restart the gateway:

```
WHATSAPP_ALLOW_ALL_USERS=true    # any sender
WHATSAPP_REQUIRE_MENTION=false   # no @mention needed
WHATSAPP_GROUP_POLICY=open       # process all groups (default "pairing" blocks them)
```

That's it — no privacy toggle like Telegram; the bridge already sees every
group message.
