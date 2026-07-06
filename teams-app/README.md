# Teams app package (Phase 1)

Minimal manifest: a bot with `team` + `personal` scopes, no RSC permissions -- @mentions
in a channel and personal DMs are delivered to a registered bot by default; RSC
(`ChannelMessage.Read.Group`) is only needed to receive *all* channel messages, which is
out of scope for Phase 1 (see `docs/phase-1-teams-io.md` limits).

## Package and sideload

```bash
# Fill in the bot's Microsoft App ID (printed by infra/provision.sh as BOT_APP_ID --
# it's the brain managed identity's client ID, since Phase 1 uses a UserAssignedMSI bot).
sed -i '' "s/__BOT_APP_ID__/<BOT_APP_ID from provisioning output>/g" manifest.json

zip teams-triage-poc.zip manifest.json color.png outline.png
```

Upload `teams-triage-poc.zip` via Teams → Apps → **Manage your apps** → **Upload an app**
→ **Upload a custom app** (or through Teams admin center if custom app uploads are
restricted in your tenant). Install it into the target channel and/or start a DM with it.
