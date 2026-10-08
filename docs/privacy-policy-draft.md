# Privacy policy draft

Draft for administrator review. This is not yet the application's published privacy policy.

Before publication, confirm the operator, private contact, application identity, enabled features, production version, actual storage security, retention decisions, and public URL. Other loaded Red cogs may process data beyond NHCogs.

Operator: [OPERATOR]
Application: [APPLICATION_NAME_AND_ID]
Private contact for data requests: [PRIVATE_CONTACT]
Effective date: [EFFECTIVE_DATE]

## Purpose

The application supports moderation, verification, role management, and community workflows in the NewHorizons Discord server. It processes data needed for the features enabled by the server's administrators.

## Data processed

Member and role features use guild, user, and role IDs, current assignments, joins, account metadata relevant to configured verification, and persistent-role settings.

Activity features store user-linked message counts for a configured detail period. Older daily summaries contain aggregate counts and channel IDs without user IDs. Role analytics stores a current member-role index.

Moderation and verification features store relevant observations, actions, reasons, account snapshots, verification outcomes, and selected evidence. The message registry retains identifiers, timestamps, pin state, and optional content fingerprints, without raw text or attachments.

Image moderation stores reference images, hashes, labels, source information where available, and review records. The implementation uses deterministic hashes, similarity matching, and heuristic calibration. It is not an LLM or neural-network training pipeline.

CustomCommands stores responses, weights, cooldowns, access configuration, revisions, and author or editor metadata. Migration can create local backup and report files.

Bot Proxy stores character presets and avatars, active workflow records, published text and versions, publication metadata, and operational references.

GitHubTickets stores developer profiles, expertise categories, optional GitHub usernames, ticket and notification records, and selected-reviewer presence at notification time.

Technical errors contain operational identifiers and bounded summaries. Tracebacks can be sent to configured private Discord destinations.

## Storage, access, and copies

The application uses local SQLite, Red configuration, and feature-specific files on its host. Selected evidence and reports can also be published to configured Discord destinations.

[CONFIRM_HOSTING_ACCESS_CONTROLS_AND_ENCRYPTION]

Exports and backups are separate copies and need their own access and retention procedures. A local deletion does not automatically erase previously downloaded exports or old backups.

## Retention

The message registry expires after 14 days. Detailed activity retention is configurable. Current-state role analytics is updated as membership and roles change.

Image references have no automatic expiry and remain until explicitly removed. Verification, moderation, imported history, ticket history, custom responses, and Proxy publication records have feature-specific retention rather than one common expiry.

[CONFIRM_APPROVED_RETENTION_FOR_MODERATION_EVIDENCE_REFERENCE_SAMPLES_AND_OTHER_HISTORY]

The operator must finalize retention and request handling before publication. Do not describe a general moderation exception as confirmed by Discord without a specific basis.

## Data requests and implemented controls

Requests go to [PRIVATE_CONTACT]. Confirm the account and specify the data or feature concerned.

CustomCommands requests anonymize author and editor attribution in the current catalog, legacy configuration, and local migration artifacts. Shared command content, cooldowns, and current access configuration remain for ordinary requests.

Bot Proxy requests delete presets created by the user and their saved avatars, close active sessions, and anonymize matching personal attribution. Presets created by other users, published server-owned messages, their content, and operational references remain.

Automatic reviewer notifications are opt-in. Disabling them or clearing the profile prevents future availability processing by that routing feature. Existing ticket history is separate from this opt-out.

Other NHCogs stores have existing deletion or anonymization hooks with feature-specific behavior. The handling of moderation evidence, sanctions, and reference data must be finalized with the administrator and, where necessary, Discord. The image-reference dataset is not included in the current case-deletion hook.

A deletion request does not itself remove a Discord ban or replace the server's appeal process. Published messages and shared content may require separate review when they contain personal information.

New activity, command edits, or a newly opened workflow can create new records after a request. Data deletion is not a general opt-out from every future moderation action.

## Publication requirements

Replace every placeholder. Verify the description against production and establish the actual private request route. Publish the final document at a stable public URL, configure it in Developer Portal, and make it accessible from the bot.

[Discord Developer Terms, section 5](https://support-dev.discord.com/hc/en-us/articles/8562894815383-Discord-Developer-Terms-of-Service) defines the application's policy, request-handling, and security obligations.
