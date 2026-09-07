# GitHub Tickets

GitHub Tickets publishes pull request review tickets in Discord. Developers can maintain
profiles, find people by category or GitHub username, request a specific reviewer, or let the bot
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

When replacing the old Discord category system, follow the one-time
[deployment reset](DEPLOYMENT.md) before enabling automatic creation.

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
channel and categories are optional.

```ini
[p]githubtickets role add @GTNH-Devs
[p]githubtickets role add @GTNH-Contributors
[p]githubtickets channel set #github-tickets
[p]githubtickets logchannel set #github-ticket-logs
```

## Application commands

### Create a ticket

`/newticket`

Opens an ephemeral form for a canonical GitHub pull request link, ping
behavior, and an optional direct reviewer. The title is read from GitHub and cannot be entered
separately. The integration validates the organization, repository access, open state, draft
state, and active ticket binding before creating the ticket. The `discord-ticket` label is not
required for manual creation. Categories come from the pull request's GitHub labels, not the
form. Automatic routing requires at least one approved reviewer category and a reviewer who
has all of them. Without those categories, the ticket stays open for manual claims without an
automatic ping. Direct routing requires a selected reviewer.

The available ping behaviors are:

- No ping creates the ticket without scheduling reviewer pings
- Automatic waits for the volunteer window, then selects eligible reviewers
- Direct then wait pings the selected reviewer immediately and does not fall back to
  automatic routing
- Direct then automatic pings the selected reviewer immediately, waits for the direct
  response time, then starts automatic routing

### Manage your developer profile

`/developerprofile`

Opens an ephemeral dashboard where a participant can enter their optional GitHub profile link,
select categories, allow or disable automatic pings, browse profiles by category, find
Discord members by an exact GitHub username, or clear their profile after confirmation.
The link must use `https://github.com/<login>`. Saving an empty profile removes its stored row.
Up to 50 approved reviewer categories are available. Above 25, the profile form and category
browser show a second category selector on the same page.

### View another developer profile

`Apps → Developer Profile`

The user context menu shows the selected member's optional GitHub username and
categories in an ephemeral response. It does not show presence or automatic ping consent.

## GitHub App integration

The GitHub integration is configured by moderators through the `github` group. Every command
in this group requires Manage Messages and is invoked from a guild. The integration settings,
receiver, and selected guild are process-wide. Running `enable` in a guild selects that one
guild for this bot process. A bare group shows a safe runtime overview. It never displays
credentials, private key paths, installation IDs, client IDs, App IDs, webhook secrets, or raw
network diagnostics.

| Command | Description |
|---|---|
| `[p]githubtickets github` | Show the GitHub integration state and available commands |
| `[p]githubtickets github enable` | Enable the integration when credentials and receiver settings are ready |
| `[p]githubtickets github disable` | Disable the receiver and GitHub workers while preserving Discord ticket data |
| `[p]githubtickets github creation` | Show automatic ticket creation state and commands |
| `[p]githubtickets github creation enable` | Allow future qualifying events to create tickets |
| `[p]githubtickets github creation disable` | Stop creating tickets without stopping synchronization |
| `[p]githubtickets github receiver` | Show receiver state and commands |
| `[p]githubtickets github receiver set <host> <port>` | Set the local receiver bind address and restart an enabled integration |
| `[p]githubtickets github receiver clear` | Clear receiver settings and disable the integration |
| `[p]githubtickets github recovery` | Show delivery recovery state and commands |
| `[p]githubtickets github recovery interval <duration>` | Set the recovery interval using seconds, `s`, `m`, or `h` |
| `[p]githubtickets github recovery run` | Queue one recovery pass when the integration is running |

The receiver accepts signed `POST` requests at `/githubtickets/webhook`. Put it behind a
public HTTPS reverse proxy and forward that path to the configured host and port. The receiver
validates the raw body signature, installation, organization, and repository before durably
accepting a delivery. It returns before Discord or GitHub processing, then workers process the
delivery asynchronously. Recovery runs after startup and every 15 minutes by default. GitHub
App delivery history is used to request redelivery for locally missing deliveries.

Requests for the same missing delivery are tracked across restarts. The bot makes at most
five requests, at least one hour apart, and alerts the maintainer if the webhook still has
not arrived. A late valid delivery is still accepted. Locally terminal failures are not
automatically redelivered, and their stored payload is erased. Invalid payloads and
conflicting immutable PR identities fail without retry. Other transient processing failures
retain the bounded retry policy. Successful retries close their corresponding diagnostic
records without suppressing the original error alerts or posting success messages.

Automatic ticket creation is off by default, independently of the integration switch. Enable
the integration first, classify the labels, then run `[p]githubtickets github creation enable`
in a private moderator channel. While creation is off, webhooks, label discovery and existing
ticket synchronization still run. Enabling creation does not replay old creation events.

When creation is on, adding `discord-ticket` to a ready pull request creates one Discord
ticket. A labeled draft waits until it becomes ready for review. The ticket mirrors GitHub
labels, using only approved reviewer categories for automatic matching. Label changes update
existing tickets without clearing claims. A manually selected No ping remains unchanged.

