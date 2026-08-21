# NHCogs migration runbook

`NHCogsMigrator` is temporary. Run every migration command in a private
moderator channel. The moderator needs `Manage Messages`.

## Install the migration release

Update the repository, then install the combined suite and migrator without
unloading the existing cogs.

```ini
[p]repo update NHCogs
[p]cog update NHMisc Honeypot
[p]cog install NHCogs NHCogs NHCogsMigrator
[p]reload NHMisc Honeypot
[p]load NHCogsMigrator
```

`cog install` does not update cogs that Downloader already installed. Update
both legacy cogs first, then reload them to activate the migration release's
quiescent unload. Preflight refuses older loaded copies. Do not load `NHCogs`
manually.

## Plan and apply

```ini
[p]nhcogsmigrate plan
[p]nhcogsmigrate status
[p]nhcogsmigrate apply confirm
```

Review the attached JSON plan before applying. Confirmation is blocked for the
first 10 seconds. Apply stops both legacy cogs, creates and verifies the full
local backup, loads and validates `NHCogs`, then changes the persisted package
list. A failure before that final package write automatically restores the
backup and legacy runtime.

## Restart and finalize

After apply reports `committed`, restart Red normally. Do not use cog reload as
a substitute for the restart.

```ini
[p]nhcogsmigrate status
[p]nhcogsmigrate finalize
```

Status performs restart verification. Finalize removes `NHCogsMigrator` from
the persisted package list and unloads it. It does not delete the backup or the
legacy installed files.

After finalize reports success, wait for the migrator to unload, then remove
the temporary installed source packages:

```ini
[p]cog uninstall NHMisc Honeypot NHCogsMigrator
```

Only do this after a full external bot backup exists and status reached
`restart_verified`. Downloader removes the installed source packages but does
not delete the existing `NHMisc` or `Honeypot` cog data directories. From this
point, recovery uses the external full-bot backup instead of the legacy
packages. The later cleanup pull request removes the same temporary sources
from the repository.

## Automatic recovery

If Red stops before package authority is committed, the next normal start loads
`NHMisc` and `Honeypot`. The migrator verifies or restores the recorded backup
and returns the run to `rolled_back`.

If Red stops after the package write, the next normal start loads `NHCogs`. The
migrator verifies the new suite and moves the run to `restart_verified`.

## Manual recovery

If status reports `manual_intervention`, keep the entire backup directory and
do not load legacy and consolidated extensions together. Reinstall the
migration-release legacy packages, then retry the verified rollback:

```ini
[p]cog install NHCogs NHMisc Honeypot
[p]nhcogsmigrate recover confirm
[p]nhcogsmigrate status
```

Recovery verifies the recorded manifest, reconstructs an interrupted directory
swap, restores both data directories and both Config exports, reloads the
legacy cogs, checks their inventory, and finishes at `rolled_back`.

If recovery reports that the original package authority or a legacy cog is
missing, stop Red and do not copy individual files by hand. Keep the run ID,
the full backup path, `manifest.json`, `manifest.sha256`, both Config exports,
and the original package order from status. Correct that reported prerequisite,
start Red, and run the same recovery command again.

If finalization was interrupted and the migrator is no longer loaded, load it
and repeat finalization. The recorded finalized intent makes this idempotent:

```ini
[p]load NHCogsMigrator
[p]nhcogsmigrate finalize
```
