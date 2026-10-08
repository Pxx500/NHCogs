# Discord intents and data controls

Updated 8 October 2026. This document distinguishes repository changes from production configuration and unresolved retention questions. The changes have not been deployed or submitted to Discord.

## Implemented changes

### CustomCommands attribution

Ordinary owner, user, and user_strict requests now anonymize command author and editor identities. This covers the SQLite catalog, legacy configuration, and local migration backups and reports.

Command responses, cooldowns, revisions, and current access rules are preserved for ordinary requests. Discord account deletion retains the existing behavior that redacts individual access IDs without opening a restricted command to other users.

Author identity is displayed by customcom show. Editor identities are retained for migration and catalog metadata, but are not needed to execute commands or check permissions. New command creation or editing can create new attribution after a deletion request. Previously downloaded migration files are separate copies.

### Bot Proxy personas and attribution

A request deletes character presets created by the user, including their saved avatar bytes. Presets created by other users are preserved and the requesting user's updater identity is anonymized.

The user's active sessions are closed. The manager drains admitted writes and rejects stale controls or delayed avatar callbacks so they cannot recreate deleted data. Historical publisher, editor, deleter, and event-author IDs are anonymized.

Published server messages, their content and versions, and operational message, channel, and webhook references are retained. This change is a scoped deletion of personas and personal attribution, not a blanket removal of every record connected with an account.

### Imagescan export isolation

Exports now create a fresh SQLite snapshot containing only the requested guild. Events, corresponding files, active and inactive samples, heuristic state, and counters are scoped to that guild. JSONL and SQLite are taken from the same source read transaction, including committed WAL data.

The archive keeps the SQLite, JSONL, and file format. Files resolving outside the guild's directory are excluded. Source images, reference hashes, labels, and detector behavior are preserved.

### Presence minimization

Automatic GitHubTickets routing reads status only for reviewers who have opted into automatic pings, match every requested category, have the required permissions, and are not excluded by previous ticket outcomes.

Disabling automatic pings or clearing the profile prevents future status reads for this routing feature. Existing ticket history is handled separately. The selected reviewer's status at notification time remains in local SQLite with the notification and deadline.

The post-PR review reproduced a queued automatic notification being sent after profile opt-out or clearing. The correction rechecks eligibility immediately before a new send, cancels only the unsent automatic reservation, and keeps the schedule and budget so another eligible reviewer can be selected. Previously sent notifications are reconciled before this check to avoid duplicate sends. A send already underway may complete.

## What the changes establish

The patches address concrete gaps in attribution deletion, persona deletion, cross-guild export isolation, and unnecessary presence processing. They do not establish that the entire production application is compliant.

The regression tests cover requester types, access preservation, persona ownership, in-flight and delayed workflow writes, two-guild exports with WAL, and reviewer opt-in boundaries.

Final validation:

- python -m pytest tests -q -n 4 --dist loadscope after the post-PR correction: 1701 passed, 1 skipped, 442 subtests passed
- ruff check .: passed
- git diff --check: passed
- mypy: 147 errors in 31 files, advisory under the repository's CI configuration. None were reported in the new CustomCommands, Bot Proxy, Presence, or scoped-export methods. Existing imagescan errors occur outside the changed export method

The first full run found an incomplete Gate privacy test fixture, which now initializes the new Bot Proxy store as a real cog does. An existing capture-start test with a 50 ms timeout also failed under load. It passed in isolation and in the second full parallel run. Its assertions and timing were not weakened.

An independent review found no blocker in the Bot Proxy deletion lock order, session-creation gate, stale callback checks, or attribution sweep.

PR #149 was created with the approved title and body. The initial GitHub quality check passed. The post-PR review also reproduced and corrected unsent automatic notifications bypassing profile opt-out. Its regression coverage includes profile clearing, category or permission changes, replacement reviewers, and reconciliation of already sent notifications without duplicates.

## Corrections to the initial audit

A deletion hook omitting a store does not, by itself, prove that every record in the store must be deleted. The initial audit treated that implication too broadly.

Deterministic image hashes and similarity comparisons are not evidence of AI training. The detector also derives a matching threshold from moderator-labelled examples. The application should describe that mechanism accurately without claiming an LLM or neural-network training pipeline was found.

The reference dataset is a required moderation feature. No automatic dataset purge or heuristic change is part of this patch.

Moderation evidence, reasons, active sanctions, and references necessary to enforce restrictions require their own retention rules. They should not be removed through an indiscriminate user-ID purge. Honeypot and NHModeration now distinguish ordinary user and strict-user requests from explicit owner and deleted-account requests. Normal user requests remove optional profiles and activity snapshots while retaining essential moderation data and pending actions. A moderator's request does not erase another target's reasons. Stronger owner or deleted-account paths remain explicit, and unknown requester types fail before mutation.

## Questions that remain open

### Moderation and reference retention

The public Developer Terms require handling applicable deletion requests and state an exception for legally required retention. No explicit general exception for all moderation evidence was confirmed in the reviewed documentation. This does not imply an automatic unban.

The operator needs a specific answer from Discord about retaining ban reasons, supporting evidence, and necessary image references when a sanctioned user requests deletion. Necessity and the consequences of deleting evidence should be explained directly. Do not substitute a hash of an account ID and describe it as anonymous.

No new broad deletion or removal of sanctions is implemented.

The framework's ordinary-request contract explicitly allows essential operational data to remain. Its stricter request mode still permits minimal anti-abuse IDs and timestamps, and owner requests may avoid operational hazards while offering another way to handle the request. This supports reviewing retention by purpose instead of interpreting every request as a blanket purge. It is a Red framework contract, not proof of an exception to Discord's own Terms.

