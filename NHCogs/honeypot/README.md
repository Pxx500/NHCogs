# Honeypot

Honeypot is a Red-DiscordBot cog that protects your server by creating trap channels for self-bots, scammers,
spam accounts, and suspicious users. Messages posted in honeypot channels are deleted, logged, optionally purged,
and either punished automatically or sent to moderators for review. It also alerts moderators when new accounts join the server.

This implementation is maintained by Pxx500 and was originally based on AAA3A's Honeypot.

## Installation

```ini
[p]repo add NHCogs https://github.com/Pxx500/NHCogs
[p]cog install NHCogs Honeypot
[p]load Honeypot
```

Requires `AAA3A_utils`. Red will show the pip install command if missing.

## Quick Setup

```ini
[p]honeypot channels honeypot create
[p]honeypot channels review #your-review-channel
[p]honeypot channels daily-stats #your-public-stats-channel
[p]honeypot honeypot action ban
[p]honeypot honeypot toggle true
```

## Commands

The `!honeypot` command and all subcommands require Manage Messages.

### Manual punishment

Moderators with Manage Messages can use the `Punish` message context action. It can save the message and its attachments to the private manual evidence channel, then deletes the source after the private audit is created. The audit is always written, even when evidence saving is disabled. The action can apply mute, kick, ban, or any configured Role n’t that covers the source channel. Mute uses Red's core `Mutes` cog, while kick and ban use Honeypot's existing moderation path.

All punishments start unselected and saving evidence starts enabled. Kick and ban exclude every other punishment. Mute and multiple Role n’t roles can be combined. The moderator enters one shared reason and, when needed, a mute duration. Mute durations use `30m`, `2h`, `3d`, or `1w` format and may not exceed 28 days.

| Command | Description |
|---|---|
| `!honeypot evidence status` | Show the private manual evidence configuration |
| `!honeypot evidence channel <channel>` | Set the private destination for manual evidence |
| `!honeypot punishment role-nt add <role> <channel> [channels...]` | Add source channels to a Role n’t punishment |
| `!honeypot punishment role-nt remove-channel <role> <channel> [channels...]` | Remove source channels from a Role n’t punishment |
| `!honeypot punishment role-nt notification <role> [channel]` | Show or set its notification channel |
| `!honeypot punishment role-nt notification-clear <role>` | Restore source-channel notifications |
| `!honeypot punishment role-nt remove <role>` | Remove a Role n’t punishment |
| `!honeypot punishment role-nt list` | List configured Role n’t punishments |

### honeypot

| Command | Description |
|---------|-------------|
| `!honeypot honeypot toggle <bool>` | Master switch for all message detection: honeypot channels, spam, firstpost and the image detector. Joinwatch and the bait role are unaffected |
| `!honeypot honeypot action <kick\|ban\|review\|none>` | Main action for suspicious posts |
| `!honeypot honeypot fallback_action <review\|kick\|ban\|none>` | Action for non-suspicious posts |
| `!honeypot honeypot dry_run <bool>` | Log what would happen without punishing |
| `!honeypot honeypot whitelist_mode <bypass\|review\|fallback\|none>` | How whitelisted roles behave |
| `!honeypot honeypot automated_kick_fail_warn <bool>` | Warn when the target has already left before the kick is applied |

### gifdetector

The GIF detector removes GIF uploads, direct GIF links, Discord GIF embeds, and supported Tenor/Giphy video transcodes in configured channels and their threads. Ordinary MP4 uploads and links remain allowed. It ignores bots, webhooks, and protected moderators; at most one horizontal ICBM animation runs per server while additional GIFs use the static warning. The ICBM movement completes in five seconds. The GIF remains for the configured retention period, which defaults to five seconds; bot warnings remain for at least five seconds and never expire before the GIF.

By default, three GIFs from one member inside a rolling 60-second window trigger a one-hour server mute through Red's core `Mutes` cog. Core `Mutes` must have a mute role configured; Honeypot never falls back to a native timeout, warning, kick, or ban. Burst counters are in memory and reset when the cog reloads.

