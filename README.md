# NHCogs

Red-DiscordBot V3 cogs maintained for the NewHorizons Discord server.

## Available cogs

The migration release adds the combined `NHCogs` extension. The top-level
`NHMisc` and `Honeypot` packages are frozen rollback copies until the production
migration has been verified and finalized. Feature changes must be made only in
the nested packages under `NHCogs/` during this release.

- [`Honeypot`](Honeypot/README.md) detects and reviews suspicious activity,
  captures moderation evidence, and supports automated containment.
- [`NHMisc`](NHMisc/README.md) provides voice logging, sticky roles, activity
  statistics, and other server utilities.

## Installation

`[p]` means your bot prefix.

```ini
[p]load downloader
[p]repo add NHCogs https://github.com/Pxx500/NHCogs
```

For the consolidation release, install the combined suite and temporary
migrator. Keep the currently loaded legacy cogs active until the migration plan
is applied.

```ini
[p]cog update NHMisc Honeypot
[p]cog install NHCogs NHCogs NHCogsMigrator
[p]reload NHMisc Honeypot
[p]load NHCogsMigrator
```

Follow [`NHCogsMigrator/README.md`](NHCogsMigrator/README.md) for plan, apply,
restart verification, finalization, and recovery instructions.
