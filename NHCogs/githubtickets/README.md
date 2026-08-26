# GitHub Tickets

GitHub Tickets publishes pull request review tickets in Discord. Developers can maintain
expertise profiles, find people by category, request a specific reviewer, or let the bot
route a ticket to eligible reviewers over time.

`[p]` means your bot prefix. If your bot prefix is `!`, then `[p]githubtickets` is typed as
`!githubtickets`.

## Installation

GitHub Tickets is loaded as part of the combined `NHCogs` extension.

```ini
[p]load downloader
[p]repo add NHCogs https://github.com/Pxx500/NHCogs
[p]cog install NHCogs NHCogs
[p]load NHCogs
[p]slash sync
```

Run `[p]slash sync` again after an update that changes the application commands.

## Permissions and initial setup

Every `[p]githubtickets` configuration command is guild-only and requires Manage Messages.
A member can use the profile and ticket application commands when they have one of the
configured participant roles or Manage Messages. A person selected as the direct reviewer
can claim or decline that ticket even when they are not otherwise a participant.

The bot needs these permissions in the ticket channel:

- View Channel
- Read Message History
- Send Messages
- Create Public Threads
- Send Messages in Threads
- Manage Threads

Configure at least one participant role and the ticket channel before normal use. The log
channel and expertise categories are optional.

```ini
[p]githubtickets role add @GTNH-Devs
[p]githubtickets role add @GTNH-Contributors
[p]githubtickets channel set #github-tickets
[p]githubtickets logchannel set #github-ticket-logs
[p]githubtickets category add rendering
[p]githubtickets category add mixins
```

## Application commands

### Create a ticket

`/newticket`

Opens an ephemeral form for a pull request title, pull request link, optional categories,
ping behavior, and an optional direct reviewer. The bot does not call GitHub or verify the
link. Automatic routing requires at least one category. Direct routing requires a selected
reviewer.

The available ping behaviors are:

- No ping creates the ticket without scheduling reviewer pings
- Automatic waits for the volunteer window, then selects eligible reviewers
- Direct then wait pings the selected reviewer immediately and does not fall back to
  automatic routing
- Direct then automatic pings the selected reviewer immediately, waits for the direct
  response time, then starts automatic routing

### Manage your developer profile

`/developerprofile`

Opens an ephemeral dashboard where a participant can edit their optional GitHub username,
select expertise categories, allow or disable automatic pings, browse profiles by category,
or clear their profile after confirmation. Saving an empty profile removes its stored row.

### View another developer profile

`Apps → Developer Profile`

The user context menu shows the selected member's optional GitHub username and expertise
categories in an ephemeral response. It does not show presence or automatic ping consent.

## Prefix command overviews

`[p]githubtickets`

The bare root group shows current configuration and its direct command categories. It does
not dump deeper commands into the root overview. Invoking a bare nested group shows its
current configuration and all descendant leaf commands under that group.

In a channel visible to `@everyone`, configuration values are not read. The overview shows
only safe command syntax and explains that current values are available in a private
moderator channel. All overview output disables mentions.

| Command | Description |
|---|---|
| `[p]githubtickets` | Show configuration and direct command categories |
| `[p]githubtickets channel` | Show ticket-channel configuration and commands |
| `[p]githubtickets logchannel` | Show log-channel configuration and commands |
| `[p]githubtickets role` | Show participant-role configuration and commands |
| `[p]githubtickets category` | Show expertise-category configuration and commands |
| `[p]githubtickets timing` | Show routing timing configuration and commands |
| `[p]githubtickets profile` | Show developer-profile maintenance commands |

## Ticket and log channels

| Command | Description |
|---|---|
| `[p]githubtickets channel set <channel>` | Set the text channel where tickets are published |
| `[p]githubtickets channel clear` | Clear the ticket channel |
| `[p]githubtickets logchannel set <channel>` | Set the channel that records completed tickets |
| `[p]githubtickets logchannel clear` | Disable completed-ticket logs |

