# NHBot privacy policy

Last updated: 9 October 2026

NHBot supports moderation and community features in the NewHorizons Discord server. This policy covers its NHCogs features, depending on which are enabled.

**Privacy contact:** [gregtech.newhorizons@gmail.com](mailto:gregtech.newhorizons@gmail.com)

## Data we use

Records may include Discord user, server, channel, message, and role IDs, usernames, and timestamps.

| Feature | Data and purpose |
| --- | --- |
| Verification and roles | Account details, membership, roles, and verification results to check new members and manage access |
| Moderation | Relevant messages, attachments, account details, reasons for actions, and moderation history to detect abuse, review evidence, enforce restrictions, and support appeals |
| Activity and achievements | Message counts, achievement records, and role awards for community statistics and rewards |
| Custom commands and character messages | Command responses, character names and avatars, published text, edit history, and contributor details to manage shared server content |
| Review tickets | Developer profiles, optional GitHub usernames, expertise categories, notification preferences, PR details, and reviewer records to coordinate reviews |
| Diagnostics | Error logs and relevant Discord identifiers to investigate failures and recover interrupted work |

Image detection compares images with stored scam and false-positive examples. It does not train language models or neural networks on Discord content.

## Reviewer availability

Automatic reviewer selection uses the current Discord status of eligible volunteers to choose a reviewer and set a response deadline. Ticket records retain the notification and deadline, but do not save the status itself or a history of online time.

Disable **Allow automatic pings** or clear your developer profile to stop this selection process for you. A notification already being sent may still complete.

## Storage and access

Data is stored on the computer or server running the bot. Access to files on that machine is restricted to administrators and authorized staff. Staff use moderation and export tools according to their permissions. Messages and reports posted to Discord are visible to people with access to their channels or threads.

Staff can export selected channels' full available history for offline analysis, including message text, author details, and attachment links.

Discord handles data on its platform under its own [privacy policy](https://discord.com/privacy).

## Retention

- Temporary message IDs and comparison data used to detect repeated spam are kept for up to 14 days. These records do not contain message text or attachments
- Detailed activity counts are kept for the configured period. Older summaries contain totals without user IDs
- Moderation and verification history, and image-reference samples, have no automatic expiry
- Local case attachment files are removed during resolution cleanup. Copies posted to Discord and separately saved image references can remain
- Local ticket data is removed when a ticket closes. Developer profiles remain until cleared or deleted, and completion logs on Discord can remain
- Shared commands, published messages, and their versions remain until removed through the relevant feature or reviewed individually
- Temporary diagnostic logs are kept for up to 24 hours. Exported logs and other copies are handled separately

## Your choices and data requests

Use `!mydata 3rdparty` to read the bot's data statements and `!mydata forgetme` to request deletion. Follow the prompts.

Contact [gregtech.newhorizons@gmail.com](mailto:gregtech.newhorizons@gmail.com) for corrections, questions, or requests needing manual review. Include your Discord user ID and the records concerned. Keep private evidence out of public channels.

Automated deletion removes optional personal records, detailed activity, saved roles, and achievement history. It removes your author or editor attribution and deletes character profiles and avatars you created. Shared or published content can remain and may need manual review if it contains personal information.

It also removes your developer profile, closes tickets you authored, and removes your reviewer links from other tickets.

Automated requests do not remove moderation reasons, evidence, verification history, image references, active restrictions, or records needed to complete pending actions. These retained records can be raised for individual review under applicable law and Discord's Developer Terms. A data request does not itself remove a ban or exempt future messages from moderation.

Automated deletion does not erase earlier exports or Discord copies. Contact us about those copies. New activity can create new records after a request.

Updates to this policy will appear here with a revised date.
