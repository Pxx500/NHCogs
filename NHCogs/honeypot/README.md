# Honeypot

Detect scam messages, review evidence, intercept animated images, monitor young accounts, and apply moderator-selected punishments. Maintained by Pxx500, originally based on AAA3A's Honeypot.

## Setup

Honeypot loads through the combined `NHCogs` extension. Requires Red 3.5.23+ and Python 3.10+. Downloader installs dependencies, including AAA3A_utils, Pillow, and the AVIF plugin.

```text
[p]load downloader
[p]repo add NHCogs https://github.com/Pxx500/NHCogs
[p]cog install NHCogs NHCogs
[p]load NHCogs
```

Start with a private review destination and run `doctor` before enabling enforcement:

```text
[p]honeypot channels honeypot create
[p]honeypot channels review #automod-filter
[p]honeypot honeypot action review
[p]honeypot honeypot toggle true
[p]honeypot doctor
```

The bot needs View Channel, Send Messages, Embed Links, and Manage Messages where it detects or deletes messages. Review requires Read Message History, Attach Files, Create Public Threads, Send Messages in Threads, and Manage Threads. Enable Ban Members, Kick Members, Manage Roles, or Manage Channels only for features that use them. Keep the bot's role above punishment roles and targets. GIF and manual mutes require Red's core `Mutes` cog and its configured mute role.

Enable the privileged Server Members and Message Content intents. Configure technical error reporting through the shared `[p]nhcogs errors` commands in the [shared README](../README.md).

## Command reference

- `[p]` means your bot's prefix. All Honeypot text commands and the `Punish` message menu require Manage Messages and a server context
- `<argument>` is required, `[argument]` is optional. A slash separates choices: `[true/false]` means an optional boolean. Write one value without brackets or the slash. Most optional configuration arguments show the current value when omitted
- `[confirm]` means the optional literal word `confirm`, not a boolean. For example, `[p]honeypot debug imagescan cleanup_events confirm` deletes event files. Omitting `confirm` only previews the cleanup
- Use `clear` to unset a channel or role destination where supported
- Bare `[p]honeypot` shows direct categories. Nested groups show descendant commands and full syntax, with current configuration only when `@everyone` cannot view the channel. The `gifdetector message` group instead shows or sets its warning text

### Main detection

Groups: `[p]honeypot honeypot`, `[p]honeypot honeypot roles`, `[p]honeypot honeypot keywords`, `[p]honeypot honeypot keywords attachments`, `[p]honeypot spam`, `[p]honeypot purge`

| Command | What it does |
|---|---|
| `[p]honeypot honeypot toggle [true/false]` | Main message-detection switch |
| `[p]honeypot honeypot action [kick/ban/review/none]` | Suspicious-message action: kick, ban, review, none |
| `[p]honeypot honeypot fallback_action [review/kick/ban/none]` | Fallback action: review, kick, ban, none |
| `[p]honeypot honeypot dry_run [true/false]` | Suppress punitive effects, not evidence capture or deletion |
| `[p]honeypot honeypot whitelist_mode [bypass/review/fallback/none]` | Trusted-role handling: bypass, review, fallback, none |
| `[p]honeypot honeypot automated_kick_fail_warn [true/false]` | Warn when an automated kick target has left |
| `[p]honeypot honeypot roles add <role>` | Trust a role |
| `[p]honeypot honeypot roles remove <role>` | Remove a trusted role |
| `[p]honeypot honeypot roles list` | List trusted roles |
| `[p]honeypot honeypot keywords add <keyword>` | Add a scam phrase |
| `[p]honeypot honeypot keywords remove <keyword>` | Remove a scam phrase |
| `[p]honeypot honeypot keywords list` | List scam phrases |
| `[p]honeypot honeypot keywords reset` | Restore default phrases |
| `[p]honeypot honeypot keywords attachments add <pattern>` | Add a filename-base regex |
| `[p]honeypot honeypot keywords attachments remove <pattern>` | Remove a filename-base regex |
| `[p]honeypot honeypot keywords attachments list` | List filename patterns |
| `[p]honeypot honeypot keywords attachments reset` | Restore default patterns |
| `[p]honeypot spam toggle [true/false]` | Detect repeats across channels |
| `[p]honeypot spam action [review/kick/ban/none]` | Spam action: review, kick, ban, none |
| `[p]honeypot spam window [seconds]` | Matching window, 3–60 seconds |
| `[p]honeypot spam channels [count]` | Required distinct channels, 2–10 |
| `[p]honeypot purge backward [seconds]` | Backward deletion window, 60–3600 seconds |
| `[p]honeypot purge forward [seconds]` | Forward window, 0–300 seconds. Zero disables it |

