# Discord privileged intent application answers

Updated 9 October 2026. Prepared from the original four screenshots, the NHCogs changes in PR #149, and the presence and ticket-data minimization changes.

Each entry follows the same order: **Field from the screenshot**, **Answer**, then **Internal rationale**. Only the answer belongs in the application. Keep the internal rationale for our own review.

These answers describe the reviewed NHCogs features. Before submitting, confirm deployment, remove disabled features from the explanations, and check the other loaded cogs. Replace all bracketed placeholders. Public policy and demonstration links have not been supplied.

## Screenshot 1: Application details and intent selection

### 1. Application Details

- **Field from the screenshot**

  Application Details

  What does your application do? Please be as detailed as possible, and feel free to include links to image or video examples.

- **Answer**

  NHBot is a moderation and community management bot for the NewHorizons Discord community, which has more than 90,000 members. It is built on Red-DiscordBot and the NHCogs extensions.

  The application supports automatic detection and human review of suspicious messages and attachments, join verification, configured role restrictions, persistent roles on rejoin, role synchronization, achievements, activity summaries, custom commands, moderator message publishing, and GitHub pull-request review coordination. Moderation cases collect relevant context and selected evidence for authorized moderators. Review coordination uses the availability of volunteers who have enabled automatic notifications.

  The bot stores feature-specific identifiers, records, and selected content on its host. Image moderation uses deterministic image hashes, moderator-labelled reference examples, similarity comparisons, and a heuristic matching threshold. These references help identify recurring scam images and avoid false positives.

- **Internal rationale**

  This describes the purpose of the application before the individual intents. The community size comes from the operator's report. The feature list comes from NHCogs, but it must match the running bot. Remove inactive features and add relevant functionality from other loaded cogs. Simple message counting is not the main reason for Message Content access.

### 2. Public privacy policy

- **Field from the screenshot**

  Do you have a public Privacy Policy telling your users about their data usage?

- **Answer**

  **No**

- **Internal rationale**

  The repository has `end_user_data_statement` declarations in `NHCogs/info.json` and individual cog metadata. These are useful disclosures, but they do not confirm that a complete public policy has been published. No public policy URL was found in the original repository, and an external policy has not been confirmed.

  The current draft therefore selects No. If the administrator confirms a complete, accessible policy at `[PUBLIC_PRIVACY_POLICY_URL]`, change the answer to Yes before submission.

  The [privacy policy draft](privacy-policy-draft.md) needs the operator, `[PRIVATE_CONTACT]`, production configuration, storage safeguards, and retention details confirmed. Check that ordinary users can access the policy and the Red data-request commands, including `[p]mydata 3rdparty` and `[p]mydata forgetme`. Here, `[p]` means the bot's command prefix.

### 3. Requested privileged intents

- **Field from the screenshot**

  Which intents are you applying for, if any? (Leave blank if you do not need any of these)

  Checkboxes: Server Members Intent, Presence Intent, Message Content Intent.

- **Answer**

  Select **Server Members Intent**, **Presence Intent**, and **Message Content Intent** if all three corresponding feature groups described below are enabled in production.

- **Internal rationale**

  Server Members supports membership events, verification, and role management. Presence supports availability-based selection of opted-in reviewers. Message Content supports passive moderation of ordinary messages and attachments. Request only the intents the running application needs. If availability-based routing is disabled and no other confirmed feature needs Presence, do not select Presence solely because the code supports it.

## Screenshot 2: Server Members Intent

### 4. Guild Members justification

- **Field from the screenshot**

  Why do you need the Guild Members intent?

- **Answer**

  The application uses Guild Members events to handle joins, departures, and role changes. Configured join-verification checks can alert moderators and apply a verification role. Configured persistent roles are saved when a member leaves and restored on rejoin. Role tools maintain current assignments through full member synchronization when needed and lifecycle events thereafter.

  The pull-request review workflow also checks current membership and participant roles. Interactions provide information about the invoking member, but they do not replace member lifecycle events or a complete role index. These features need current membership and role information to apply the configured rules correctly.

- **Internal rationale**

  These are concrete uses of member data in NHCogs. The explanation connects the intent to verification, role restoration, synchronization, and authorization. It explains why information about a single interaction participant is insufficient. Include only functions enabled in production. The safety changes preserve the last valid role index when member data is unavailable or incomplete, rather than replacing it with an empty result.

### 5. Guild Members demonstration

- **Field from the screenshot**

  Please provide links to screenshots and/or videos that demonstrate your use case

- **Answer**

  `[MEMBERS_DEMO_URL]`

- **Internal rationale**

  Supply an accessible screenshot or video of the feature working in a Discord server. Show a consenting test account joining, the configured verification or role behavior, and a private moderator alert. If persistent roles are part of the justification, show a configured role restored after rejoining. Hide unrelated users and private case data. This URL is still missing.

