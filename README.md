# NHCogs

Red-DiscordBot V3 cogs maintained for the NewHorizons Discord server.

## Available cogs

The combined `NHCogs` extension loads the maintained cogs:

- [`OperationalErrors`](NHCogs/operationalerrors/README.md) provides one private
  process-wide error channel and maintainer.
- [`Honeypot`](NHCogs/honeypot/README.md) detects and reviews suspicious activity,
  captures moderation evidence, and supports automated containment.
- [`NHMisc`](NHCogs/nhmisc/README.md) provides voice logging, sticky roles, activity
  statistics, and other server utilities.
- [`CustomCommands`](NHCogs/custom_commands/README.md) provides weighted text commands,
  Red-compatible placeholders, and thread-based moderator editing.
- [`GitHubTickets`](NHCogs/githubtickets/README.md) publishes pull request review tickets,
  manages developer expertise profiles, and routes reviewer requests.

## Installation

`[p]` means your bot prefix.

```ini
[p]load downloader
[p]repo add NHCogs https://github.com/Pxx500/NHCogs
```

```ini
[p]cog install NHCogs NHCogs
[p]load NHCogs
```