Detection combines account age, scam phrases, attachment counts and filenames, and enabled image matching. Spam detection requires matching messages with attachments or scam phrases. The main switch does not control JoinWatch, bait roles, or GIF interception.

Firstpost detection and its commands have been retired. Existing firstpost configuration keys are pruned on load. Historical cases, pending case operations, and aggregate statistics remain intact. The old `firstpost_seen.sqlite` file and claim table are left untouched, but the detector no longer reads or writes them. An attachment-heavy first message no longer triggers firstpost, and enabled image scanning can evaluate it when no remaining detector has already selected an action.

### Channels

Groups: `[p]honeypot channels`, `[p]honeypot channels honeypot`, `[p]honeypot channels gif-detector`

Destinations are independent. Module-specific setters update the same settings as these central commands. Honeypot sources and GIF scopes are separate lists, not output destinations.

| Command | What it does |
|---|---|
| `[p]honeypot channels review [channel/clear]` | Review destination |
| `[p]honeypot channels daily-stats [channel/clear]` | Public daily summaries |
| `[p]honeypot channels manual-evidence [channel/clear]` | Private manual evidence |
| `[p]honeypot channels joinwatch [channel/clear]` | JoinWatch alerts |
| `[p]honeypot channels captcha [channel/clear]` | CAPTCHA panel and wave invitations |
| `[p]honeypot channels captcha-log [channel/clear]` | Private CAPTCHA audit destination |
| `[p]honeypot channels bait-role [channel/clear]` | Bait-role notifications |
| `[p]honeypot channels gif-debug [channel/clear]` | GIF diagnostics |
| `[p]honeypot channels honeypot create` | Create a trap channel |
| `[p]honeypot channels honeypot add <channel>` | Add a trap channel |
| `[p]honeypot channels honeypot remove <channel>` | Remove a trap channel |
| `[p]honeypot channels honeypot list` | List trap channels |
| `[p]honeypot channels gif-detector add [channel]` | Monitor a channel, defaulting to the current channel |
| `[p]honeypot channels gif-detector remove [channel]` | Stop monitoring a channel, defaulting to the current channel |
| `[p]honeypot channels gif-detector list` | List GIF scopes |

### Image detection

Groups: `[p]honeypot imagescan`, `[p]honeypot imagescan detector`

| Command | What it does |
|---|---|
| `[p]honeypot imagescan add` | Learn images from the message you reply to |
| `[p]honeypot imagescan remove <identifier>` | Remove a sample and its file |
| `[p]honeypot imagescan dropfile <identifier>` | Remove its file, keep matching hashes |
| `[p]honeypot imagescan rebuild` | Rebuild detector thresholds |
| `[p]honeypot imagescan status` | Show samples, settings, and timing |
| `[p]honeypot imagescan detector toggle [true/false]` | Enable image enforcement |
| `[p]honeypot imagescan detector action [none/review/kick/ban]` | Match action: none, review, kick, ban |
| `[p]honeypot imagescan detector threshold [threshold]` | Maximum hash difference, 0–100 |

### Review and manual punishment

Groups: `[p]honeypot review`, `[p]honeypot evidence`, `[p]honeypot punishment`, `[p]honeypot punishment role-nt`

| Command | What it does |
|---|---|
| `[p]honeypot review toggle [true/false]` | Route detections to review |
| `[p]honeypot review channel [channel/clear]` | Review destination |
| `[p]honeypot review kick_fail_warn [false/true/manual]` | Missing kick-target warning: false, true, manual |
| `[p]honeypot punishment mute_role [role/clear]` | Temporary review containment role |
| `[p]honeypot evidence status` | Show manual evidence settings |
| `[p]honeypot evidence channel [channel/clear]` | Private evidence destination |
| `[p]honeypot punishment role-nt add <role> <channel> [channels...]` | Assign source channels to a Role n't |
| `[p]honeypot punishment role-nt remove-channel <role> <channel> [channels...]` | Remove source channels from a Role n't |
| `[p]honeypot punishment role-nt notification <role> [channel]` | Show or set its notification destination |
| `[p]honeypot punishment role-nt notification-clear <role>` | Restore source-channel notifications |
| `[p]honeypot punishment role-nt remove <role>` | Remove a configured Role n't |
| `[p]honeypot punishment role-nt list` | List Role n't punishments |