| Command | Description |
|---|---|
| `!honeypot gifdetector toggle <true\|false>` | Enable or disable GIF interception |
| `!honeypot gifdetector animation <true\|false>` | Enable or disable the animated ICBM warning |
| `!honeypot gifdetector retention [0-60]` | Show or set how long detected GIFs remain visible |
| `!honeypot gifdetector threshold [2-20]` | Show or set the GIF count required for a mute |
| `!honeypot gifdetector window [5-3600]` | Show or set the rolling window in seconds |
| `!honeypot gifdetector muteduration [60-604800]` | Show or set the role mute duration in seconds |
| `!honeypot gifdetector channel add [channel]` | Monitor a channel, or the current channel when omitted |
| `!honeypot gifdetector channel remove [channel]` | Stop monitoring a channel, or the current channel when omitted |
| `!honeypot gifdetector channel list` | List monitored channels |
| `!honeypot gifdetector debug toggle <true\|false>` | Enable or disable moderator-only shot diagnostics |
| `!honeypot gifdetector debug channel [channel]` | Show or set the shot diagnostics destination |
| `!honeypot gifdetector message set <text>` | Set the static warning shown for additional GIFs |
| `!honeypot gifdetector message reset` | Reset the static warning to `No gifs!` |

### channels

`!honeypot channels` shows every destination and source/scope with its current value. Destination categories are independent. Setting one never changes another.

| Command | Description |
|---------|-------------|
| `!honeypot channels review [channel]` | Show or set the review destination |
| `!honeypot channels daily-stats [channel]` | Show or set the public daily statistics destination |
| `!honeypot channels manual-evidence [channel]` | Show or set the private manual evidence destination |
| `!honeypot channels joinwatch [channel]` | Show or set the JoinWatch destination |
| `!honeypot channels bait-role [channel]` | Show or set the bait-role destination |
| `!honeypot channels gif-debug [channel]` | Show or set the GIF diagnostics destination |
| `!honeypot channels honeypot create` | Create and add a new `#honeypot` channel at position 0 |
| `!honeypot channels honeypot add <channel>` | Add an existing honeypot source |
| `!honeypot channels honeypot remove <channel>` | Remove a honeypot source |
| `!honeypot channels honeypot list` | List honeypot sources |
| `!honeypot channels gif-detector add [channel]` | Add a GIF detector scope |
| `!honeypot channels gif-detector remove [channel]` | Remove a GIF detector scope |
| `!honeypot channels gif-detector list` | List GIF detector scopes |

### punishment

| Command | Description |
|---------|-------------|
| `!honeypot punishment mute_role <role>` | Temp mute role for users awaiting review |

### purge

| Command | Description |
|---------|-------------|
| `!honeypot purge backward <60-3600>` | Seconds of cached messages the purge step may delete |
| `!honeypot purge forward <0-300>` | Seconds of new messages purged after a trigger (`0` disables it) |

### firstpost

| Command | Description |
|---------|-------------|
| `!honeypot firstpost toggle <bool>` | Enable or disable suspicious first-post detection |
| `!honeypot firstpost warmup <bool>` | Record first observed senders without taking action |
| `!honeypot firstpost action <review\|kick\|ban\|none>` | Action for suspicious first observed messages |

### spam

| Command | Description |
|---------|-------------|
| `!honeypot spam toggle <bool>` | Enable or disable repeated message detection |
| `!honeypot spam action <review\|kick\|ban\|none>` | Action for repeated messages across channels |
| `!honeypot spam window <3-60>` | Seconds in the repeated message window |
| `!honeypot spam channels <2-10>` | Different channels required to trigger |

### review

| Command | Description |
|---------|-------------|
| `!honeypot review toggle <bool>` | Send suspicious messages to moderator review instead of acting immediately |
| `!honeypot review channel <channel>` | Channel for review requests |
| `!honeypot review kick_fail_warn <false\|true\|manual>` | How to handle a review kick when the target has already left |

Detection cases expire 24 hours after the first detection. This lifetime is fixed.

### roles

| Command | Description |
|---------|-------------|
| `!honeypot honeypot roles add <role>` | Add a whitelisted role |
| `!honeypot honeypot roles remove <role>` | Remove a whitelisted role |
| `!honeypot honeypot roles list` | List whitelisted roles |

### keywords

