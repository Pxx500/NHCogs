# Custom Commands

Custom Commands stores server-owned text commands in SQLite. Regular members invoke them
with the normal bot prefix. The management group is `[p]customcom`, with `[p]cc` as an
alias.

## Inspect commands

These commands are available to regular guild members:

```ini
[p]commands
[p]customcom list
[p]customcom search <query>
[p]customcom show <name>
[p]customcom raw <name>
```

`[p]commands` is a standalone public command with its own public permission surface. It
shows the same output as `[p]customcom list`. The list and search results contain only
commands the caller can use in the channel where they requested the list. A command
restricted to another channel does not appear there. Direct `show` and `raw` requests
for inaccessible commands use the normal not-found message.
Lists and previews disable mentions. `list` and `search` show 15 commands per page. Each
entry uses one logical line in the form `[p]command - short preview`, with whitespace and
markdown normalized for compact display. The invoker navigates with Previous and Next
and closes the list with the red X button. Temporary list, raw, and delete controls expire
after 30 seconds.

A moderator can set each command's access from the **Access** button in its create or
edit thread. The modal has multi-select menus for users, roles, and channels or threads,
plus Off/On choices for **Private channels only** and **Hide response preview**. Each
menu supports up to 25 selections. Existing selections are preselected when reopened.
Selected users and roles are alternatives: a matching user **or** role may use the
command. A channel restriction and **Private channels only** must also match when set.
A long access list is summarized on the dashboard. Each type is limited to 25 entries.
A private channel is hidden from `@everyone`, and private threads qualify. Channel IDs
match the exact channel or thread where the command is invoked.

All access fields are optional. Commands without restrictions remain available to
everyone, including commands created before access controls were added. **Hide response
preview** defaults to `Off`. Turning it `On` displays `[redacted]` in command lists for
people who can use that command. It does not hide the command's name or change direct
`show` and `raw` behavior for people who can use it. People without access do not see
the command in lists at all. Restricted invocations without access are silently ignored
and do not consume cooldowns or record usage.

Running the custom command itself preserves normal Discord mention behavior. `raw` shows
exact whitespace in code blocks with Previous and Next buttons. Responses containing a
code fence are provided in one exact `.txt` transcript instead.

## Usage chart

`[p]comchart` requires Manage Messages and shows help when called without arguments.
It uses the same ranked bars and donut chart as `chatchart`, with the top 10 custom
commands, counts, percentages, and an Other slice for the remaining commands.

```ini
[p]comchart <days>
[p]comchart <channel_or_thread> <days>
[p]comchart server <days>
```

Without a target, the chart covers the current channel. A channel or thread accepts
a mention or raw ID and must be visible to the caller. `server` covers the whole
guild and must be used in a private moderator channel. Reports from private channels
also require a private destination.

For example, `[p]comchart 7` shows this channel and `[p]comchart server 30` shows
the server. Days are UTC calendar days including today, not rolling 24-hour periods.
Collection starts when this version is deployed. Earlier usage isn't backfilled.

Only successfully sent responses count. Rejected invocations and failed responses
don't count. Random variants share their command's counter. Daily totals live in the
existing Custom Commands database without message content or user IDs. Deleting a
command keeps its historical totals. Reusing its name continues the same ranking entry.

## Create and edit

These commands require Manage Messages:

```ini
[p]customcom create <name>
[p]customcom create simple <name> [initial response]
[p]customcom create random <name>
[p]customcom edit <name> [replacement response]
```

Create and edit work in any channel where the moderator can run them. The bot opens a
public thread attached to the moderator's message. Only that moderator
can change the draft or use its controls. The same editor is used for creation and
editing. It shows five responses per page and provides a response selector plus Previous,
Next, Add, Edit, Delete, Weight, Move, View exact, Save, and Cancel controls. View exact
uses a code block or an exact `.txt` attachment when the response contains a code fence.

Messages still add responses with weight `100`. The existing text controls remain
available for faster editing:

Thread controls:

```ini
weight <response number> <1-1000>
remove <response number>
replace <response number>
move <response number> <new position>
```

Save validates and writes the complete draft in one transaction. Cancel and the
30-minute timeout make no changes. Completed threads are locked and archived.

Missing arguments, invalid values, cooldowns, and other expected command failures use
Red's normal command feedback. Unexpected failures are also reported through the shared
`[p]nhcogs errors` configuration and its maintainer ping.

## Cooldowns and deletion

```ini
[p]customcom cooldown <name>
[p]customcom cooldown <name> <seconds> [member|channel|guild]
[p]customcom delete <name>
```

A zero or negative cooldown removes that scope. Delete shows the command name and
response count before asking for confirmation. Both operations reject stale changes.

## Responses and arguments

Each response has a stable ID, display order, and integer weight from `1` through
`1000`. Weights are proportional and do not need to total `100`. New and imported
responses use weight `100` by default.

Placeholders, positional arguments, converters, `query`, public object attributes, and
the ten-argument limit follow Red's CustomCom behavior. Every response in one command
must use the same argument signature.

## Stored data

The cog stores guild IDs, lowercase command names, response content, weights, response
order and IDs, cooldown settings, revisions, author and editor IDs and names, and create
or edit timestamps. Discord user-data deletion replaces matching author and editor
identity with `Deleted User` while preserving the command content.

Cooldown deadlines are kept only in memory and reset when the cog or bot restarts.

## Permanent replacement and legacy cleanup

Loading `NHCogs` permanently removes the official `customcom` package from Red's autoload,
unloads the official extension when present, and activates this replacement from the
existing SQLite catalog. `NHCogs` also persists its own package name so a later restart
retries the intended state. An unknown cog that owns the same names stops the complete
`NHCogs` load and is never removed automatically.

After updating or reloading `NHCogs`, verify the replacement and inspect the temporary
legacy cleanup plan:

```ini
[p]customcom list
[p]customcom purgelegacy
```

`purgelegacy` is hidden, guild-only, and requires Manage Messages. Its first form is
read-only. It reports the active SQLite command count, old Red Config commands, local
migration artifact files, and the migration-state table. Review those values before
running the destructive form:

```ini
[p]customcom purgelegacy confirm
```

The confirmed command clears only the inactive legacy Red Config, the local
`CustomCommands/migration/` directory, and `custom_command_migration_state`. It does not
delete active commands, responses, cooldowns, or editor records. Run the read-only form
again and require zero legacy commands, zero artifact files, and an absent migration-state
table before removing the temporary cleanup code.

Migration backups previously uploaded as Discord attachments cannot be deleted
automatically because their message IDs were not stored. Delete those messages manually
if the remote copies should also be removed.
