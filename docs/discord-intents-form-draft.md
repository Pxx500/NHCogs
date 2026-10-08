# Discord intent form draft

Updated 8 October 2026 after creation of PR #149. This is a draft for human review. Do not submit it unchanged. Confirm the running application, all loaded cogs, the privacy policy, retention, and demonstration links. The PR is open and its fixes have not been confirmed on production.

## Choices for the original screenshots

These choices describe the patched NHCogs features. Verify deployment and all other loaded cogs before using them for the whole running application.

| Screenshot | Field | Choice |
|---|---|---|
| 1 | Public Privacy Policy | Yes only with a complete public policy URL. Currently unconfirmed |
| 1 | Server Members Intent | Select if the described verification, sticky-role, or member-role features are enabled |
| 1 | Presence Intent | Select if availability-based automatic reviewer routing is enabled |
| 1 | Message Content Intent | Select if passive message and attachment moderation is enabled |
| 2 | API Data off-platform | Yes |
| 3 | Presence opt-out | Yes for the patched automatic-routing feature. Confirm the rest of the application |
| 3 | User activity off-platform | Yes |
| 4 | Message Content opt-out | No for a general moderation opt-out |
| 4 | Message Content off-platform | Yes |
| 4 | ML or AI training | No for the reviewed implementation and declared operator use. Confirm other cogs and processes |

Every screenshot's explanation and demonstration field is covered below. All demonstration URLs and the public policy URL remain to be supplied. A No opt-out answer or Yes storage answer is not itself a compliance violation.

## Application Details

The application is a moderation and community-management bot operated for the NewHorizons Discord community, which has more than 90,000 members. It is built on Red-DiscordBot and the NHCogs extensions.

Its enabled features help moderators detect and review suspicious messages and attachments, manage join verification and roles, retain moderation history, restore configured roles on rejoin, manage achievements and activity summaries, perform configured cleanup, provide custom commands, and coordinate GitHub pull-request reviews.

The bot stores feature-specific identifiers, records, and selected content on its host. Image moderation uses deterministic image hashes, labelled reference examples, similarity comparisons, and a heuristic matching threshold. No LLM or neural-network training pipeline was found in the reviewed implementation.

Privacy policy: [PUBLIC_PRIVACY_POLICY_URL]
Data-request contact: [PRIVATE_CONTACT]

Before use, remove disabled features and add relevant functionality from any other loaded cog.

## Public Privacy Policy

No existing public policy URL was found in the original repository. Check for an externally published policy. Select Yes only when an accurate policy is publicly accessible, linked in Developer Portal, and accessible from the bot.

The existing end_user_data_statement in NHCogs/info.json is part of the data disclosure. Red also provides [p]mydata 3rdparty to display loaded extensions' statements and [p]mydata forgetme for user requests, subject to the running bot's settings. Confirm that these commands work for ordinary users.

The [policy draft](privacy-policy-draft.md) still needs the final operator and manual-contact details, confirmed production configuration, and approved retention descriptions. The requirement is a complete and accessible policy, not a particular file format. Confirm the public URL and its Portal configuration before selecting Yes.

## Server Members Intent

### Why do you need Guild Members?

The application uses Guild Members events to handle joins, departures, and role changes. Configured join-verification checks can alert moderators and apply a verification role. Configured persistent roles are saved when a member leaves and restored on rejoin. Role tools maintain current role assignments through full member synchronization when needed and lifecycle events thereafter.

The review-ticket workflow checks current membership and participant roles. Interactions provide information about an invoking member, but they do not replace member lifecycle events or a complete role index.

Use only the functions actually enabled in production.

### Demonstration

[MEMBERS_DEMO_URL]

Record a consenting test account joining, the configured verification or role behavior, and a private moderator alert. A sticky-role demonstration can show a configured role restored on rejoin. Redact unrelated users and private case data.

### API Data off-platform

Yes, if the enabled features use the repository's local stores.

The bot stores guild, user, and role IDs and the records needed for configured role and verification features in local SQLite and Red configuration. The privacy policy describes retention and the data-request process.

## Presence Intent

Request this intent only if availability-based review routing or another confirmed cog needs it.

### Why do you need Guild Presences?

Our pull-request review workflow uses current availability to choose an authorized volunteer who enabled automatic notifications and selected the ticket's expertise categories. It prioritizes Online, Idle, Do Not Disturb, and Offline reviewers and uses status to set the response deadline.