| Command | Description |
|---------|-------------|
| `!honeypot honeypot keywords add <keyword>` | Add a scam keyword |
| `!honeypot honeypot keywords remove <keyword>` | Remove a scam keyword |
| `!honeypot honeypot keywords list` | List scam keywords |
| `!honeypot honeypot keywords reset` | Reset to defaults |
| `!honeypot honeypot keywords attachments add <regex>` | Add filename-base regex (triggers at 2+ matches) |
| `!honeypot honeypot keywords attachments remove <regex>` | Remove a filename regex |
| `!honeypot honeypot keywords attachments list` | List filename regexes |
| `!honeypot honeypot keywords attachments reset` | Reset to default patterns |

### imagescan

| Command | Description |
|---------|-------------|
| `!honeypot imagescan add` | Add scam images from the message this command replies to |
| `!honeypot imagescan remove <identifier>` | Remove an image sample and its stored file from the active dataset |
| `!honeypot imagescan dropfile <identifier>` | Remove a stored image file while keeping its hashes active |
| `!honeypot imagescan rebuild` | Recompute image detector threshold state |
| `!honeypot imagescan status` | Show image detector settings, samples, and timing |
| `!honeypot imagescan detector toggle <bool>` | Enable or disable production image detection |
| `!honeypot imagescan detector action <none\|review\|kick\|ban>` | Action for image detector matches |
| `!honeypot imagescan detector threshold <0-100>` | Maximum image hash distance |

### joinwatch

| Command | Description |
|---------|-------------|
| `!honeypot joinwatch toggle <bool>` | Enable or disable the joinwatch module |
| `!honeypot joinwatch alert toggle <bool>` | Enable or disable joinwatch alert messages |
| `!honeypot joinwatch channel <channel>` | Channel for join alerts |
| `!honeypot joinwatch max_age <1-1000000>` | Max account age in hours to trigger alert |
| `!honeypot joinwatch autorole toggle <bool>` | Enable or disable automatic role assignment for young accounts |
| `!honeypot joinwatch autorole role <role>` | Role to apply to young accounts |
| `!honeypot joinwatch autorole timer <1-10080>` | Minutes before punishment if the role remains |
| `!honeypot joinwatch autorole action <none\|kick\|ban>` | Action when the auto-role is not removed in time |
| `!honeypot joinwatch autorole bantimers` | List active auto-role punishment timers |
| `!honeypot joinwatch autorole randomize toggle <bool>` | Enable or disable randomized delay before the auto-role is applied |
| `!honeypot joinwatch autorole randomize min_time <1-10080>` | Minimum minutes before applying the auto-role |
| `!honeypot joinwatch autorole randomize max_time <1-10080>` | Maximum minutes before applying the auto-role |

### bait_role

| Command | Description |
|---------|-------------|
| `!honeypot bait_role toggle <bool>` | Enable or disable the bait role trap |
| `!honeypot bait_role role <role>` | Set the bait role |
| `!honeypot bait_role action <kick\|ban>` | Action to take when users take the bait role |
| `!honeypot bait_role channel [channel]` | Show or set the bait-role destination |

### Operational errors

Technical failures from Honeypot use the shared `[p]nhcogs errors` configuration. See the
[shared command catalog](../README.md) for the setup commands and privacy rules. Expected
detection outcomes and normal command feedback aren't reported as operational errors.

### other

| Command | Description |
|---------|-------------|
| `!honeypot config all` | Show a compact summary of all configuration sections |
| `!honeypot config honeypot` | Show main honeypot detection settings |
| `!honeypot config channel` | Show honeypot and log channel settings |
| `!honeypot config punishment` | Show punishment settings |
| `!honeypot config purge` | Show purge settings |
| `!honeypot config firstpost` | Show firstpost settings |
| `!honeypot config imagescan` | Show image detector settings |
| `!honeypot config spam` | Show spam detection settings |
| `!honeypot config review` | Show review settings |
| `!honeypot config roles` | Show whitelist role settings |
| `!honeypot config keywords` | Show keyword and attachment-pattern counts |
| `!honeypot config joinwatch` | Show joinwatch and joinwatch auto-role settings |
| `!honeypot config bait_role` | Show bait role settings |
| `!honeypot config stats` | Show stored stats, detection-case operations, and pending timer counts |
| `!honeypot stats show` | Show public-facing stats |
| `!honeypot stats channel [channel]` | Show or set the public daily statistics destination |
| `!honeypot modstats` | Show detailed moderator statistics |
| `!honeypot doctor` | Check config, channels, and permissions |
## Action & Fallback Logic