Detected messages and attachments appear in a case summary and its timeline thread. Failed downloads are marked missing instead of blocking the case. Use Ban, Kick, Ignore, and image-review buttons to resolve it. Pending cases expire after 24 hours. Case state and pending operations survive restarts.

Right-click a message and choose **Apps > Punish** to select mute, kick, ban, and configured Role n't roles. Punishments start unselected. Ban and kick exclude all other punishments. Mute and multiple Role n't roles can be combined. Mute durations use `30m`, `2h`, `3d`, or `1w`, up to 28 days. A private audit is always created before source deletion, even with evidence saving disabled. Public notifications do not disclose whether evidence was saved.

### JoinWatch and bait roles

Groups: `[p]honeypot joinwatch`, `[p]honeypot joinwatch alert`, `[p]honeypot joinwatch autorole`, `[p]honeypot joinwatch autorole randomize`, `[p]honeypot joinwatch groups`, `[p]honeypot joinwatch history`, `[p]honeypot bait_role`

| Command | What it does |
|---|---|
| `[p]honeypot joinwatch toggle [true/false]` | Monitor young accounts joining |
| `[p]honeypot joinwatch alert toggle [true/false]` | Enable alerts independently of role assignment |
| `[p]honeypot joinwatch channel [channel/clear]` | Alert destination |
| `[p]honeypot joinwatch max_age [hours]` | Account-age limit, 1–1000000 hours |
| `[p]honeypot joinwatch autorole toggle [true/false]` | Enable temporary role assignment |
| `[p]honeypot joinwatch autorole role [role/clear]` | Temporary role |
| `[p]honeypot joinwatch autorole timer [minutes]` | Time until escalation, 1–10080 minutes |
| `[p]honeypot joinwatch autorole action [none/kick/ban]` | Timer action: none, kick, ban |
| `[p]honeypot joinwatch bantimers` | Privately list timers and untimed role holders |
| `[p]honeypot joinwatch captcha <true/false>` | Enable or disable CAPTCHA admission for new age-based entries, preserving existing auto-role protection |
| `[p]honeypot joinwatch groups toggle <true/false>` | Enable or disable group detection for future joins |
| `[p]honeypot joinwatch groups criteria <minimum_accounts> <join_window_minutes> <creation_distance_hours>` | Preview the current-member impact and confirm future-join criteria |
| `[p]honeypot joinwatch groups limits <max_active> <per_minute>` | Set the automatic group-only enrollment budget without starting or catching up checks |
| `[p]honeypot joinwatch history import` | Import one attached normalized first-join history JSON without role changes |
| `[p]honeypot joinwatch wave <minimum_accounts> <join_window_minutes> <creation_distance_hours>` | Preview a saved historical wave and explicitly confirm its execution |
| `[p]honeypot joinwatch autorole randomize toggle [true/false]` | Delay role assignment |
| `[p]honeypot joinwatch autorole randomize min_time [minutes]` | Minimum delay, 1–10080 minutes |
| `[p]honeypot joinwatch autorole randomize max_time [minutes]` | Maximum delay, 1–10080 minutes |
| `[p]honeypot bait_role toggle [true/false]` | Enable the bait-role trap |
| `[p]honeypot bait_role role [role/clear]` | Bait role |
| `[p]honeypot bait_role action [kick/ban]` | Trap action: kick, ban |
| `[p]honeypot bait_role channel [channel/clear]` | Notification destination |

JoinWatch measures account age, not time on the server. Its deadline is set when the account is enrolled, before any delayed role assignment. Removing the role clears the timer. Changing the duration recalculates active deadlines and can trigger overdue punishments. `bantimers` shows manually assigned roles without creating timers, using the local member cache and warning when it is incomplete.