The NHCogs routing feature reads status only after profile opt-in, category, permissions, and ticket-exclusion checks. It does not read game or activity names or build a continuous online-time history. The selected reviewer's status at notification time is stored locally with the ticket ping and deadline.

Users can disable automatic notifications or clear their developer profile to stop new automatic selections using their presence. Before sending an automatic notification, the bot checks current eligibility again and cancels an unsent queued notification if the reviewer opted out or no longer qualifies. A notification whose sending is already underway may still complete. Existing notification history is handled separately through the data-request process.

### Demonstration

[PRESENCE_DEMO_URL]

Show consenting reviewers with matching categories and different statuses, the automatic selection and deadline, and a disabled profile being excluded.

### Presence opt-out

For the patched NHCogs automatic-routing feature: Yes for future availability processing through disabling automatic notifications or clearing the profile.

Confirm the patch is deployed and check other loaded cogs before selecting Yes for the whole application. This does not assert that historical records are erased by toggling the checkbox or that the underlying Gateway stops receiving events.

### User activity off-platform

Yes. Local ticket records retain selected-reviewer presence at notification time. Separately enabled activity-count features also retain user-linked message counts. Do not describe either as RAM-only storage.

## Message Content Intent

### Message content opt-out

No general per-user exemption from all configured moderation was confirmed. Deletion of retained data and opting out of future processing are different controls.

The application must describe the approved handling of moderation records, evidence, and reference samples accurately. Do not promise a blanket purge that disables moderation.

### Message content off-platform

Yes. Depending on enabled features, the bot stores selected evidence and image references, custom-command responses, and server-owned Bot Proxy messages locally.

The 14-day message registry retains identifiers, timestamps, pin state, author metadata, and optional one-way content fingerprints rather than raw text or attachments. This limit does not apply to every dataset.

### Machine learning or AI training

Recommended dropdown selection for the reviewed NHCogs implementation and the operator's stated use: No. Confirm that other loaded cogs and operator processes also do not train models on API message data.

The operator states that message data is not used for AI training. The reviewed detector hashes images and compares them with moderator-labelled references. It derives a similarity threshold from distances among those examples. No LLM or neural-network training pipeline was found.

Do not label hashing alone as AI training. Describe the implemented heuristic directly, and confirm that no other cog or operator process trains models on API message data before answering for the entire application.

Technical description for the text field or a clarification request:

We do not use Discord message data to train machine-learning or AI models. Our local image-moderation feature computes SHA-256 and perceptual hashes and compares incoming images with moderator-labelled known-scam and false-positive references. It uses hash distances and a heuristic threshold, including calibration from those reference distances. We retain the reference set to recognize recurring scam images and avoid false positives.

This description does not establish how Discord classifies every form of adaptive calibration. Obtain a specific determination if that distinction affects the application. Merely selecting Yes does not grant permission.

### Why do you need Message Content?

The application passively moderates ordinary server messages and attachments. Configured detectors inspect content and images, identify suspicious messages or animated attachments, and create moderation cases containing the relevant context for authorized human review.

These attachment and review workflows go beyond ordinary AutoMod keyword rules. They need messages that do not mention the bot and have not first been selected through an interaction. Slash commands and message context menus support explicitly requested actions but cannot supply every unreported message needed by the automatic moderation workflow.

Custom prefix commands are a secondary use. Simple message counting is not the basis for this request.

Use only the functions enabled on production. Do not justify the intent through bulk research collection.

### Demonstration

[MESSAGE_CONTENT_DEMO_URL]

Show a safe, prepared test message without a bot mention, automatic detection, a private review case, and a moderator decision. Use consenting test accounts and fabricated evidence. Do not publish real victims' content or unrelated private data.

## Before submission

- Confirm Application ID, installed guilds, loaded cogs, running version, enabled intents, and the actual review deadline
- Deploy and verify the scoped data-control fixes
- Check NHCogs behavior when required Gateway data is unavailable using the [behavior checklist](intent-loss-runbook.md). This does not verify Red startup or connection recovery
- Confirm the privacy contact, publish an accurate policy, and link it in Portal and the application
- Resolve moderation-evidence and reference-retention questions without indiscriminately removing sanctions or the required dataset
- Confirm hosting and backup safeguards and the scope of exported copies
- Replace all placeholders with accessible demonstration and policy URLs
- Have the administrator compare the final text with the running application

References: [audit and code scope](discord-intents-audit.md), [official review guide](https://docs.discord.com/developers/gateway/getting-started-with-privileged-intent-review), [Gateway intents](https://docs.discord.com/developers/events/gateway#privileged-intents).