```
suspicious + action = kick/ban  → instant punishment
suspicious + action = review    → review (if review channel is set), otherwise fallback
suspicious + action = none      → skip to fallback
non-suspicious                  → fallback_action decides

fallback_action = review   → moderator review
fallback_action = kick/ban → instant punishment
fallback_action = none     → log only
```

Dry-run is checked immediately before every punishment or role addition. Turning
it on while delayed or retried work is pending prevents those Discord side effects.

## Whitelist Modes

| Mode | Behavior |
|------|----------|
| `bypass` | Log and skip (no action) |
| `review` | Force review regardless of suspicion |
| `fallback` | Skip instant action, go through fallback logic |
| `none` | Treat as normal user |

## Detection

A message is considered suspicious if:

- Account is under 7 days old
- Content contains scam keywords (customizable, see `!honeypot honeypot keywords`)
- Has attachments and account is under 14 days old
- Has 4+ image attachments, regardless of filename
- Has 2+ generic attachment names (e.g. `image.jpeg`, `image(1).jpeg`, `1.jpeg`)
- Has 2+ attachments matching configured filename-base regexes
- An image matches the image-detector dataset, when the detector is enabled and
  samples exist (see `!honeypot imagescan`)

If firstpost is enabled, a user's first observed message is also considered
suspicious when it has exactly 4 attachments, or exactly 2 attachments with
configured scam keywords.
The keyword `bro` is treated as attachment-only: it does not trigger by itself,
but can satisfy the 2-attachment firstpost rule.
`firstpost warmup` and active firstpost detection are mutually exclusive:
enabling one disables the other.

If spam detection is enabled, a user's matching message fingerprint in multiple
different channels within the configured window is considered suspicious when
the message has attachments or configured scam keywords.

Default scam keywords: `free nitro`, `giveaway`, `steam gift`, `free discord`, `discord.gift`,
`claim your`, `you won`, `free vbucks`, `free robux`, `free coins`, `boost your server`,
`limited time`, `exclusive offer`, `free membership`, `hack`, `crack`, `generator`.

Default attachment patterns: `^image$` (matches `image.jpeg`), `^image ?\(\d+\)$`
(matches `image(1)`, `image (2)`) and `^\d+$` (matches `1.png`, `42.jpeg`).

## Review Flow

1. Attachment capture starts before the source message is deleted. A failed or
   timed-out download is shown as missing evidence and does not block containment.
2. The source message is deleted, along with recent cached messages from that user.
   Backward purge cannot be turned off; its window is 60-3600 seconds. Forward
   purge is disabled by setting `purge forward` to `0`.
3. The review mute role is applied while review is pending when configured.
4. One compact summary is posted in the review channel and a public case thread is
   created from it.
5. The thread receives a chronological copy of every detected message, its signals,
   deletion status, content, and copied attachment evidence.
6. Ban / Kick / Ignore and image-learning controls are available on the summary and
   on the relevant thread messages. Moderators can classify all images, one message,
   or individual images.
7. Resolution records the moderator and time, disables controls, releases owned
   containment roles, and locks and archives the thread.
8. Open cases, publications, and moderation operations survive bot restarts.
9. Unresolved cases expire after the fixed 24-hour lifetime.

Any configured honeypot channel uses the same flow. If a user with the review
mute role or joinwatch auto-role posts in any honeypot channel, the bot treats it
as repeat honeypot activity and forces a ban with the reason `Suspicious Activity`.

## Stats

`stats` shows a compact public-facing summary: messages, bans,
sent-for-review cases, early catches, auto-roles applied, and auto role
punishments.

When a daily statistics channel is configured, Honeypot publishes a separate
summary at `00:05 UTC` for the completed UTC day. The fixed delay normally puts
it after the general server activity report without creating a dependency on
NHMisc. An unset destination disables publication.

The public daily summary contains two compact sections:

- `Honeypot`: detections, automated bans, and manual bans
- `JoinWatch`: shadowbans and bans

