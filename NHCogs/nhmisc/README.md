# NHMisc

`NHMisc` is a Red-DiscordBot cog with small moderation and server-utility tools.

`[p]` means your bot prefix. If your bot prefix is `!`, then `[p]nhmisc log`
is typed as `!nhmisc log`.

## Installation

```ini
[p]repo add NHCogs https://github.com/Pxx500/NHCogs
[p]cog install NHCogs NHMisc
[p]load NHMisc
```

## Forum Autopin

NHMisc can automatically pin the starter message of each new post in selected Discord
forum channels.

```ini
[p]nhmisc forumautopin add #forum
[p]nhmisc forumautopin remove #forum
[p]nhmisc forumautopin list
```

The commands require Manage Server permission or bot-admin status. Multiple forums can
be enabled in the same server.

The bot needs these permissions in each configured forum:

- View Channel
- Read Message History
- Pin Messages

Only posts created after the forum is enabled and while the bot is online are processed.
NHMisc does not scan old posts or backfill posts created while offline. Removing a forum
does not unpin starter messages that were pinned earlier.

If one of these permissions is revoked later, autopinning stops silently on Discord's
side. NHMisc reports it once per forum in the maintenance channel configured with
`[p]nhmisc log maintenance`, and reports it again only after a later pin succeeds
and the problem reappears. Deleting a configured forum removes it from the configuration
and is also reported in the maintenance channel.

## Voice Logging

Voice logging sends messages when users join, leave, or move between voice channels.
Move logs are sent immediately. If Discord audit logs later show that a moderator moved
the member, the bot edits the move log and adds the moderator name and user ID.

```ini
[p]nhmisc log voice #voice-logs
```

Sets the text channel used for voice join, leave, and move logs.

```ini
[p]nhmisc log alert #alerts
```

Sets the alert channel used by higher-priority alerts, such as voice-channel jumping.

```ini
[p]nhmisc log maintenance #bot-maintenance
```

Sets the private channel used for operational messages, including achievement syncs and
backups, sticky-role maintenance, and forum-autopin failures. The bot needs View Channel,
Send Messages, and Attach Files in this channel.

```ini
[p]nhmisc log moderation #moderator-actions
```

Sets the private channel used for non-pinging audit logs of moderator achievement and
Gate actions.

```ini
[p]nhmisc vcjumping visits 3
```

Sets how many voice-channel entries trigger a VC jumping alert. Entries do not need to be
different channels; entering the same channel repeatedly still counts.

```ini
[p]nhmisc vcjumping seconds 30
```

Sets the VC jumping detection window in seconds.

Running `[p]nhmisc log` without a log type shows all four configured destinations.
Running one of its child commands without a channel shows that destination without changing it.
VC jumping configuration is shown by `[p]nhmisc vcjumping`.

Defaults:

- VC jumping entries: `3`
- VC jumping window: `30` seconds
- Maintenance channel: not configured
- Moderator action channel: not configured

The maintenance and moderator action channels do not inherit the alert channel. Configure
each one explicitly after installing or updating NHMisc.

## Bot Proxy

Bot Proxy lets moderators prepare text in a private workflow and publish it through the
normal bot identity or a one-message webhook character.

```ini
[p]botproxy
[p]botproxy create
[p]botproxy channel
[p]botproxy channel #moderator-channel
[p]botproxy channel clear
[p]botproxy deleteclosed [true|false]
```

`[p]botproxy` shows the available operations. `create` deliberately opens an additional
empty session and must be run in the configured Bot Proxy channel. Moderators need
Manage Messages to use Bot Proxy. The `channel` command shows, sets, or clears the
private workflow channel. In that channel, the bot account needs View Channel, Send
Messages, Create Public Threads, Send Messages in Threads, Manage Threads, Manage
Messages, and Manage Webhooks. `deleteclosed` shows or changes
whether closing a session deletes its
launcher message and thread instead of archiving them. It defaults to disabled. Run
channel configuration only from a private moderator channel. Bot
Proxy rechecks the moderator's Manage Messages permission when controls and mutations
are used, not only when the workflow is opened.

The message Apps action is the fast path:

```text
Apps → Bot Proxy
```

