from __future__ import annotations

from collections.abc import Sequence

DISCORD_MESSAGE_LIMIT = 2_000
DISCORD_EMBED_DESCRIPTION_LIMIT = 4_096
MAX_PR_TITLE_LENGTH = 256
MAX_PR_URL_LENGTH = 1_024
MAX_GITHUB_USERNAME_LENGTH = 100
_MAX_USER_MENTION = "<@18446744073709551615>"

NEW_TICKET_COMMAND = "newticket"
NEW_TICKET_COMMAND_DESCRIPTION = "Create a new GitHub ticket"
DEVELOPER_PROFILE_SLASH_COMMAND = "developerprofile"
DEVELOPER_PROFILE_SLASH_DESCRIPTION = "Manage your developer profile"
DEVELOPER_PROFILE_COMMAND = "Developer Profile"
DASHBOARD_TITLE = "GitHub Tickets"
NEW_TICKET = "New Ticket"
EDIT_PROFILE = "Edit Profile"
BROWSE_CATEGORIES = "Browse Categories"
FIND_BY_GITHUB_USERNAME = "Find by GitHub username"
CLEAR_PROFILE = "Clear Profile"
GITHUB_USERNAME = "GitHub username"
GITHUB_USERNAME_DESCRIPTION = "Leave empty if it matches your Discord name"
ENTER_GITHUB_USERNAME = "Enter a GitHub username"
CATEGORIES = "Categories"
SELECT_YOUR_CATEGORIES = "Select your categories"
ALLOW_AUTOMATIC_PINGS = "Allow automatic pings"
ARE_YOU_SURE = "Are you sure?"
NO_PROFILE = "No profile"
SELECT_A_CATEGORY = "Select a category"
NO_USERS_FOUND = "No users found"
NO_CATEGORIES_CONFIGURED = "No categories configured"
PREVIOUS = "Previous"
NEXT = "Next"
BACK = "Back"
CONFIRM_CATEGORIES = "Confirm categories"
CREATE_TICKET = "Create Ticket"
PR_TITLE = "PR title"
ENTER_PR_TITLE = "Enter the PR title"
PR_LINK = "PR link"
ENTER_PR_LINK = "Enter the PR link"
SELECT_CATEGORIES = "Select categories"
PING_BEHAVIOR = "Ping behavior"
SELECT_PING_BEHAVIOR = "Select ping behavior"
DIRECT_REVIEWER = "Direct reviewer"
DIRECT_REVIEWER_DESCRIPTION = "Ignored unless a direct ping option is selected"
SELECT_A_REVIEWER = "Select a reviewer"
NO_PING = "No ping"
AUTOMATIC = "Automatic"
DIRECT_THEN_WAIT = "Direct, then wait"
DIRECT_THEN_AUTOMATIC = "Direct, then automatic"
MARK_FINISHED = "Mark Finished"
CLAIM = "Claim"
DECLINE = "Decline"
UNASSIGN = "Unassign"
TICKET_CHANNEL_NOT_CONFIGURED = "Ticket channel is not configured"
CANNOT_USE_ACTION = "You cannot use this action"
TICKET_NOT_ACTIVE = "This ticket is no longer active"
TICKET_ALREADY_CLAIMED = "This ticket has already been claimed"
AUTOMATIC_REQUIRES_CATEGORY = "Select at least one category for automatic pings"
DIRECT_REQUIRES_REVIEWER = "Select a reviewer for direct pings"
CATEGORY_NO_LONGER_EXISTS = "This category no longer exists"
COULD_NOT_CREATE_TICKET = "Could not create the ticket"
COULD_NOT_COMPLETE_ACTION = "Could not complete this action"
TICKET_CHANNEL_CLEARED = "Ticket channel cleared"
TICKET_CHANNEL_MUST_BE_TEXT = "Ticket channel must be a text channel"
LOG_CHANNEL_CLEARED = "Log channel cleared"
ROLE_ALREADY_CONFIGURED = "Participant role is already configured"
ROLE_NOT_CONFIGURED = "Participant role is not configured"
CATEGORY_NAME_EMPTY = "Category name cannot be empty"
CATEGORY_NAME_TOO_LONG = "Category name cannot exceed 100 characters"
CATEGORY_ALREADY_EXISTS = "Category already exists"
CATEGORY_NOT_FOUND = "Category not found"
CATEGORY_LIMIT_REACHED = "Category limit reached"
MAXIMUM_PINGS_NEGATIVE = "Maximum pings cannot be negative"
INVALID_DURATION = "Invalid duration"
DURATION_NEGATIVE = "Duration cannot be negative"
INVALID_USER_ID = "Invalid user ID"

HELP_COPY = (
    "Configure GitHub Tickets",
    "Configure the ticket channel",
    "Set the ticket channel",
    "Clear the ticket channel",
    "Configure the log channel",
    "Set the log channel",
    "Clear the log channel",
    "Configure participant roles",
    "Add a participant role",
    "Remove a participant role",
    "Configure categories",
    "Add a category",
    "Rename a category",
    "Remove a category",
    "Set the maximum pings per ticket",
    "Configure ticket timing",
    "Set the protection period",
    "Set the initial volunteer window",
    "Set the Online response time",
    "Set the Idle response time",
    "Set the Do Not Disturb response time",
    "Set the Offline response time",
    "Set the direct response time",
    "Manage developer profiles",
    "Clear a developer profile",
)