Detections count newly persisted detected messages. Automated and manual bans
are separated at the action source and count only successful Discord bans.
JoinWatch shadowbans count successful role applications, while JoinWatch bans
count successful ban actions after the role timer. Failed actions, dry runs,
kicks, merely scheduled roles, and retry attempts that do not complete an
effect do not count. JoinWatch bans are not also included in automated bans.

Daily collection is forward-only from the version that introduces it. Existing
lifetime counters do not contain enough dates or action-source information for
a reliable historical backfill. An observed day without matching activity
publishes zeros, while an unobserved date from before deployment or a full
outage is skipped.

`modstats` is the detailed moderator view. `Total detections` counts every
non-exempt message caught in the honeypot channel. `Suspicious detections`
counts only detections matching suspicious-account, keyword, or attachment
rules. `Reviews sent` is a historical counter. `Active detection cases` is the
current number of pending or resolving cases read from SQLite. The operational
case counters also expose overdue cases, stale resolution leases, failed
containment, forbidden message deletes, and outstanding durable operations.

`Applied temporary mutes` and `Failed temporary mutes` are historical counters
for temporary review mutes. They do not mean those users are still muted.
The review mute role may be the same role as joinwatch auto-role. If that user
has an active joinwatch auto-role timer, review cleanup leaves the role in place
so it does not clear the joinwatch timer.

`Purged messages` counts extra recent messages removed by the purge step. It
does not include the original honeypot message, which is deleted separately as
part of every detection.

Cached purge stores recent message IDs observed through Discord Gateway events
for the configured `purge backward` window, then deletes those known messages
directly. After a purge trigger, the bot also forward-purges new messages from
that user for the configured `purge forward` window.

`Early catches` counts suspicious first observed messages handled by firstpost.
`Spam catches` counts repeated messages across channels handled by spam detection.

The `Joinwatch` stats section tracks non-bot joins while joinwatch is enabled.
`Young joins` counts accounts below the configured `joinwatch max_age`
threshold, and `Young join rate` is `Young joins / Total joins`. Auto-role
scheduled, clear, and punishment counters are historical. `Pending role applications`
is the current number of delayed role applications waiting to run, and
`Active auto-role timers` is the current number of users still waiting for staff
action or timeout after the role was applied.

## Config Dumps

Use `!honeypot config <section>` to inspect current settings without exposing raw
IDs or message contents. Config dumps resolve channels and roles when possible, show
missing IDs when objects were deleted, and summarize pending reviews or
joinwatch timers by count instead of exposing message contents.

## Joinwatch

When a user with an account younger than the configured threshold joins, an embed
is sent to the joinwatch channel when alerts are enabled. If joinwatch auto-role
is enabled, the cog also applies the configured role and starts a timer. Auto-role
can run even when alert messages are disabled.

Enable randomized auto-role delay to avoid applying the configured role immediately
when the user joins. When enabled, the cog schedules the role for a random time
between `min_time` and `max_time`. The punishment timer starts only after the role
is actually applied, not when the account joins.

Setup:

```ini
[p]honeypot joinwatch max_age 24
[p]honeypot joinwatch alert toggle true
[p]honeypot joinwatch autorole role @NewAccount
[p]honeypot joinwatch autorole randomize min_time 5
[p]honeypot joinwatch autorole randomize max_time 30
[p]honeypot joinwatch autorole randomize toggle true
[p]honeypot joinwatch autorole timer 1440
[p]honeypot joinwatch autorole action ban
[p]honeypot joinwatch autorole toggle true
```

If staff removes the auto-role before the timer expires, the timer is cleared and
no punishment is taken. If the timer expires and the user still has the role, the
cog applies the configured joinwatch action: `none`, `kick`, or `ban`.
Changing `joinwatch autorole timer` recalculates active timers immediately. If
the new timer is already expired for a user, the normal timeout action is handled
right away.

Joinwatch auto-role ignores bot owners, server mods, server admins, users with
`Manage Server`, and users whose top role is at or above the bot's top role.

## Bait Role

The bait role trap watches for users receiving a configured role. This is meant for
roles that should not be assigned to normal users, for example a fake verification,
reward, or access role used to catch automated accounts.

The bait role must be dedicated to this trap. Do not reuse the review mute role
or the Joinwatch auto-role: receiving the bait role directly triggers its configured
kick or ban action regardless of which system assigned it.
`!honeypot doctor` warns when the bait role reuses either of those roles or a role
configured as sticky by NHMisc.