With no active session, Apps creates one under the configured private channel. With one
session, it updates that session's reply destination. With several sessions, it shows
an ephemeral paginated selector. Apps never opens the workflow on the public source
channel and never creates an additional session implicitly.

`[p]botproxy` also explains the workflow buttons before a session is created. Each
workflow repeats the instructions through `Help` and shows a Help hint in its dashboard.

Each workflow has `Set destination`, `Set content`, `Identity`, `Help`,
`Persistent Messaging`, `Send Confirmation`, `Send`, and `Cancel`. A channel mention
or raw channel ID selects a standalone
destination. A same-server message link selects a native reply. Draft text is limited
to 2000 characters. Messages consumed as workflow input are deleted after being read.
Successful input also removes its ephemeral prompt, while validation errors reuse that
prompt. Only explicit
user mentions can notify. Role mentions, `@everyone`, `@here`, and reply-author pings
are suppressed.

`Send` first freezes the draft and posts an exact non-notifying preview in the session
thread. `Confirm sending` publishes that frozen draft, `Go back` returns to editing,
and `Cancel` closes the session. Successful publication keeps the session open and
resets the draft. `Persistent Messaging` is available for standalone channel
destinations. When enabled, successful publication preserves the destination and
identity while clearing only the content.

`Send Confirmation` defaults to enabled. Disabling it makes `Send now` publish without
a preview. Entering valid content also publishes immediately when the rest of the draft
is ready. Combined with `Persistent Messaging`, this provides a rapid-fire mode where
each submitted content message is published and then cleared.

Bot identity supports standalone messages and native replies. Character identity uses
an owned webhook with a one-time public name and optional avatar. Webhooks do not
support native replies, so a character cannot use a reply destination. Existing
threads and forum posts are supported, but Bot Proxy does not create a forum post.

Character presets are shared by the guild. The Identity panel can select, create, edit,
delete, and page through presets. Avatars can come from a validated HTTPS image or one
image attachment in the workflow thread. NHMisc copies validated PNG, JPEG, GIF, or
WebP data up to 2 MiB.

Using Apps on a message previously sent by Bot Proxy offers `Use as destination`,
`Edit`, and `Delete`. Edit changes content only. Delete requires confirmation. Both
actions recheck Manage Messages in the destination and write a private moderation log.

Only the moderator who opened a session can control it. Sessions time out after 10
minutes of inactivity. Cancelled, timed-out, reloaded, and interrupted sessions lose
their controls. They are archived and locked by default. When `deleteclosed` is enabled,
Bot Proxy explicitly deletes both the thread and its launcher message.

## Gatecount

```ini
[p]gatecount
```

Shows how many members completed Singleplayer and the member count for every linear
Gate tier from 1 through 6 using the role analytics database. Each member is counted
only in their highest Gate tier, and tiers with no members are still displayed. Each
count is labeled with a non-pinging role mention. This command can be used by any
server member.

## Gate Increment

```ini
Apps → Increment Gate roles
```

Use the completion message's Apps menu to review a one-tier Gate increment for its
author and explicitly mentioned members. The review supports up to 25 eligible users
and lets the moderator remove accidental mentions before confirming. When exactly one
user remains selected, the moderator can grant `Solo Gater` in the same role update.
Gate 6 users remain visible but cannot be selected.

The action requires Manage Messages and uses a durable one-use source lock. A second
message cannot reserve the same member's next Gate while an earlier increment is still
pending. Successful users are publicly pinged beside a non-pinging mention of their
new Gate role. Manual Gate role changes are reverted; Gate progress must be changed
through the bot.

## Gate Revoke

```ini
/gaterevoke user:<member>
Apps → Revoke Gate
```

Use the slash command or a member's Apps menu to choose any of that member's stored
Gates. Both entry points require Manage Messages and open the same ephemeral review,
showing every Gate and its proof before anything changes. Removing a non-latest Gate can
either compact every remaining Gate number or leave a temporary gap. The Discord Gate
role always follows the number of remaining Gates; `Solo Gater` and unrelated roles are
left unchanged. Successful reviews disappear silently, while a non-pinging audit is sent
to the configured private moderator action channel.