The bait role triggers punishment when assigned. Use a dedicated role, never a review mute, JoinWatch role, or sticky role. Protected moderators, administrators, bot owners, and targets outside the bot's role hierarchy are exempt from automated role enforcement.

### CAPTCHA and historical waves

Group: `[p]honeypot captcha`. Its bare invocation shows command syntax and, in a private moderator channel, the configured channel, log, panel, and existing JoinWatch role. There is no separate CAPTCHA role or role setter.

| Command | What it does |
|---|---|
| `[p]honeypot captcha channel [channel/clear]` | Set the ordinary text channel for Verify and wave invitations |
| `[p]honeypot captcha logchannel [channel/clear]` | Set a private moderator audit destination |
| `[p]honeypot captcha panel` | Explicitly publish or refresh one persistent Verify panel |
| `[p]honeypot captcha test <member>` | Test the full restriction flow for a regular account, or offer CAPTCHA-only practice to a protected member |
| `[p]honeypot captcha status <member>` | Privately inspect an account's active check and deadline |
| `[p]honeypot captcha resolve <member> <reason>` | Accept the member and settle only their JoinWatch restriction, recording the moderator and reason |

New CAPTCHA and group sources are off after installation. Installation doesn't publish a panel, import history, start a wave, or change the account-age limit. Tests and the panel work without enabling automatic enrollment. Configure the role through `[p]honeypot joinwatch autorole role`, the CAPTCHA channel and private log, then publish the panel. `doctor` checks the configured role, channel access, bot permissions, and panel. Run controlled account tests before wider activation. Daily statistics use the existing destination without another enable switch.

The participant uses Verify without Manage Messages. An active JoinWatch timer grants access, including timers created before CAPTCHA was added, not merely possession of the role. New timers are activated after successful role application. Pending role assignments don't grant access to CAPTCHA. Each attempt has two independently generated shape-counting questions, displayed as lossless WebP images in embeds, and six numbered answers per question. The second question replaces the same private reply. Once verification lifts the restriction, the private task is deleted without a separate success message. Errors and remaining restrictions stay visible. An incorrect answer ends the full attempt. One more attempt starts both questions again. After two failed attempts, the participant must DM a moderator. The existing ban deadline continues. No Help button or automatic attempt reset is provided. Infrastructure failures don't consume attempts.

The test command doesn't override a real timer, lift independent punishments, add public statistics, or enable automatic bans. An unfinished test is reused. A completed or locked test can be repeated with new questions. Successful checks settle their own timer, but another moderator or case restriction keeps the role and requires moderator review. Restarts and rejoins preserve attempts and deadlines. A pending role release resumes after a restart even if the member has since become protected. `joinwatch captcha false` disables admission of new age-based entries to CAPTCHA, not existing JoinWatch role assignment or its ban timer. Previously admitted participants can still complete their checks. `joinwatch autorole toggle` controls age-based role protection. `joinwatch groups toggle` controls new group detection independently.

For protected members, `captcha test` instead offers a Try CAPTCHA button usable only by the selected member. Practice uses the same questions, two-step answers, and two attempts, but never changes roles, timers, or statistics. It can be repeated with the command. Practice buttons expire after five minutes of inactivity and aren't restored after a restart. Bots can't participate.

CAPTCHA updates the existing private JoinWatch alert instead of posting each event separately. If there is no alert, the JoinWatch publisher creates one in the configured CAPTCHA log channel. Its message reference survives reloads with the incident. Verify panels are rebound on cog reload, and active question/retry buttons use the current cog.

Bot logs under `red.Honeypot.captcha` record interaction arrival age, acknowledgement time, and reply upload time at INFO level. Failures include tracebacks. These diagnostics stay out of Discord messages and don't include answers or interaction tokens. A successful upload doesn't confirm that the participant's client has displayed the image.

New wave invitations include the member mentions, a short verification prompt, and Verify. They are scheduled for deletion after 25 seconds, leaving the permanent panel and member timers unchanged. The next batch's questions prepare during the 10-second pause between batches, without consuming another cooldown. Old invitations aren't cleaned up. Pending invitation deletions don't survive a full bot restart.