Setup:

```ini
[p]honeypot bait_role role @SuspiciousRole
[p]honeypot bait_role action ban
[p]honeypot bait_role toggle true
```

When the trap is enabled and a non-exempt user receives the bait role, the cog
immediately performs the configured bait action: `kick` or `ban`. It then sends a
log embed to the configured bait-role channel if one is available.

The bait trap ignores bot owners, server mods, server admins, users with
`Manage Server`, and users whose top role is at or above the bot's top role. If
the bait role is deleted or no bait role is configured, the trap does nothing.

## Adding a channel category

Channel routing is declared in `channel_routing.py`. To add a category:

1. Reuse an existing semantic category when it fits
2. Otherwise add one `ChannelCategory` entry with its config field, type, permissions, central command, and module command
3. Route publication through that category and use the shared configuration operations
4. Add the declared static commands. The registry contract tests name any missing central or module path
5. Send every technical failure through the shared `[p]nhcogs errors` configuration. Never add a cross-category fallback

## Permissions

- View Channel, Send Messages, Read Message History, Manage Messages (in honeypot channel)
- Manage Messages (in every visible channel where cached purge should remove recent scammer messages)
- View Channel and the permissions declared for each configured destination
- Create Public Threads, Send Messages in Threads, and Manage Threads
  (in the review channel)
- Send Messages (in the joinwatch channel)
- Kick Members (if using kick)
- Ban Members (if using ban)
- Manage Roles (if using review mute role or joinwatch auto-role)
- Manage Channels (if using `channel create`)
- Bot role must be above users it punishes, the review mute role, and the joinwatch auto-role

## Intents

- `GUILD_MEMBERS` (privileged): required for `on_member_join` (joinwatch) and `on_member_update` (joinwatch auto-role and bait role)
- `MESSAGE_CONTENT` (privileged): required for `on_message` (detection)

Both are enabled by default in RedBot v3.5+.

## Data Storage

Guild configuration stores channel IDs, role IDs, booleans, numeric settings,
custom messages, historical counters, and joinwatch timers. Detection cases are
stored separately in `detection_cases.sqlite`; SQLite is authoritative for case
status, captured messages and signals, attachment metadata, containment state,
review publications, and durable operations while the case is active. After the
terminal summary/thread update, role release, and evidence cleanup complete, the
detailed case data is compacted. Only the case/guild/user identifiers and Discord
summary/thread endpoint needed for later privacy deletion remain.

Copied case evidence is stored under `detection_case_files`. Resolution queues
durable cleanup of those files. If moderators select attachments as image
learning samples, both true-positive and false-positive files are copied to the
separate image-scan dataset with SHA-256, pHash, dHash, and aHash before temporary
case evidence cleanup. False-positive samples prevent known safe images from being
reported again. Samples remain under the image-scan retention controls. Red
user-data deletion removes case rows and case evidence for that
user. When Red leaves a guild, the guild-removal listener removes that guild's
case rows and case evidence.

Firstpost seen authors are stored separately in `firstpost_seen.sqlite` under
the cog data directory so large servers do not inflate Red Config.

Honeypot also maintains `message_registry.sqlite`, a 14-day index of guild,
channel, author, and message IDs observed through Discord Gateway events. It
stores the observation timestamp, latest observed pin state, author kind, and an
optional one-way spam fingerprint. It does not store message content or
attachments. The registry powers automatic purge, duplicate-spam detection, and
the managed Cleanup commands. User-data deletion, guild removal, and channel or
thread deletion remove the matching registry rows.

## Operational Notes

- Bot owners, mods, admins, users with `Manage Server`, and users at or above the bot's top role are ignored
- Purge and `[p]cleanup` use the durable Gateway-observed message registry;
  they never scan channel history
- `[p]cleanup messages <1-1000>` removes observed messages before the command in the
  current channel; `[p]cleanup user <mention-or-id> <1-1000>` removes the
  user's latest observed messages across the server
- Cleanup covers only messages observed while Honeypot was loaded and enabled for
  the guild, skips messages last observed as pinned by default, and requires the
  consolidated NHCogs extension
- When using review mode, a mute role is used as temporary containment until moderators decide
- `!honeypot doctor` checks all permissions and configuration at once
- Stats are per-server