The original completion message remains marked as already processed. If a Gate was
revoked by mistake, restore it from a new correction message through the normal Gate
increment action.

## Add Gate Proof

```ini
Apps → Add Gate Proof
```

Use a historical completion message's Apps menu to attach it as proof for Gate records
that were imported without one. The action requires Manage Messages and opens an
ephemeral review for the message author and mentioned members. Each user defaults to
`Don't add proof` and can independently select one of their completed Gate ordinals that
still has no proof. Four users are shown per page, with no total candidate limit imposed
by the action.

Confirmation changes only the selected proof-message references. It does not increment
Gate progress or change any roles. The interactive picker only offers Gates without a
proof, and the same source message may be attached to any number of users even when it
was previously used for a normal Gate increment.

The same action detects a strict batch list:

```text
1 https://discord.com/channels/<server>/<channel>/<message>
2 https://discord.com/channels/<server>/<channel>/<message> <@user> user_id
```

Every line must contain an existing Gate number, one space, and a message link from the
current server. Optional targets after the link may be user mentions or bare user IDs;
mentions may be adjacent and extra horizontal whitespace is ignored. Several targets on
one line attach the same proof to that Gate ordinal for every listed member. A line with
no targets belongs to the message author. Targeted members must be current non-bot server
members, but do not need to be online or cached.

Batch mode clearly marks the review as proof-only: it never adds or increments a Gate.
When a listed Gate already has a proof, the moderator can replace every listed proof,
attach only missing proofs, or cancel. The entire multi-user operation is atomic, and an
invalid target, missing Gate, duplicate user-and-ordinal pair, changed proof, or stale
source message prevents every write.

## Achievements

```ini
/achievements [user]
Apps → View achievements
Apps → Grant achievements
[p]achievement create <display name>
[p]achievement list
[p]achievement missingproofs
[p]achievement rename <key> <new display name>
[p]achievement delete <key>
[p]achievement role bind @Role
[p]achievement role unbind @Role
[p]achievement role replace @OldRole @NewRole
[p]achievement role list
[p]achievement revoke <users...>
```

`/achievements` and the user Apps action show the same member profile ephemerally, with
the same `Send publicly` button and published attribution. Both are available to every
server member.

The message Apps grant action uses the message author and mentions as its candidate
list, then lets a moderator select up to 25 recipients and one or more achievements.
The revoke command finds only revocable achievements shared by every selected user and
opens a confirmation review. Grant and revoke actions require Manage Messages.

Achievement creation and role commands also require Manage Messages. Achievements do
not need Discord roles. `role bind` uses the mentioned role ID, then lets the moderator
choose an unbound achievement from a dropdown. Binding imports current role holders
without proof. Unbinding stops role tracking without removing the role or achievement
history. Replacing a binding opens a review with two choices: move current holders from
the old role to the new role, or keep the old role and add the new role. Both choices keep
existing achievement history and import current holders of the new role.
Deleting a bound Discord role automatically stops tracking it without deleting awards.
`achievement rename` changes only the display name; the key and existing awards stay
unchanged. `achievement delete` (or `achievement del`) requires an unbound, non-system
achievement and opens a destructive confirmation. Confirming permanently deletes the
achievement definition and every stored award for it. `achievement list` shows the
stable keys required by these commands. Because keys are internal identifiers, `list`,
`rename`, and `delete` are unavailable in channels visible to `@everyone`.

`achievement missingproofs` reports current non-bot server members who have at least one
Gate without a proof link. It previews the first 20 affected members and attaches the full
result as CSV, or as a ZIP when needed. The command requires a complete member cache and
is unavailable in channels visible to `@everyone`.

Gate increments, proof attachments and revokes, achievement grants and revokes,
achievement definition changes, and role binding changes are recorded in the configured
moderator action channel. Operational failures and partial results are sent to the
maintenance channel.

## Tier Distribution

```ini
[p]tierdistribution
```

Shows the current player-role distribution from Stone through UXV, plus the
number of players with a configured Gate role. Each player contributes only to
their highest progression tier and at most once to Gate. Percentages use the
total of those displayed progression and Gate counts. The Singleplayer-completed
role alone does not count as Gate membership. This command can be used by any server
member.