`Extra party guests` counts historical-wave enrollments minus confirmed rollbacks, on the original enrollment day. Passing CAPTCHA doesn't subtract an enrollment. On upgrade, retained wave records also correct previous rollbacks, without changing roles or timers. This corrects stored totals and future previews or reports, not messages already published on Discord.

An explicit `joinwatch captcha true` also admits existing valid JoinWatch timers, including entries created while CAPTCHA was off. It reports the admitted count and prepares questions in the bounded background queue. Role assignments, deadlines, already used attempts, and existing question progress stay unchanged. Repeating `true` doesn't reset checks. Startup restoration alone doesn't adopt unknown old timers.

Group criteria support 2–100000 distinct accounts, a 1–1440 minute join window, and a 1–8760 hour creation-distance window. They compare creation dates against each triggering account, not a chain of similarities. A rejoin doesn't add another participant. Absent and banned historical participants still count toward the original cohort. Only current, eligible, unprotected members receive restrictions.

Automatic group-only checks default to at most 50 active or reserved entries and five new entries per rolling minute. `groups limits` accepts 1–10000 for each value. Pending assignments reserve capacity. The budget doesn't weaken existing age-based JoinWatch protection or change an explicitly confirmed historical wave. Exceeding it stops new group-only restrictions and reports privately. Raising a limit doesn't automatically catch up previously skipped accounts. Missing CAPTCHA configuration also stops new group-only restrictions, including their delayed first application, without cancelling already-applied restrictions or age-based protection.

Criteria changes show the previous and proposed values, data completeness, current matches, already handled accounts, exclusions, and new eligible accounts. Confirm and Cancel belong to the moderator who opened the preview, with Manage Messages checked again at the click. Either action updates the original preview with its final state and removes the buttons without another private reply. Expired or stale previews cannot change settings. Confirming criteria changes future joins only, not historical members.

`wave` is a leaf command. It saves a fixed candidate list, sends a private candidate attachment, and offers Start wave with the new-account count. Start rechecks eligibility and never grows the saved list. The private progress message shows Pause while running and Resume while paused, next to Rollback. After completion it shows Rollback and Mark finished. Successful controls update that message without an extra private status reply. Errors and rollback confirmation still get private replies. Mark finished permanently closes the controls without changing roles, timers, or CAPTCHA access. Pause stops new role assignments and invitations, not existing ban deadlines or participants' ability to pass. Restart pauses unfinished waves until explicit Resume. Rollback needs a second confirmation and settles only that wave's own reasons and timers, keeping independent moderation restrictions. Invitations name at most five newly restricted members with at least 10 seconds between batches and use the same Verify handler. Account processing and Discord requests can extend the interval. Uncertain sends are never silently repeated.

History imports accept version 1 JSON, at most 20 MiB per file and 200000 observations in total. Top-level fields are `version`, `guild_id`, `source`, `generated_at`, `range_start`, `range_end`, `complete`, and `observations`. Each observation contains `user_id` and `first_joined_at`. IDs are Discord IDs as decimal strings. Dates are UTC ISO timestamps. The source range must contain the observed first joins and end no later than generation. Imported rows preserve the earliest observed first join, including absent or banned participants. Import is idempotent and doesn't infer historical first joins from a member's current rejoin date. Keep these history files private.

### GIF detector

Groups: `[p]honeypot gifdetector`, `[p]honeypot gifdetector channel`, `[p]honeypot gifdetector debug`, `[p]honeypot gifdetector message [text]`

| Command | What it does |
|---|---|
| `[p]honeypot gifdetector toggle <true/false>` | Enable GIF interception |
| `[p]honeypot gifdetector animation <true/false>` | Enable the ICBM animation |
| `[p]honeypot gifdetector retention [seconds]` | GIF visibility, 0–60 seconds. Default: 5 |
| `[p]honeypot gifdetector threshold [count]` | GIFs before a mute, 2–20. Default: 3 |
| `[p]honeypot gifdetector window [seconds]` | Burst window, 5–3600 seconds. Default: 60 |
| `[p]honeypot gifdetector muteduration [seconds]` | Mute duration, 60–604800 seconds. Default: 3600 |
| `[p]honeypot gifdetector channel add [channel]` | Monitor a channel, defaulting to the current channel |
| `[p]honeypot gifdetector channel remove [channel]` | Stop monitoring a channel, defaulting to the current channel |
| `[p]honeypot gifdetector channel list` | List monitored channels |
| `[p]honeypot gifdetector debug toggle <true/false>` | Enable one diagnostic record per completed interception |
| `[p]honeypot gifdetector debug channel [channel/clear]` | Diagnostic destination |
| `[p]honeypot gifdetector message [text]` | Show or set the static warning |
| `[p]honeypot gifdetector message set <text>` | Explicit warning setter, hidden from normal help |
| `[p]honeypot gifdetector message reset` | Restore `No gifs!` |