FIXED_COPY = (
    NEW_TICKET_COMMAND_DESCRIPTION,
    DEVELOPER_PROFILE_SLASH_DESCRIPTION,
    DEVELOPER_PROFILE_COMMAND,
    DASHBOARD_TITLE,
    NEW_TICKET,
    EDIT_PROFILE,
    BROWSE_CATEGORIES,
    CLEAR_PROFILE,
    GITHUB_USERNAME,
    GITHUB_USERNAME_DESCRIPTION,
    CATEGORIES,
    SELECT_YOUR_CATEGORIES,
    ALLOW_AUTOMATIC_PINGS,
    ARE_YOU_SURE,
    NO_PROFILE,
    SELECT_A_CATEGORY,
    NO_USERS_FOUND,
    NO_CATEGORIES_CONFIGURED,
    PREVIOUS,
    NEXT,
    BACK,
    PR_TITLE,
    ENTER_PR_TITLE,
    PR_LINK,
    ENTER_PR_LINK,
    SELECT_CATEGORIES,
    PING_BEHAVIOR,
    SELECT_PING_BEHAVIOR,
    DIRECT_REVIEWER,
    DIRECT_REVIEWER_DESCRIPTION,
    SELECT_A_REVIEWER,
    NO_PING,
    AUTOMATIC,
    DIRECT_THEN_WAIT,
    DIRECT_THEN_AUTOMATIC,
    MARK_FINISHED,
    CLAIM,
    DECLINE,
    UNASSIGN,
    TICKET_CHANNEL_NOT_CONFIGURED,
    CANNOT_USE_ACTION,
    TICKET_NOT_ACTIVE,
    TICKET_ALREADY_CLAIMED,
    AUTOMATIC_REQUIRES_CATEGORY,
    DIRECT_REQUIRES_REVIEWER,
    CATEGORY_NO_LONGER_EXISTS,
    COULD_NOT_CREATE_TICKET,
    COULD_NOT_COMPLETE_ACTION,
    TICKET_CHANNEL_CLEARED,
    TICKET_CHANNEL_MUST_BE_TEXT,
    ROLE_ALREADY_CONFIGURED,
    ROLE_NOT_CONFIGURED,
    CATEGORY_NAME_EMPTY,
    CATEGORY_NAME_TOO_LONG,
    CATEGORY_ALREADY_EXISTS,
    CATEGORY_NOT_FOUND,
    CATEGORY_LIMIT_REACHED,
    MAXIMUM_PINGS_NEGATIVE,
    INVALID_DURATION,
    DURATION_NEGATIVE,
    INVALID_USER_ID,
    *HELP_COPY,
)


def confirm_categories(candidate_count: int) -> str:
    if candidate_count == 0:
        detail = "No one can receive automatic pings for all selected categories"
    elif candidate_count == 1:
        detail = "1 person can receive automatic pings for all selected categories"
    else:
        detail = (
            f"{candidate_count} people can receive automatic pings for all selected categories"
        )
    return f"{CONFIRM_CATEGORIES}\n{detail}"


def _linked_title(title: str, url: str) -> str:
    return f"[{title}](<{url}>)"


def ticket_message(
    *,
    title: str,
    url: str,
    author_mention: str,
    categories: Sequence[str] = (),
    reviewer_mention: str | None = None,
    reviewer_github: str | None = None,
) -> str:
    metadata = [f"Author: {author_mention}"]
    if categories:
        metadata.append(", ".join(categories))
    if reviewer_mention is not None:
        reviewer = f"Reviewer: {reviewer_mention}"
        if reviewer_github:
            reviewer = f"{reviewer} | {reviewer_github}"
        metadata.append(reviewer)
    return f"{_linked_title(title, url)}\n{' | '.join(metadata)}"


def finished_ticket_log(
    *,
    title: str,
    url: str,
    actor_id: int,
    author_id: int,
    reviewer_id: int | None,
) -> str:
    metadata = [f"Finished by <@{actor_id}>", f"Author <@{author_id}>"]
    if reviewer_id is not None:
        metadata.append(f"Reviewer <@{reviewer_id}>")
    return f"{_linked_title(title, url)}\n{' | '.join(metadata)}"


def ticket_category_selection_limit(categories: Sequence[str]) -> int:
    if not categories:
        return 1
    selected: list[str] = []
    for category in sorted(categories, key=len, reverse=True):
        candidate = [*selected, category]
        content = ticket_message(
            title="x" * MAX_PR_TITLE_LENGTH,
            url="x" * MAX_PR_URL_LENGTH,
            author_mention=_MAX_USER_MENTION,
            categories=candidate,
            reviewer_mention=_MAX_USER_MENTION,
            reviewer_github="x" * MAX_GITHUB_USERNAME_LENGTH,
        )
        if len(content) > DISCORD_MESSAGE_LIMIT:
            break
        selected.append(category)
    return max(1, len(selected))