## Sticky Roles

Sticky roles remember selected roles when a member leaves and restore them when the
member rejoins. Roles are stored by Discord role ID, so role name changes do not matter.

```ini
[p]nhmisc stickyroles add @Role
[p]nhmisc stickyroles add 123456789012345678
```

Marks a role as sticky. The role must exist on the server and the bot must be able to
assign it.

```ini
[p]nhmisc stickyroles remove @Role
[p]nhmisc stickyroles remove 123456789012345678
```

Removes a role from sticky-role tracking. Removing a sticky role also removes that role
from all saved sticky-role snapshots. If the role exists in the sticky role database,
the bot first asks whether to `remove`, `keep`, or `change <role mention or ID>`.

```ini
[p]nhmisc stickyroles list
```

Lists sticky roles configured for the server.

```ini
[p]nhmisc stickyroles scan
```

Scans the sticky role database for entries that need review: missing Discord roles and
saved user-role rows that are no longer configured as sticky. Choices are `remove`,
`keep`, or `change <role mention or ID>`.

```ini
[p]nhmisc stickyroles debuglogging toggle true
[p]nhmisc stickyroles debuglogging toggle false
```

Enables or disables sticky-role debug logs. When enabled, the bot logs sticky-role
snapshot writes on member leave and snapshot reads/restores on member join. Debug logs
use the configured NHMisc maintenance channel. Deleted-role prompts always use that same
maintenance channel, even when debug logging is disabled.

## Activity Analytics

Activity analytics passively counts normal user messages from Discord gateway events.
The cog does not fetch message history and does not make Discord API requests per
message.

Ignored messages:

- direct messages;
- bot messages;
- webhook messages;
- system messages.

The bot stores counters in a local SQLite database in the cog data directory. Current-day
details are collapsed into daily summaries after the UTC day ends. Detailed user/channel
rows are retained only for the configured retention period.

### Daily Summary Channel

```ini
[p]nhmisc activity channel #activity-reports
```

Sets the channel where the bot posts automatic daily activity summaries. Summaries are
closed on UTC day boundaries. If the bot was offline at midnight, it closes stale days on
startup or on the next relevant activity command/message.

Daily summaries include:

- total messages;
- active users;
- active-user percentage of server members;
- users with at least 10, 50, and 100 messages;
- number of active channels;
- average messages per active user;
- peak hour shown as a Discord-localized timestamp;
- top 5 channels as Discord channel mentions.

### Activity Commands

```ini
[p]nhmisc activity current
```

Shows a preview of the current UTC day. This does not close the day and does not write a
final history row.

```ini
[p]nhmisc activity latest
```

Shows the latest retained closed daily summary.

```ini
[p]nhmisc activity timeline 7
```

Shows a compact table for the last closed days. You can pass any positive day count, for
example:

```ini
[p]nhmisc activity timeline 30
```

If a retained row does not exist for a day, the table shows `n/d`. If data exists and the
value is zero, it shows `0`.

```ini
[p]nhmisc activity channelstats #channel 30
```

Shows day-by-day message activity for one channel. Current and detail-retained days use
per-user/channel detail rows. Closed days use daily channel summaries retained by
history retention. Days with no retained data show `n/d`; retained days with no messages
show `0`.

```ini
[p]nhmisc activity retention 31
```

Sets how many days to keep detailed user/channel/thread rows. These rows power
`usermodstats`, `selfchart`, and `chatchart`.

If lowering retention would delete existing rows, the bot first reports how many rows
will be deleted and requires the same moderator to reply exactly:

```text
I understand
```

```ini
[p]nhmisc activity historyretention -1
```

Sets how long daily summary history is retained:

- `-1`: keep daily summary history indefinitely;
- `0`: send the daily summary, then delete that day's aggregate history;
- any positive number: keep that many days of daily summary history.

Reducing history retention also asks for `I understand` when it would permanently delete
existing summary rows.

```ini
[p]nhmisc activity verify
```

Checks today's open activity aggregates for internal consistency. This compares the
canonical per-user/channel/thread rows with the faster per-user and per-channel cache
rows.