Monitors configured channels and their threads. Recognizes GIF uploads and links, Discord GIF embeds, supported Tenor/Giphy transcodes, and verified animated WebP, PNG/APNG, and AVIF images. Ordinary MP4 files are allowed. Bots, webhooks, and protected moderators are ignored. Historical message edits older than 30 days are ignored.

Only one animation runs per server. Other interceptions use the static warning and the same retention. Zero retention deletes the source immediately. Animated impact deletes the source and leaves an explosion for three seconds. Static warnings last at least five seconds and never disappear before the source. Burst counters reset on reload. GIF mutes use core `Mutes`, without switching to another punishment on failure.

### Statistics and configuration

Groups: `[p]honeypot stats`, `[p]honeypot config`

| Command | What it does |
|---|---|
| `[p]honeypot stats show` | Show aggregate server statistics |
| `[p]honeypot stats preview` | Preview this server's current UTC-day statistics in the invocation channel, including zero fields, without changing counters or publication state |
| `[p]honeypot stats channel [channel/clear]` | Daily summary destination |
| `[p]honeypot modstats` | Moderator counters and current workload |
| `[p]honeypot doctor` | Privately check configuration, permissions, and runtime health |
| `[p]honeypot config all` | Compact configuration summary |
| `[p]honeypot config honeypot` | Main detection settings |
| `[p]honeypot config channel` | Destinations and scopes |
| `[p]honeypot config punishment` | Punishment settings |
| `[p]honeypot config purge` | Purge windows |
| `[p]honeypot config imagescan` | Image detector settings |
| `[p]honeypot config spam` | Spam settings |
| `[p]honeypot config review` | Review settings |
| `[p]honeypot config roles` | Trusted roles |
| `[p]honeypot config keywords` | Keyword and filename-pattern counts |
| `[p]honeypot config joinwatch` | JoinWatch settings and timers |
| `[p]honeypot config bait_role` | Bait-role settings |
| `[p]honeypot config stats` | Stored counters and current case workload |

Daily summaries are published at 00:05 UTC for the completed UTC day. They contain detections, automated bans, manual bans, JoinWatch shadowbans, and JoinWatch bans. Only completed effects count, not failed actions, dry runs, or retries. Historical totals are not backfilled into dated statistics. Clearing the destination disables publication.

Historical wave enrollments add `Extra party guests` with the configured `boubs_ultra` emoji only on days with a positive count. This counts unique accounts actually restricted by an approved wave, not bans, previews, pings, tests, or already enrolled members. Ordinary JoinWatch entries remain in their existing line. `stats preview` uses the same renderer with real counters for the current UTC day, not the previous completed day. It also shows normally hidden zero-count fields, including `Extra party guests: 0`, without inventing sample values. It can be posted publicly by a moderator and never changes the counters or publication schedule.

### Research and maintenance

Groups: `[p]honeypot research`, `[p]honeypot debug`, `[p]honeypot debug imagescan`

| Command | What it does |
|---|---|
| `[p]honeypot research dump <channel_id> [channel_ids...]` | Export full history from one or more channels by ID |
| `[p]honeypot research cancel` | Stop this server's dump and clean temporary files |
| `[p]honeypot debug imagescan dump` | Export image-review events, sample files, and dates |
| `[p]honeypot debug imagescan importtpzip` | Import TP images from ZIPs attached to the command |
| `[p]honeypot debug imagescan cleanup_events [confirm]` | Preview event-file cleanup. Supply literal `confirm` to delete, leaving samples intact |
| `[p]honeypot debug exportjoinwatch` | Copy JoinWatch live rows back into Config. Bot owner only |
| `[p]honeypot debug resetstats` | Reset stored Honeypot counters |