def direct_review_notification(target_mention: str) -> str:
    return f"{target_mention} was directly requested for review"


def automatic_review_notification(target_mention: str) -> str:
    return f"{target_mention} was automatically selected for review"


def developer_profile(
    *,
    mention: str,
    has_profile: bool,
    github_username: str | None = None,
    categories: Sequence[str] = (),
) -> str:
    if not has_profile:
        return NO_PROFILE
    first_line = mention
    if github_username:
        first_line = f"{first_line} | {github_username}"
    if not categories:
        return first_line
    return f"{first_line}\n{', '.join(categories)}"


def message_chunks(
    content: str,
    *,
    limit: int = DISCORD_MESSAGE_LIMIT,
) -> tuple[str, ...]:
    if not content:
        return (content,)
    chunks: list[str] = []
    remaining = content
    separators = (("\n", 1), (", ", 2), (" ", 1))
    while len(remaining) > limit:
        cut = 0
        for separator, width in separators:
            position = remaining.rfind(separator, 0, limit + 1)
            if position > 0:
                cut = position + width
                break
        if cut == 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def category_page(
    *,
    category: str,
    users: Sequence[str],
    page: int,
    page_count: int,
) -> str:
    lines = [category]
    if users:
        lines.extend(users)
        if page_count > 1:
            lines.append(f"Page {page} of {page_count}")
    else:
        lines.append(NO_USERS_FOUND)
    return "\n".join(lines)


def thread_name(title: str) -> str:
    return title[:100]


def duration_text(seconds: int) -> str:
    if seconds and seconds % 3600 == 0:
        value = seconds // 3600
        unit = "hour" if value == 1 else "hours"
    elif seconds and seconds % 60 == 0:
        value = seconds // 60
        unit = "minute" if value == 1 else "minutes"
    else:
        value = seconds
        unit = "second" if value == 1 else "seconds"
    return f"{value} {unit}"


def configuration_overview(
    *,
    ticket_channel: str | None,
    log_channel: str | None,
    participant_roles: Sequence[str],
    categories: Sequence[str],
    max_pings: int,
    protection_seconds: int,
    volunteer_seconds: int,
    online_response_seconds: int,
    idle_response_seconds: int,
    dnd_response_seconds: int,
    offline_response_seconds: int,
    direct_response_seconds: int,
) -> str:
    channel = ticket_channel or "Not set"
    log = log_channel or "Not set"
    roles = ", ".join(participant_roles) if participant_roles else "None"
    category_text = ", ".join(categories) if categories else "None"
    lines = (
        f"Ticket channel: {channel}",
        f"Log channel: {log}",
        f"Participant roles: {roles}",
        f"Categories: {category_text}",
        f"Maximum pings: {max_pings}",
        f"Protection period: {duration_text(protection_seconds)}",
        f"Initial volunteer window: {duration_text(volunteer_seconds)}",
        f"Online response time: {duration_text(online_response_seconds)}",
        f"Idle response time: {duration_text(idle_response_seconds)}",
        f"Do Not Disturb response time: {duration_text(dnd_response_seconds)}",
        f"Offline response time: {duration_text(offline_response_seconds)}",
        f"Direct response time: {duration_text(direct_response_seconds)}",
    )
    return "\n".join(lines)


def github_integration_overview(
    *,
    enabled: bool,
    organization: str | None,
    receiver: str | None,
    credentials_available: bool,
    running: bool,
    recovery_seconds: int,
) -> str:
    return "\n".join(
        (
            f"Enabled: {'Yes' if enabled else 'No'}",
            f"Organization: {organization or 'Not configured'}",
            f"Receiver: {receiver or 'Not configured'}",
            f"Credentials: {'Available' if credentials_available else 'Not configured'}",
            f"Runtime: {'Running' if running else 'Stopped'}",
            f"Recovery interval: {duration_text(recovery_seconds)}",
        )
    )


def ticket_channel_set(channel_mention: str) -> str:
    return f"Ticket channel set to {channel_mention}"


def log_channel_set(channel_mention: str) -> str:
    return f"Log channel set to {channel_mention}"


def participant_role_added(role_mention: str) -> str:
    return f"Participant role added: {role_mention}"


def participant_role_removed(role_mention: str) -> str:
    return f"Participant role removed: {role_mention}"


def category_added(category: str) -> str:
    return f"Category added: {category}"


def category_renamed(old_name: str, new_name: str) -> str:
    return f"Category renamed from {old_name} to {new_name}"


def category_removed(category: str) -> str:
    return f"Category removed: {category}"


def maximum_pings_set(count: int) -> str:
    return f"Maximum pings set to {count}"


def timing_set(label: str, seconds: int) -> str:
    return f"{label} set to {duration_text(seconds)}"


def profile_cleared(user_id: int) -> str:
    return f"Profile cleared: {user_id}"
