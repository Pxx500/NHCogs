# NHCogs

The combined extension provides the cogs in this repository and one shared technical
error configuration.

`[p]` means your bot prefix.

## Shared commands

All commands below are moderator-only and require Manage Messages. Overview commands can
show safe syntax in a public channel, but current values are shown only in a private
moderator channel. The shared error configuration starts unset.

| Command | Description |
|---|---|
| `[p]nhcogs` | Show the shared command overview |
| `[p]nhcogs errors` | Show the shared error configuration |
| `[p]nhcogs errors channel [channel]` | Show the configured private error channel, or change it when a channel is provided |
| `[p]nhcogs errors channel clear` | Clear the private error channel |
| `[p]nhcogs errors maintainer [member]` | Show the configured error maintainer, or change it when a member is provided |
| `[p]nhcogs errors maintainer clear` | Clear the error maintainer |

Commands that change or clear settings require a private invocation. With the optional
`[channel]` or `[member]` argument omitted, the command shows the current value. Providing
an argument changes the setting. The configured error channel must be hidden from
`@everyone`, and the bot needs View Channel, Send Messages, and Attach Files there. The
maintainer setting controls the only mention target for new technical failures.

Technical failures are stored with their retry and recovery state. Expected command,
permission, validation, and normal operational outcomes are not reported as errors.

The shared commands replace the former per-cog error commands. Old error-channel and
maintainer settings are not used as a fallback. Configure the shared destination and
maintainer with the commands above.