The post-review correction preserves cases, evidence, verification restrictions, deadlines, and action retries for both user and user_strict. This matters because strict-user mode is Red's default. Owner or deleted-account requests retain the stronger path. Necessary operational retention follows the framework contract and does not establish a universal exemption from Discord's own Terms.

### Public privacy policy and contact

The repository already has an end_user_data_statement in NHCogs/info.json and cog metadata. Those existing disclosures were updated with the implemented controls. They are the basis for the application's policy, rather than evidence that no data disclosure exists.

Red provides a request route already: [p]mydata forgetme. It can also display loaded extensions' declarations through [p]mydata 3rdparty. Their production availability and bot settings remain unconfirmed. A new email address is not the only possible way to provide an accessible request route. A private manual contact is still useful for corrections, disputes, and requests that the automatic hook does not resolve.

No public policy URL or private data-request contact was found in the original repository. A policy may exist elsewhere and needs administrator confirmation.

A [policy draft](privacy-policy-draft.md) now describes the repository's data categories and current controls. Its contact, production features, operator, storage security, and retention details must be completed before publication. The public URL must be configured in Developer Portal and made accessible from the application.

### Production facts

Confirm Application ID, actual installs, enabled intents, loaded Red cogs, production version, enabled features, storage and backup security, existing exported copies, and the review deadline.

The user reports one NewHorizons server with more than 90,000 members. Since 10 June 2026 the review threshold is based on more than 10,000 unique users who can see the application, rather than a count of guilds. The guide describes a standard 90-day notification window and annual review, but the application's actual deadline must be read from its notice.

### Research export

The existing research dump can export full channel histories. Its purpose, use, and retention need review against Discord's mining and scraping restrictions. It was not changed or represented as a justified intent use case in this patch.

### Dataset and other retention

The message registry retains IDs and optional content fingerprints for 14 days, without raw message content or attachments. Image references have no automatic expiry. Verification history, imported join history, ticket history, and Bot Proxy messages have different retention behavior. A single 14-day statement would be inaccurate.

Reference retention must remain transparent. It should not be described as an automatic user-data purge while the dataset is deliberately retained.

## Form answers supported by the repository

| Question | Answer or condition |
|---|---|
| Members justification | Join, leave, role updates, sticky roles, verification, and full role synchronization, if enabled in production |
| Members data off-platform | Yes for the local member and role stores |
| Presence justification | Availability-based routing for opted-in reviewers, if enabled |
| Presence opt-out | The routing feature supports opt-out from future availability processing. Verify other loaded cogs before answering for the whole application |
| Presence off-platform | Yes, selected-reviewer status is retained in ticket notification records |
| Message Content justification | Passive moderation of ordinary messages and attachments, beyond what interactions or AutoMod can supply |
| Message Content opt-out | No confirmed general per-user exemption from moderation |
| Message Content off-platform | Yes, selected evidence, reference images, fingerprints, custom responses, and Proxy content are stored locally |
| ML or AI training | No for the reviewed implementation and declared operator use, subject to checking other cogs and processes. Describe deterministic hashes, reference matching, and heuristic calibration accurately |
| Public policy | Pending administrator confirmation, contact, publication, and Portal configuration |
| Demo links | Still required. No demonstration URLs were supplied |

The [English form draft](discord-intents-form-draft.md) must be checked against the running application before submission.

## Code and tests

## Intent-loss protection added before the deadline

The shared capability guard blocks dependent work when the requested mask is disabled,
approval is denied, or the required state is unknown. OperationalSupport refreshes
application flags every 30 seconds while the bot is running. These changes do not manage
Red startup, change its requested intents, or reconnect it after a rejected Gateway
connection. They protect NHCogs work in the connected application.

Received messages requiring unavailable content are retained as a metadata-only queue
for up to 14 days. The worker fetches only observed message IDs after restoration.
Pending attachment capture waits before source deletion without spending its retry
budget. Automatic reviewer work and role synchronization remain pending, and an
incomplete member cache cannot replace a valid generation. A newly sent notification
gets a fresh response window after a long pause without duplicating a known sent ping.

Known actions that can use REST without privileged intents remain usable. Events never
delivered by Discord and content deleted before capture cannot be reconstructed.

Regression tests cover data-request handling, preserved moderation evidence, deferred
capture, and known REST actions. [The behavior checklist](intent-loss-runbook.md)
describes NHCogs validation and its limits.

- CustomCommands: catalog.py, cog.py, migration.py, migration_controller.py, and the catalog, cog, and migration tests
- Bot Proxy: bot_proxy_store.py, bot_proxy_manager.py, bot_proxy_workflow.py, nhmisc.py, and store, workflow, and deletion-hook tests
- Imagescan export: imagescan.py, imagescan_store.py, and test_detection_diagnostics.py
- Reviewer privacy: githubtickets.py and test_github_tickets_lifecycle.py
- Data disclosures: relevant README and info.json files

## Sources

[Discord intent review guidance](https://docs.discord.com/developers/gateway/getting-started-with-privileged-intent-review) explains review, justification, retention disclosures, and demonstrations. [Intent alternatives](https://docs.discord.com/developers/gateway/you-might-not-need-a-privileged-intent) explains why message counting alone does not justify Message Content.

[Developer Terms, section 5](https://support-dev.discord.com/hc/en-us/articles/8562894815383-Discord-Developer-Terms-of-Service) covers privacy policies, deletion requests, and data security. [Developer Policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy) covers necessary uses, mining and scraping, and ML training restrictions.

Official sources were fetched from docs.discord.com and the Discord Help Center article API on 8 October 2026.

[Red's end-user data documentation](https://docs.discord.red/en/stable/red_core_data_statement.html) and the [Cog request contract](https://github.com/Cog-Creators/Red-DiscordBot/blob/V3/develop/redbot/core/commands/commands.py) were also checked during the post-PR review.