Only Mark finished is logged. A missing log channel or a failed log send never blocks ticket
completion and is reported only in the bot logs.

## Participant roles

| Command | Description |
|---|---|
| `[p]githubtickets role add <role>` | Allow a role to create tickets and use participant actions |
| `[p]githubtickets role remove <role>` | Remove a configured participant role |

Members with Manage Messages always count as participants. Losing a participant role does
not cancel an existing assignment, but it prevents new participant-only actions unless the
member is the ticket's direct target.

## Expertise categories

| Command | Description |
|---|---|
| `[p]githubtickets category add <name>` | Add an expertise category |
| `[p]githubtickets category remove <name>` | Remove an expertise category |

Category names are trimmed, converted to lowercase, limited to 100 characters, and unique
per server. A server can have at most 25 categories. Removing a category removes it from
stored profiles and active routing state. Existing ticket text is not rewritten.

Categories are optional for manual and direct tickets. A ticket without categories cannot
use automatic routing.

## Ping limit and timing

| Command | Description |
|---|---|
| `[p]githubtickets maxpings <count>` | Set the maximum number of pings per ticket |
| `[p]githubtickets timing` | Show all routing timing values and commands |
| `[p]githubtickets timing protection <duration>` | Set the automatic-ping protection period after ticket activity |
| `[p]githubtickets timing volunteer <duration>` | Set the initial period where anyone can claim before automatic routing |
| `[p]githubtickets timing online <duration>` | Set the response time after pinging an Online reviewer |
| `[p]githubtickets timing idle <duration>` | Set the response time after pinging an Idle reviewer |
| `[p]githubtickets timing donotdisturb <duration>` | Set the response time after pinging a Do Not Disturb reviewer |
| `[p]githubtickets timing offline <duration>` | Set the response time after pinging an Offline reviewer |
| `[p]githubtickets timing direct <duration>` | Set the response time for a direct reviewer |

Durations accept a nonnegative integer with an optional `s`, `m`, or `h` suffix. A value
without a suffix is interpreted as seconds. Setting the maximum ping count to `0` disables
scheduled pings without closing existing tickets.

Defaults:

- Maximum pings: `3`
- Protection period: `10` seconds
- Initial volunteer window: `2` hours
- Online response time: `2` hours
- Idle response time: `4` hours
- Do Not Disturb response time: `6` hours
- Offline response time: `24` hours
- Direct response time: `24` hours

Automatic routing prefers Online members, then Idle, Do Not Disturb, and Offline members.
It considers only cached server members with a participant role or Manage Messages, a
stored profile, automatic pings enabled, and at least one matching category. A reviewer who
declined, unassigned, timed out, or was already pinged cannot be selected again for the same
ticket. When the ping limit is exhausted, the ticket remains open for a manual claim.

## Administrative profile maintenance

| Command | Description |
|---|---|
| `[p]githubtickets profile clear <user_id>` | Clear one member's stored developer profile |

This command accepts a positive Discord user ID. Participants clear their own profile from
the `/developerprofile` dashboard.

## Ticket controls and cleanup

Open tickets show Mark finished, Claim, and Decline. Claimed tickets show Mark finished and
Unassign.

- A participant, a member with Manage Messages, or the selected direct reviewer can claim
  or decline an open ticket
- The assignee or a member with Manage Messages can unassign a claimed ticket
- The ticket author, assignee, or a member with Manage Messages can mark a ticket finished
- Declining or unassigning excludes that member from future pings for the same ticket
- Ticket activity postpones only automatic pings by the configured protection period

After successful cleanup, Mark finished deletes the ticket message, its thread, and the
active database state. Failed Discord cleanup is retried later while the ticket remains in
its finishing state. When a configured log channel is available, the bot records who
finished the ticket. Deleting the ticket message or its thread also removes the ticket. The
bot uses saved Discord IDs for normal updates and does not fetch messages merely to check
whether they still exist.

All profile dashboards and profile lookups are ephemeral. The ticket channel and optional
completed-ticket log channel are the normal non-ephemeral surfaces created by this cog.
