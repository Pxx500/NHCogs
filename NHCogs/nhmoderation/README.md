# NHModeration

NHModeration stores ban-related moderation evidence in SQLite, renders BanChart from local history, and deletes messages containing configured phrases. Chart rendering and message filtering never read the Discord API.

Version 1 does not replace ban, unban, mute, warn, or softban commands.

## Installation

```ini
[p]repo add NHCogs https://github.com/Pxx500/NHCogs
[p]cog install NHCogs NHModeration
[p]load NHModeration
```

The bot needs View Audit Log and Ban Members for migration and synchronization. It needs Manage Messages to delete filtered messages. Message Content intent must be enabled for phrase matching. Administrative `nhmod` output must be used in a channel hidden from `@everyone`. BanChart may be posted in public channels.

## Initial migration

Run the read-only readiness check first:

```ini
[p]nhmod migrate plan
```

Then import all available Red ModLog cases, Discord ban and unban audit entries, and one active ban snapshot:

```ini
[p]nhmod migrate run
```

The import is idempotent and can be retried. Discord audit history is retained for a limited time, so old actions missing from Red ModLog may not be recoverable.
The migration report and `[p]nhmod status` record when a historical coverage gap is possible. This diagnostic is not shown on BanChart.

## BanChart

```ini
[p]banchart [days|all] [amount] [--automation]
```

Defaults are 30 days, 10 named moderators, and no pure automation. `--automation` adds the Automation category. Human-approved Honeypot actions remain credited to the approving moderator. Unknown attribution appears last. Softbans and unbans do not enter BanChart.

Names are resolved only from the Discord cache. The command does not call `fetch_user` or another Discord REST endpoint.

## Administration commands

`[p]nhmod` shows its direct command categories. Every nested group shows its registered commands with the active prefix and complete syntax.

| Command | Description |
|---|---|
| `[p]nhmod` | Show the NHModeration command overview |
| `[p]nhmod status` | Show private migration, historical coverage, synchronization, and schedule health |
| `[p]nhmod filter` | Show filter commands and the configured phrases |
| `[p]nhmod filter add <phrase>` | Add a phrase to the message filter |
| `[p]nhmod filter remove <phrase>` | Remove a phrase from the message filter |
| `[p]nhmod filter list` | List the configured phrases |
| `[p]nhmod migrate` | Show migration commands |
| `[p]nhmod migrate plan` | Check cached permissions and local readiness without importing history |
| `[p]nhmod migrate run` | Start or resume the initial import |
| `[p]nhmod sync` | Fetch only entries after committed cursors |
| `[p]nhmod repair [confirm]` | Re-import all available sources, read the active ban list, and rebuild the projection |

The `nhmod` root, all maintenance commands, and BanChart require Manage Messages.

## Message phrase filter

The filter applies to guild message content and text inside embeds. It checks embed titles, descriptions, field names, field values, author names, and footer text. Matching is a case-insensitive plain substring check. It also matches a phrase inside a larger word. The first match deletes the whole message without posting a public response. Messages from moderators, bots, and webhooks use the same rules.

New messages and cached message edits are checked. This covers embeds that Discord adds or updates after the original message without fetching the message from the Discord API.

Phrases are configured per guild and normalized before storage. The listener reads a memory cache that is restored when the cog loads and updated by the filter commands. Deletion is not recorded as a moderation action and does not affect BanChart.

## Synchronization

Gateway and Red ModLog events are stored immediately. A low-cost catch-up runs after startup when migration is complete.

Weekly reconciliation runs every Sunday at `04:20 UTC`. It re-reads a 14-day audit overlap and does not read the full active ban list.

`sync` reads only new Red ModLog and audit entries. `repair` reads all Red ModLog and available audit entries, reads the full active ban list, and rebuilds the projection. Repair requires the literal `confirm` argument.

## Operational errors

Unexpected command, event, migration, synchronization, repair, scheduler, database, and rendering failures are stored in SQLite and written to the Python logger. NHModeration does not own separate error channel or maintainer commands.

Expected input and permission errors return a short useful response. Public output never includes raw exceptions, audit IDs, case numbers, source keys, reasons, or database identifiers.

## Stored data and deletion

NHModeration stores immutable source observations and rebuildable canonical actions. Stored fields may include guild, target, technical executor, credited moderator, and channel IDs, action type, timestamps, reasons, expiry, source identity, migration identity, attribution, synchronization cursors, and operational failures. Configured message filter phrases are stored per guild in Red Config.

Red user-data deletion anonymizes matching identities and reasons, then rebuilds affected actions. Guild removal deletes the guild's history, synchronization state, migration state, failures, and configuration.