### 6. Member API data stored outside Discord

- **Field from the screenshot**

  Are you storing any API Data off-platform (outside of Discord)?

- **Answer**

  **Yes**

- **Internal rationale**

  Enabled member and role features store guild, user, and role IDs, verification records, role state, and moderation history in local SQLite databases and Red configuration. Storage on the bot's own host is outside Discord. The policy must explain retention and data-request handling. Selecting Yes is not itself a compliance violation.

## Screenshot 3: Presence Intent

### 7. Guild Presences justification

- **Field from the screenshot**

  Why do you need the Guild Presences intent?

- **Answer**

  Our pull-request review workflow uses current availability to select an authorized volunteer who has enabled automatic notifications and chosen the ticket's expertise categories. It prioritizes Online, Idle, Do Not Disturb, and Offline reviewers and uses status to set the response deadline.

  The NHCogs routing feature reads current status from the Discord client's in-memory cache only after checking profile opt-in, categories, permissions, and ticket exclusions. It uses that status to select a reviewer and calculate the response deadline, without persisting the raw status value. It does not inspect game names, listening activity, or device platform, or build a continuous history of online time.

  The active ticket retains the selected reviewer, notification times, and response deadline needed for the workflow. Closing a ticket removes its local routing and history records. If Discord message or thread deletion must be retried, only the technical cleanup identifiers and retry metadata remain. Developer profiles and their notification preferences are separate and remain available. An optional completion log containing PR details and participant IDs remains on Discord.

  Users can disable automatic notifications or clear their developer profile to stop future automatic selections using their presence. Before a new automatic notification is sent, the bot checks eligibility again and cancels an unsent notification if the reviewer has opted out or no longer qualifies.

- **Internal rationale**

  Availability-based reviewer routing is the confirmed NHCogs use case for Presence. Current status is used briefly for selection and deadline calculation. Raw status values are not saved in pending notifications or ping history, and the migration clears values written by older versions. The operational deadline can still reflect the response window chosen from status, so do not claim that all status-derived information disappears immediately. Request Presence only if this feature is enabled, or another loaded cog has a separately confirmed need.

### 8. Guild Presences demonstration

- **Field from the screenshot**

  Please provide links to screenshots and/or videos that demonstrate your use case

- **Answer**

  `[PRESENCE_DEMO_URL]`

- **Internal rationale**

  Show consenting reviewers with matching categories and different statuses, the resulting automatic selection and response deadline, and an opted-out profile being excluded. Demonstrate the actual routing behavior on the running version. This URL is still missing.

### 9. Presence opt-out

- **Field from the screenshot**

  Can users opt-out of having their Presence data tracked?

- **Answer**

  **Yes**

- **Internal rationale**

  Disabling automatic notifications or clearing the profile stops future availability processing for automatic selection. Confirm deployment and check that no other loaded cog tracks presence without an opt-out before answering Yes for the whole application.

  The Yes answer is supported by the ability to stop future status use through the notification preference or profile removal. Brief use and automatic data cleanup are additional minimization measures, not a substitute for an opt-out.

  The routing feature does not persist raw status values. Active notification timing and deadlines remain until the ticket is closed. The Discord client still maintains its current Gateway cache in memory, and disabling this feature does not stop Gateway events from arriving. Do not claim that the entire bot clears every cached presence value after a ping.

### 10. User activity stored outside Discord

- **Field from the screenshot**

  Are you storing user activity data off-platform (outside of Discord)?

- **Answer**

  **Yes**

- **Internal rationale**

  Active local ticket records retain the selected reviewer, notification times, and the response deadline calculated from current availability. The raw status itself is not persisted, but these operational records still derive from presence processing. Separately enabled activity features also keep user-linked message counts on the bot's host. The Yes answer therefore remains appropriate. The Presence justification should remain about reviewer availability, rather than implying that message counts require Presence.

## Screenshot 4: Message Content Intent

### 11. Message content opt-out

- **Field from the screenshot**

  Can users opt-out of having their message content data tracked?

- **Answer**

  **No**

- **Internal rationale**

  No general per-user exemption from configured moderation was confirmed in NHCogs. Removing stored optional data is different from opting out of future moderation. Do not promise that a user can exempt their messages from detection or erase necessary moderation records through a deletion request. Describe the actual handling of reasons, evidence, restrictions, and reference samples in the policy. The No answer alone does not establish a compliance violation.

### 12. Message content stored outside Discord

- **Field from the screenshot**

  Are you storing message content data off-platform (outside of Discord)?

- **Answer**

  **Yes**

- **Internal rationale**

  Depending on enabled features, local storage contains selected moderation evidence and image references, custom-command responses, and published Bot Proxy message content and versions.

  The message registry keeps identifiers, timestamps, pin state, author metadata, and optional one-way fingerprints for 14 days. It does not store raw text or attachments. The intent-loss backlog likewise stores only message, channel, and guild IDs with times and retry state. These limited stores do not change the Yes answer for other retained content, and their 14-day limit does not apply to every dataset.