```ini
[p]nhmisc activity dbsize
```

Shows the activity SQLite file size in bytes and MiB, SQLite page usage, and row counts
for the main activity tables.

### Moderator Tools

```ini
[p]nhmisc usermodstats @User 7
```

Shows moderator-only message stats for one user.

You can also use a raw Discord user ID:

```ini
[p]nhmisc usermodstats 123456789012345678 30
```

Pass any positive number of days. The requested range is capped to the configured detail
retention.

The output includes total messages, active days, average messages per active day, top
channels, and a daily breakdown. Missing retained data is shown as `n/d`; real zeroes are
shown as `0`.

```ini
[p]nhmisc usermodstats channel @User #channel 30
```

Shows the user's activity in one channel. When a parent text channel is provided, the
command includes direct messages in that channel and messages in all threads under it.
When a thread is provided, it only shows that thread. The output includes total messages,
active days, average messages per active day, and a daily breakdown.

```ini
[p]nhmisc usermodstats channels @User 30
```

Shows how the user's activity is distributed across channels and threads. Threads are
shown as separate locations. The output includes total messages, active days, locations
used, top locations in the range, and the dominant location for each retained day.

```ini
[p]chatchart <days> [amount]
[p]chatchart <channel_or_thread> <days> [amount]
```

Running `[p]chatchart` without arguments shows the command help and both accepted
forms. The optional channel or thread target accepts a mention or raw ID. When no
target is provided, the command charts the channel or thread where it is used. An
explicit target must be visible to the moderator. The result is posted where the
command was invoked. `amount` controls how many of the most active users are shown,
defaults to `10`, and accepts values from `1` through `20`. Each displayed user keeps
the same distinct colour in the horizontal ranking and donut, while `Other` contains
everyone outside the selected ranking. Choosing `1` adds `One is a bit low, no? 🤨`
to the chart message. The requested day count is capped to the configured detail
retention. The chart includes message counts and overall percentages for each shown
user.

```ini
[p]nhmisc topyapper 30 10
```

Shows the users who sent the most messages across the server in the retained date range.
The result count must be between 1 and 20.

### User Command

```ini
[p]selfchart
```

Shows the caller's own simplified activity for the last 7 retained days:

- total messages;
- messages for each day;
- top 1 channel.

This command has no arguments and only shows the caller's own data.

## Role Analytics

Role analytics maintains a current SQLite mirror of guild members and their role IDs.
Run the initial synchronization once to opt the guild in:

```ini
[p]rolesync
```

The command reuses Red's complete member cache when it is available. If the cache is
incomplete, it requests the guild member list through the Discord gateway. After the
initial synchronization, member and role events keep the database current. The bot also
reconciles enabled guilds after startup, after a resumed gateway session, and once every
24 hours. A manual `rolesync` forces an immediate reconciliation.

After installing the achievement system, configure the NHMisc maintenance channel and
run:

```ini
[p]rolesync discord
```

The command prepares an import plan from the role-analytics snapshot, uploads a backup,
and posts both in the maintenance channel. The invoking moderator must type `confirm`
there before anything changes. This initializes achievement data from current Discord
roles. Later uses deliberately replace achievement progress with Discord's current role
state. In every normal sync, the achievement database has priority and Discord roles are
restored from it.

A reconciliation builds the replacement snapshot in a separate generation and swaps it in
atomically, so `rolestats` and `roleusers` keep answering from the previous snapshot for
the whole duration. If a reconciliation fails, the previous snapshot stays queryable and
a retry is scheduled with exponential backoff.

```ini
[p]rolestats 1348078496710135888 AND 1097204292198338692
[p]rolestats <@&1348078496710135888> OR NOT <@&1097204292198338692>
```

`rolestats` returns only the number of matching non-bot members. It may be used in
public channels. Role operands can be raw Discord role IDs or role mentions. Operators
are case-insensitive and use this precedence:

1. `NOT`
2. `AND`
3. `OR`

Parentheses can override the precedence, for example:

```ini
[p]rolestats (10 OR 20) AND NOT 30
```

```ini
[p]roleusers 1348078496710135888 AND 1097204292198338692
```

