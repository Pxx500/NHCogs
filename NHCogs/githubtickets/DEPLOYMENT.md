# Switching to GitHub labels

This is a one-time deployment procedure for replacing the old Discord category system.
It removes old tickets and category assignments. GitHub profile links and Red's channel,
role, timing, credential and maintainer configuration are preserved.

Do not run this as routine maintenance. Discord message and thread deletion is irreversible.

## Stop and back up

1. Update the cog, leaving automatic ticket creation disabled
2. Run `[p]githubtickets github disable` if the integration was enabled
3. Stop Red before editing its database
4. Back up the GitHubTickets data directory and Red configuration

The database is `githubtickets.sqlite` inside Red's GitHubTickets cog data directory.
In the standard container layout this is `/data/cogs/GitHubTickets/githubtickets.sqlite`.
Use the actual persistent volume location for your installation.

## Remove old Discord tickets

Before clearing the database, inspect the exact Discord targets with a SQLite client:

```sql
SELECT ticket_id, channel_id, message_id, thread_id FROM tickets;
```

Delete those ticket messages and their threads on Discord. Do not delete the configured
channel or unrelated messages. Do not mark the GitHub pull requests closed, change labels,
or remove GitHub assignees. Confirm that all listed Discord targets are gone before clearing
the database. If cleanup cannot be completed, retain the database and finish cleanup first.

## Reset ticket and category data

With Red still stopped, run this transaction against the updated database:

```sql
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
DELETE FROM github_outbox;
DELETE FROM github_deliveries;
DELETE FROM github_redelivery_requests;
DELETE FROM github_delivery_recovery;
DELETE FROM github_pull_requests;
DELETE FROM tickets;
DELETE FROM profile_categories;
DELETE FROM categories;
UPDATE profiles SET automatic_pings = 0;
DELETE FROM profiles WHERE github_username IS NULL OR trim(github_username) = '';
COMMIT;
```

Ticket category links, exclusions and ping history are deleted through foreign keys.
GitHub usernames remain stored and continue to represent the developer's profile link.
No GitHub API calls are made by this reset. Keep the backup until deployment is verified.

## Configure labels and enable creation

1. Start Red and confirm GitHubTickets loaded successfully
2. Configure the GitHub App and receiver as described in the README
3. Run `[p]githubtickets github enable`
4. Run `[p]githubtickets category sync` in a private moderator channel
5. Use `[p]githubtickets category review` to approve reviewer categories
6. Ask developers to select their new categories and opt into automatic pings again
7. Run `[p]githubtickets github creation enable` when the catalog is ready

Old creation events recovered during setup do not create a ticket backlog. For an existing
pull request that needs a new ticket, remove and reapply `discord-ticket`, or use `/newticket`.
Check one test pull request through creation, claim, unassign and close before wider use.