Discord claims add the mapped GitHub login as a pull request assignee. Discord unassign removes
that assignee. GitHub assignment and qualifying submitted reviews can claim the Discord ticket.
Converting the pull request to a draft shows Keep Ticket and Remove Ticket controls. Closing or
merging the pull request finishes the Discord ticket and writes the configured best-effort
finish log. Removing the label does not remove an existing ticket.

Create one private GitHub App for the organization and install it on the organization's
repositories. The App needs these repository permissions:

- Metadata: Read-only
- Pull requests: Read & write

Subscribe the App to these webhook events:

- Pull request
- Pull request review

Supply `organization`, `client_id`, `app_id`, and `installation_id` through Red's shared API
token service named `githubtickets`.

Store the secret files under the Red-managed GitHubTickets cog data directory using these
fixed relative paths:

```text
secrets/github-app.pem
secrets/webhook-secret.txt
```

With the standard container layout, the files are visible inside the container as:

```text
/data/cogs/GitHubTickets/secrets/github-app.pem
/data/cogs/GitHubTickets/secrets/webhook-secret.txt
```

The external host path depends on the volume mounted at `/data`. Both files must be readable
by the account running Red. Their contents and paths are never shown in command output or
public messages.

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
| `[p]githubtickets category` | Show category configuration and commands |
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

## Categories

| Command | Description |
|---|---|
| `[p]githubtickets category review` | Open controls for classifying discovered labels |
| `[p]githubtickets category sync` | Refresh labels from GTNewHorizons/GT5-Unofficial |

The bot samples labels from GTNewHorizons/GT5-Unofficial on the recovery interval and discovers
additional names from pull request events. Names are trimmed and lowercased. The same name in
different repositories maps to the same Discord category.

New names await a moderator decision. A grouped alert in the shared maintainer destination
opens Review labels. Choose Reviewer category to make a label selectable in developer
profiles, or PR label only to display it on tickets without affecting reviewer matching.
The panel also allows changing earlier decisions. Already-notified names do not cause repeat
alerts. Failed notification delivery leaves them pending for the next pass.

At most 50 labels can be reviewer categories. Other labels are not subject to this limit.
The control label `discord-ticket` is always PR-only and is omitted from ticket category text.
Demoting a reviewer category removes it from profiles. Profiles with no remaining categories
have automatic pings disabled. Both category commands require a private moderator channel.

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
stored profile, automatic pings enabled, and every category selected on the ticket. The
ticket author is never an automatic reviewer. If no eligible reviewer has every category,
the ticket remains open without an automatic ping. A reviewer who
declined, unassigned, timed out, or was already pinged cannot be selected again for the same
ticket. When the ping limit is exhausted, the ticket remains open for a manual claim.

## Administrative profile maintenance

| Command | Description |
|---|---|
| `[p]githubtickets profile clear <user_id>` | Clear one member's stored developer profile |
| `[p]githubtickets profile pings` | Show all automatic ping report commands |
| `[p]githubtickets profile pings summary` | Show total profiles and enabled/disabled counts with percentages |
| `[p]githubtickets profile pings enabled` | List developer profiles with automatic pings enabled |
| `[p]githubtickets profile pings disabled` | List developer profiles with automatic pings disabled |

The `profile clear` command accepts a positive Discord user ID. Participants clear their own profile from
the `/developerprofile` dashboard. Leaving the server removes the member's profile and
profile categories, but does not rewrite or remove their existing ticket history.

Red's user-data deletion cancels queued GitHub assignments, including work already claimed
by the worker but not executing. An in-flight GitHub write settles before deletion completes.
Previously requested unassignment can still finish, after which its identifying queue row
is erased. Clearing a developer profile is separate from Red's user-data deletion workflow.

The `profile pings` group shows a command overview without running a report. The three
report commands require Manage Messages and a channel hidden from `@everyone`. Public
channels are rejected before profiles are read.

Reports include every stored developer profile on the current server, even if the owner
no longer has a participant role. People without profiles are excluded from percentages.
An empty server report shows zero counts and `0.0%` for both preferences. These values
describe the profile's automatic ping preference, not Discord notification settings or
current eligibility for ticket routing.

Lists are sorted alphabetically by Discord username and show the member mention, Discord
username, and optional GitHub username. Profiles missing from the member cache remain
listed with `Discord username unavailable`. Long lists are sent as numbered message pages.
Reports suppress all mentions, so listing someone does not ping them.

## Ticket controls and cleanup

Open tickets show Mark finished, Claim, and Decline. Claimed tickets show Mark finished and
Unassign.

A pinged target is only being asked to review. The main ticket shows Reviewer only after
that person or another participant successfully claims the ticket.

Ticket messages start with a readable state: 🟢 Claimed, 🟡 Review requested,
⚪ No reviewer categories, ⚪ Automatic pings off, 🔴 No eligible reviewers, or
🔵 Looking for reviewer.

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

Profile dashboards and public profile lookups are ephemeral. Moderator ping reports are
sent to the private channel where the command was invoked. The ticket channel and optional
completed-ticket log channel are the other normal non-ephemeral surfaces created by this cog.


Technical failures are sent to the shared maintainer destination configured with `!nhcogs errors`. GitHubTickets keeps its ticket state and retry schedules in its own database. User-facing interaction failures contain a short generic message.
