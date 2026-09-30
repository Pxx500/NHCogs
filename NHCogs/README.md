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
| `[p]nhcogs errors` | Show the shared error configuration and its commands |
| `[p]nhcogs errors channel [channel|clear]` | Show, set, or clear the private error channel |
| `[p]nhcogs errors maintainer [member|clear]` | Show, set, or clear the error maintainer |
| `[p]nhcogs errors list` | List active operational failures |

Showing or changing either value requires a private invocation. A public channel does not
reveal the current value. The configured error channel must be hidden
from `@everyone`, and the bot needs View Channel, Send Messages, and Attach Files there. The
maintainer setting controls the only mention target for new technical failures.

`[p]nhcogs errors` shows the alert destination. `[p]nhcogs errors list` shows the active
failures themselves, including Honeypot detection failures and failures stored by the
other cogs. Failure text is shown only in a private moderator channel. A public channel
shows the count.

Technical failures are stored with their retry and recovery state. Expected command,
permission, validation, and normal operational outcomes are not reported as errors.

The shared commands replace the former per-cog error commands. Old error-channel and
maintainer settings are not used as a fallback. Configure the shared destination and
maintainer with the commands above.
