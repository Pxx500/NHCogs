# Operational errors

OperationalErrors is the process-wide private error reporter for the combined NHCogs
extension. It stores bounded failure summaries in SQLite and attempts one private alert
for every report.

## Setup

Load the combined `NHCogs` extension. The configured channel must be hidden from
`@everyone`. The bot needs View Channel, Send Messages, and Attach Files there. Every
command requires Manage Messages.

```ini
[p]nhcogs
[p]nhcogs errors
[p]nhcogs errors channel
[p]nhcogs errors channel set <channel>
[p]nhcogs errors channel clear
[p]nhcogs errors maintainer
[p]nhcogs errors maintainer set <member>
[p]nhcogs errors maintainer clear
```

Bare groups show their current private configuration and registered command paths. In a
channel visible to `@everyone`, they show the safe command catalog without reading or
displaying the protected configuration. The root shows its direct categories. Nested
groups show every leaf below them.

`channel set` selects the one process-wide private alert destination. `channel clear`
removes it. `maintainer set` selects the only member an alert may ping, and `maintainer
clear` disables the ping.

Alerts contain the source, action, bounded error summary, occurrence count, optional
Discord location, and a traceback attachment. If persistence or Discord delivery fails,
the reporting entry point logs the failure and returns without raising it to the caller.

## Stored data

Operational error records store the guild, source, action, bounded error summary,
exception type, first and last occurrence times, occurrence count, failure fingerprint,
recovery state, and optional channel, thread, and message IDs. Tracebacks are attached to
the private Discord alert and are not stored in SQLite.
