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

Install either or both cogs:

```ini
[p]cog install NHCogs Honeypot
[p]cog install NHCogs NHMisc
[p]load Honeypot
[p]load NHMisc
```

Each cog keeps its own requirements, metadata, documentation, and end-user data
statement in its directory.