For `research dump`, provide one or more text-channel IDs separated by spaces. Every source is exported identically, including all authors and message formats, up to the run's start time. Duplicate IDs are scanned once. Run it in a private moderator channel. The bot needs View Channel and Read Message History in every source, plus Attach Files in the destination. Only one dump can run per server.

Progress is always active. One status message updates every 30 seconds with the current channel, total message count, current message date, and elapsed time. Temporary API failures wait and resume after the last fetched message. Permanent API failures produce explicitly incomplete exports containing the data collected so far.

ZIPs contain one `channel-<id>.jsonl` per source and `metadata.json` with channel names, counts, completeness, and format version 2. Records preserve IDs, dates, authors, text, embeds, attachment metadata, reply references, and source links. Attachment files and avatars are not downloaded. Cached names and role labels are current observations, not historical snapshots. For split archives, concatenate matching JSONL files in numbered ZIP order. Keep exports private.

```text
[p]honeypot research dump 123456789012345678
[p]honeypot research dump 123456789012345678 234567890123456789
[p]honeypot research cancel
```

## Stored data

Settings and counters are per server. Cases, operations, first-observed senders, and the message registry use local SQLite storage. The registry retains observed message IDs, dates, author IDs, pin state, and optional spam fingerprints for 14 days, without content or attachments. Purge uses observed IDs, not history scans. Separate `[p]cleanup` commands are documented in their [own README](../cleanup/README.md).

Captured case files are temporary. Selected TP and FP samples remain in the image dataset under its retention settings. Red user-data deletion and guild removal remove matching records and evidence, with unavailable Discord deletions queued for retry. Developers declare channel routing in [channel_routing.py](channel_routing.py).

JoinWatch keeps active incident and attempt metadata and verification outcomes in `joinwatch_live_state` in the case SQLite store, one row per account and kind (`verified`, `pending_role`, or `pending_assignment`). The three guild Config maps stay registered so they are not pruned, and they are cleared only after a cog load has copied them and checked every user id, incident id, and payload. That load runs on `[p]reload` as well as a process start. A guild already marked `sqlite` is not copied again, so a repeated reload cannot replace live rows from Config. An empty Config is never copied over SQLite. If the copy does not match, the rows are discarded, Config stays in service, and an operational failure is recorded.

The same transaction stores a one-time backup of the three maps in `joinwatch_live_backup` before Config is cleared. A later failure cannot remove the only copy. Backups expire after 7 days, on the next cog load. Leaving a guild deletes that guild's backup immediately. A user-data deletion does not rewrite the snapshot. `[p]honeypot debug exportjoinwatch` copies the live rows back into the three Config keys and clears the marker. `restore_live_backup` does the same from the snapshot. Both write Config before clearing the marker, so a crash in between can be retried while the backup still exists. A later export or restore does nothing while Config is already the source.

Auxiliary first-observed joins and frozen wave execution state use the same case SQLite store, outside the configuration read by message detectors. First-observed joins are one row per account. The earlier JSON document stays in that database after its rows have been checked, and joins read and write the rows. Current challenge images and answers are retained only with their active check, not in audit logs or evidence archives. Live first-join observations and settled waves are retained for 90 days. Imported history remains explicitly retained for historical analysis. User-data deletion removes the corresponding observations, that account from the retained JSON document, results, and wave references. Leaving a guild clears pending JoinWatch work and verification history so rejoining doesn't revive stale enforcement.

JoinWatch also retains enrollment context and factual outcomes in the case SQLite store after active timers and waves are removed. This includes account metadata, available activity totals from NHMisc, source and wave ID, attempt events, and completion, rollback or timer punishment. Existing active timers are adopted as partial history without changing their deadlines. Missing activity is unknown, not zero. No CAPTCHA answers, session tokens or images enter this archive. Retained verification history has no automatic expiry and follows Red user-data deletion and guild removal.

With NHModeration loaded, moderators can use `[p]nhmod history export` in a private moderator channel to export these records together with observed bans, retained detection context and first joins. See [NHModeration](../nhmoderation/README.md#private-history-export). A solved CAPTCHA isn't proof of a human account, and a ban isn't a confirmed scam label. Compare completed cohorts with their pending counts, and exclude tests and rollbacks from enforcement results.
