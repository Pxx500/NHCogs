# Custom Commands

Custom Commands stores server-owned text commands in SQLite. Regular members invoke them
with the normal bot prefix. The management group is `[p]customcom`, with `[p]cc` as an
alias.

## Inspect commands

These commands are available to regular guild members:

```ini
[p]customcom list
[p]customcom search <query>
[p]customcom show <name>
[p]customcom raw <name>
```

Lists and previews disable mentions. Running the custom command itself preserves normal
Discord mention behavior. `raw` shows exact whitespace in code blocks with Previous and
Next buttons. Responses containing a code fence are provided in one exact `.txt`
transcript instead.

## Create and edit

These commands require Red moderator status or Manage Messages:

```ini
[p]customcom create <name>
[p]customcom create simple <name> [initial response]
[p]customcom create random <name>
[p]customcom edit <name> [replacement response]
```

The bot opens a public thread attached to the moderator's message. Only that moderator
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
Red's normal command feedback. Unexpected failures are also reported through NHMisc's
configured error destination and maintainer ping.

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

## One-time migration from Red CustomCom

The migration commands are hidden and require Manage Messages. Configure a private
`[p]nhmisc errors` channel first, then run:

```ini
[p]nhcustomcom migrate plan
[p]nhcustomcom migrate apply confirm
```

Review the plan, attached backup, validation report, counts, and both SHA-256 digests
before applying. Apply unloads the official cog before the final source snapshot,
imports the complete validated catalog, verifies it, removes `customcom` from Red's
persisted package list, and activates the replacement.

If the plan includes data from a guild the bot has left, remove that complete legacy
guild scope with:

```ini
[p]nhcustomcom migrate forgetguild <guild_id> confirm
```

This permanently deletes every legacy CustomCom command for that guild. It only works
after a plan exists, rejects guilds the bot is still connected to, and invalidates the
current plan. Run `[p]nhcustomcom migrate plan` again before applying.

The durable phases are `planned`, `imported_not_active`, and `complete`. A validation or
import failure restores the official cog without activating the replacement. A cutover
failure keeps the verified import and restores the official cog where possible. Run the
same apply command again to retry. After `complete`, restart or reload `NHCogs` and
verify that `[p]customcom list` is owned by the replacement. The original Red Config and
local migration artifacts remain as inactive recovery data and participate in user-data
redaction.

Take a full bot backup immediately before apply. There is no partial post-cutover restore
command. If a rollback is required after `complete`, stop Red, restore that full backup,
and start Red again. Never load the official and replacement Custom Commands handlers at
the same time.
