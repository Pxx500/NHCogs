# NHCogs

The combined extension provides the cogs in this repository and one shared technical
error configuration.

`[p]` means your bot prefix.

## Shared commands

All commands below are moderator-only and require Manage Messages. Overview commands can
show safe syntax in a public channel, but current values are shown only in a private
moderator channel. The shared error configuration starts unset. Shared prefix-group
rules are in [Command trees](../docs/command-trees.md).

| Command | Description |
|---|---|
| `[p]nhcogs` | Show the shared command overview |
| `[p]nhcogs errors` | Show the shared error configuration |
| `[p]nhcogs errors channel [channel|clear]` | Show, set, or clear the private error channel |
| `[p]nhcogs errors maintainer [member|clear]` | Show, set, or clear the error maintainer |
| `[p]nhcogs lag [on|off]` | Measure event-loop lag for up to 48 hours |

Showing or changing either value requires a private invocation. A public channel does not
reveal the current value. The configured error channel must be hidden
from `@everyone`, and the bot needs View Channel, Send Messages, and Attach Files there. The
maintainer setting controls the only mention target for new technical failures.

Technical failures are stored with their retry and recovery state. Expected command,
permission, validation, and normal operational outcomes are not reported as errors.

The shared commands replace the former per-cog error commands. Old error-channel and
maintainer settings are not used as a fallback. Configure the shared destination and
maintainer with the commands above.

## Loop lag

`[p]nhcogs lag` is a temporary check for event-loop blocking. It stays off until a
moderator runs it in a private channel. `on` measures for 48 hours and then stops.
`off` stops it sooner. A restart during that window keeps measuring.

The check times each event-loop callback and task step. It does not enable asyncio
debug mode, which would capture a stack on every scheduled call. A callback longer
than 100 ms is written to the process log under `red.NHCogs.loop_lag`. Joinwatch
sizes are logged once when the window starts. After cutover those counts are the
SQLite rows. Nothing is posted to the error channel.

`[p]nhcogs lag` with no argument replies with the slowest callbacks seen in this
process.

A size line looks like:

`Loop lag sizes: guild 123 verified_members=10 pending_roles=2 pending_role_assignments=1 join_history_observations=30`

A slow callback looks like:

`Loop lag: Detection.on_message cog=Honeypot task=discord.py: on_message took 184 ms`
