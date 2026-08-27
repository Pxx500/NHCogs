# NHModeration

NHModeration stores ban-related moderation evidence in SQLite and renders BanChart from local history. Chart rendering never reads the Discord API.

Version 1 does not replace ban, unban, mute, warn, or softban commands.

## Installation

```ini
[p]repo add NHCogs https://github.com/Pxx500/NHCogs
[p]cog install NHCogs NHModeration
[p]load NHModeration
```

The bot needs View Audit Log and Ban Members for migration and synchronization. Administrative output and BanChart must be used in a channel hidden from `@everyone`.

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
| `[p]nhmod status` | Show private migration, synchronization, schedule, and operational health |
| `[p]nhmod migrate` | Show migration commands |
| `[p]nhmod migrate plan` | Check cached permissions and local readiness without importing history |
| `[p]nhmod migrate run` | Start or resume the initial import |
| `[p]nhmod sync` | Fetch only entries after committed cursors |
| `[p]nhmod repair [confirm]` | Re-import all available sources, read the active ban list, and rebuild the projection |
| `[p]nhmod errors` | Show private error configuration and registered error commands |
| `[p]nhmod errors channel [channel]` | Show or set the private operational error channel |
| `[p]nhmod errors channel clear` | Clear the operational error channel |
| `[p]nhmod errors maintainer [member]` | Show or set the maintainer notified about operational failures |
| `[p]nhmod errors maintainer clear` | Clear the operational error maintainer |

The `nhmod` root requires Red moderator status or Manage Messages. Migration run, sync, and repair require administrator authorization. BanChart requires Red moderator status or Ban Members.

## Synchronization

Gateway and Red ModLog events are stored immediately. A low-cost catch-up runs after startup when migration is complete.

Weekly reconciliation runs every Sunday at `04:20 UTC`. It re-reads a 14-day audit overlap and does not read the full active ban list.

`sync` reads only new Red ModLog and audit entries. `repair` reads all Red ModLog and available audit entries, reads the full active ban list, and rebuilds the projection. Repair requires the literal `confirm` argument.

## Operational errors

Unexpected command, event, migration, synchronization, repair, scheduler, database, and rendering failures are stored in SQLite. Alerts and traceback attachments are sent only to the configured private error channel. Only the configured maintainer may be mentioned.

Expected input and permission errors return a short useful response. Public output never includes raw exceptions, audit IDs, case numbers, source keys, reasons, or database identifiers.

## Stored data and deletion

NHModeration stores immutable source observations and rebuildable canonical actions. Stored fields may include guild, target, technical executor, credited moderator, and channel IDs, action type, timestamps, reasons, expiry, source identity, migration identity, attribution, synchronization cursors, and operational failures.

Red user-data deletion anonymizes matching identities and reasons, then rebuilds affected actions. Guild removal deletes the guild's history, synchronization state, migration state, failures, and configuration.