`roleusers` uses the same expression language and exports matching non-bot members as
UTF-8 CSV with `user_id`, `username`, and `display_name` columns. It sends a ZIP instead
when the CSV exceeds Discord's attachment limit. Usernames and display names are read
from the current Discord cache for the export and are not stored in the analytics
database. Names beginning with `=`, `+`, `-`, or `@` are prefixed with an apostrophe so
that spreadsheet applications treat them as text instead of evaluating them as formulas.

For privacy, `roleusers` only works when the invocation channel is not visible to the
guild's `@everyone` role and the bot can View Channel, Send Messages, and Attach Files
there. The export is sent to that channel. `rolesync` and `rolestats` may be used in
public channels.

All three top-level role analytics commands require the effective Manage Messages
permission in the invocation channel. Role references in responses never notify role
members.

Moderators with Manage Messages can disable role analytics and delete the guild's analytics data:

```ini
[p]nhmisc roleanalytics disable
```

## Permissions

Configuration commands require Manage Server or bot admin permissions.

Sticky-role commands require Manage Server or bot admin permissions.

Server-wide activity commands and moderator tools require Manage Messages, Manage
Server, or bot admin permissions.

Bot Proxy commands, Apps, character management, publication, edit, and delete require
Manage Messages. The permission is checked again in the destination. Character
publication additionally requires the bot to have Manage Webhooks in the destination's
parent channel.

`rolesync`, `rolestats`, and `roleusers` require Manage Messages in the invocation
channel. Only `roleusers` additionally requires a non-public channel and the bot channel
permissions described above.

`[p]selfchart` is available to regular guild users because it only returns the caller's
own activity.

## Operational errors

NHMisc and Custom Commands share one private operational error destination. Configure it
with:

```ini
[p]nhmisc errors
[p]nhmisc errors channel [channel]
[p]nhmisc errors channel clear
[p]nhmisc errors maintainer [member]
[p]nhmisc errors maintainer clear
```

The channel must be hidden from `@everyone`, and the bot needs View Channel, Send
Messages, and Attach Files there. Alerts include a short summary and a traceback file.
Only the configured maintainer can be pinged. If the alert itself cannot be sent, the
error remains in SQLite and the failure is written to the bot console.

## Stored Data

The cog stores Discord user IDs with passively collected message-count aggregates for
configurable short-term activity detail retention. Closed daily summary history stores
aggregate counts and channel IDs, but not user IDs.

The detailed activity row is keyed by UTC date, user ID, parent channel ID, thread ID,
and message count. A user can have multiple rows for the same day when they post in
multiple channels or threads. Activity analytics does not store message content,
message IDs, jump URLs, attachment URLs, embeds, or deleted/edited message state.

Bot Proxy stores guild, moderator, channel, thread, webhook, and published message IDs;
the original and current published text; the revision history for send, edit, and delete
transitions; reply source IDs; sender and character snapshots; and saved character
avatars in `bot_proxy.sqlite`. Active workflow
records contain only the IDs needed to disable and archive an interrupted session.
Unpublished draft content remains in memory and is not restored after a restart.

Sticky roles are stored in a local SQLite database as guild IDs, user IDs, and role IDs.

Role analytics stores guild IDs, current member user IDs, bot flags, and current role
IDs in a local SQLite database. It stores no usernames, display names, role-change
history, or message data. Members who leave and roles that are deleted are removed from
the current-state index. Disabling role analytics deletes the guild's analytics data.

Achievements store guild and user IDs, achievement keys, display names, optional bound
role IDs, timestamps, state, and optional proof-message IDs. Role synchronization uses
this data to keep bound achievement roles and Gate progression consistent. It does not
store message content or usernames. Deleting an achievement permanently removes its
definition and all associated award records.

The cleanup commands do not add an NHMisc database. They delegate to Honeypot,
which owns its 14-day Gateway-observed message registry and its privacy deletion.

Operational error records store the guild, source, action, bounded error summary,
exception type, first and last occurrence times, occurrence count, failure fingerprint,
recovery state, and optional channel, thread, and message IDs. Tracebacks are attached to
the private Discord alert and are not stored in SQLite.
