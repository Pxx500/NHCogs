# Cleanup

`Cleanup` replaces Red's bundled Cleanup cog with moderator commands backed by
Honeypot's durable message registry. It never scans channel history or fetches
boundary messages from Discord.

`[p]` means your bot prefix.

## Commands

```ini
[p]cleanup messages <count>
[p]cleanup user <user> <count>
[p]cleanup after [message_id] [delete_pinned]
[p]cleanup before [message_id] <count> [delete_pinned]
[p]cleanup between <older_id> <newer_id> [delete_pinned]
```

Running `[p]cleanup` shows the complete command overview. All actions require
Manage Messages. Counts must be from
1 through 1000. `after`, `before`, and `between` accept only message IDs retained
for the current channel. For `after` and `before`, reply to a retained message to
omit `message_id`.

Pinned messages are skipped unless `delete_pinned` is true. Range cleanup is
rejected before deletion when it contains more than 1000 matching messages.

## Operational errors

Unexpected cleanup failures use the shared `[p]nhcogs errors` configuration. See the
[shared command catalog](../README.md) for setup and privacy rules. Expected input,
permission, and empty-result outcomes use normal command feedback.

## Data and Discord access

Cleanup uses only messages observed through Discord Gateway events during the
last 14 days while Honeypot was loaded. Messages sent while the bot was offline
aren't available. The registry stores IDs, timestamps, pin state, author kind,
and an optional one-way fingerprint. It doesn't store message content or
attachments.

Discord is contacted only to delete the selected messages. Deletion uses batches
of at most 100 messages. The bot needs View Channel and Manage Messages in every
affected channel.

## Replacement behavior

Loading the consolidated NHCogs extension removes Red's bundled `cleanup` package
from autoload and unloads it before registering this cog. Startup stops the
replacement if an unknown cog or command already owns the `Cleanup` name.