### 13. Machine learning or AI training

- **Field from the screenshot**

  Will the message content data be used to train machine learning or AI Models?

  If yes, please explain how in the text box below, including how such models would be used.

- **Answer**

  **No**

- **Internal rationale**

  This answer is based on the reviewed NHCogs implementation and the operator's stated use. Confirm that no other loaded cog or operator process trains models on Discord message data. The operator states that message data is not used for AI training. The reviewed image detector computes SHA-256 and perceptual hashes, compares images with moderator-labelled scam and false-positive references, and uses a heuristic matching threshold. That threshold includes calibration from distances between reference examples. No LLM or neural-network training pipeline was found.

  Hashing alone is not evidence of AI training. Keep the threshold-calibration description accurate. This answer does not settle how Discord classifies every form of adaptive calibration. If Discord asks about that distinction, obtain a specific determination. Check other loaded cogs and any use of exported data before answering for the whole application. Selecting Yes would not itself grant training permission.

  The conditional explanation applies when Yes is selected. If Discord separately requests technical clarification, use this description:

  > Our local image-moderation feature computes SHA-256 and perceptual hashes and compares incoming images with moderator-labelled scam and false-positive references. It uses hash distances and a heuristic threshold, including calibration from those reference distances. We retain the reference set to recognize recurring scam images and avoid false positives.

### 14. Message Content justification

- **Field from the screenshot**

  Why do you need the Message Content intent?

- **Answer**

  The application passively moderates ordinary server messages and attachments. Configured detectors inspect content and images, identify suspicious messages or animated attachments, and create moderation cases with relevant context for authorized human review.

  These attachment and review workflows go beyond ordinary AutoMod keyword rules. They require messages that do not mention the bot and have not first been selected through an interaction. Slash commands and message context menus support explicitly requested actions, but they cannot provide every unreported message needed by the automatic moderation workflow.

  Custom prefix commands are a secondary use. Simple message counting is not the basis for this request.

- **Internal rationale**

  The main justification is passive moderation of ordinary content and attachments, with relevant evidence and human review. It explains why interactions, bot mentions, and standard AutoMod keyword rules do not replace these workflows. Include only detectors enabled in production. Do not justify Message Content through bulk research collection. The existing research export requires its own purpose and retention review.

### 15. Message Content demonstration

- **Field from the screenshot**

  Please provide links to screenshots and/or videos that demonstrate your use case

- **Answer**

  `[MESSAGE_CONTENT_DEMO_URL]`

- **Internal rationale**

  Show a safe, prepared test message without a bot mention, automatic detection, a private review case, and a moderator decision. Use consenting test accounts and fabricated evidence. Hide unrelated private information and real victims' content. Demonstrate the deployed feature. This URL is still missing.

## Internal checks before submission

- Confirm the Application ID, actual server installs, loaded cogs, production version, enabled features, and requested intents
- Confirm deployment of the reviewed data controls and opt-out checks from PR #149
- Confirm deployment of raw-presence removal and immediate local ticket-data cleanup before using the updated Presence explanation
- Confirm that the public policy is complete, accessible, and configured in Developer Portal
- Confirm that the data-request commands work for ordinary users and supply the administrator's private contact
- Confirm hosting, backups, retained exports, and the descriptions of moderation evidence and reference retention
- Supply all three demonstration links and replace the policy and contact placeholders
- Compare every answer with the running application, including other loaded cogs
- Have the administrator review the final text before submission

The supplied Portal notice shows `Request due: 09/10/2026` and `0 days`. It gives no hour or time zone. Do not infer a confirmed local or UTC midnight cutoff from it.

## Screenshot evidence checklist

Screenshots are allowed. The form asks for links to screenshots and/or videos. Provide accessible links that show the feature working in Discord.

- **Server Members:** the supplied join and CAPTCHA collage shows a new-account alert, containment role, and successful verification with role removal
- **Presence:** the supplied ticket and profile collage shows automatic reviewer selection and the profile setting that controls automatic notifications. The application answer explains how current availability affects selection and response timing
- **Message Content:** the supplied detection collage shows NHBot's detection signal, copied attachment evidence, and a moderator decision

These three images cover the requested use cases. The captions only need to explain the workflow clearly. Replace the three demonstration URL placeholders with accessible links to the images.

Related files: [technical audit](discord-intents-audit.md), [privacy policy draft](privacy-policy-draft.md), [intent-loss behavior](intent-loss-runbook.md).

Official references: [privileged intent review](https://docs.discord.com/developers/gateway/getting-started-with-privileged-intent-review), [Gateway intents](https://docs.discord.com/developers/events/gateway#privileged-intents).
