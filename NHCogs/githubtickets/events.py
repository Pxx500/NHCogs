from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias, cast

from .models import GitHubDelivery, GitHubPullRequest

PullRequestAction: TypeAlias = Literal[
    "labeled",
    "ready_for_review",
    "opened",
    "reopened",
    "unlabeled",
    "edited",
    "synchronize",
    "converted_to_draft",
    "closed",
    "assigned",
    "unassigned",
]


class InvalidGitHubDelivery(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PullRequestEvent:
    action: PullRequestAction
    pull_request: GitHubPullRequest
    label: str | None = None
    assignee_login: str | None = None
    assignee_logins: tuple[str, ...] = ()
    title_changed: bool = False


@dataclass(frozen=True, slots=True)
class PullRequestReviewEvent:
    pull_request: GitHubPullRequest
    state: str
    reviewer_login: str | None
    assignee_logins: tuple[str, ...] = ()


ParsedGitHubEvent: TypeAlias = PullRequestEvent | PullRequestReviewEvent

_PULL_REQUEST_ACTIONS = frozenset(
    {
        "labeled",
        "ready_for_review",
        "opened",
        "reopened",
        "unlabeled",
        "edited",
        "synchronize",
        "converted_to_draft",
        "closed",
        "assigned",
        "unassigned",
    }
)
_INVALID_MESSAGE = "GitHub delivery payload is invalid"


def parse_delivery(delivery: GitHubDelivery) -> ParsedGitHubEvent | None:
    if delivery.event == "pull_request":
        if delivery.action not in _PULL_REQUEST_ACTIONS:
            return None
        try:
            return _parse_pull_request_event(delivery)
        except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError, ValueError):
            raise InvalidGitHubDelivery(_INVALID_MESSAGE) from None
    if delivery.event == "pull_request_review":
        if delivery.action != "submitted":
            return None
        try:
            return _parse_review_event(delivery)
        except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError, ValueError):
            raise InvalidGitHubDelivery(_INVALID_MESSAGE) from None
    return None


def _parse_pull_request_event(delivery: GitHubDelivery) -> PullRequestEvent:
    payload = _payload(delivery)
    action = _delivery_action(payload, delivery)
    pull_request = _pull_request(payload, action)
    label = None
    if action in {"labeled", "unlabeled"}:
        label_payload = _optional_mapping(payload, "label")
        if label_payload is not None:
            label = _string(label_payload, "name")
    assignee_login = None
    if action in {"assigned", "unassigned"}:
        assignee = _optional_mapping(payload, "assignee")
        if assignee is not None:
            assignee_login = _string(assignee, "login")
    changes = _optional_mapping(payload, "changes")
    return PullRequestEvent(
        action=cast(PullRequestAction, action),
        pull_request=pull_request,
        label=label,
        assignee_login=assignee_login,
        assignee_logins=_assignee_logins(payload),
        title_changed=action == "edited" and changes is not None and "title" in changes,
    )


def _parse_review_event(delivery: GitHubDelivery) -> PullRequestReviewEvent:
    payload = _payload(delivery)
    action = _delivery_action(payload, delivery)
    review = _mapping(payload, "review")
    reviewer = _optional_mapping(review, "user")
    state = _string(review, "state").strip().casefold()
    if not state:
        raise ValueError
    return PullRequestReviewEvent(
        pull_request=_pull_request(payload, action),
        state=state,
        reviewer_login=_string(reviewer, "login") if reviewer is not None else None,
        assignee_logins=_assignee_logins(payload),
    )


def _delivery_action(
    payload: Mapping[str, object],
    delivery: GitHubDelivery,
) -> str:
    action = _string(payload, "action")
    if action != delivery.action:
        raise ValueError
    return action


def _payload(delivery: GitHubDelivery) -> Mapping[str, object]:
    raw_body = delivery.raw_body
    if raw_body is None:
        raise ValueError
    payload = json.loads(raw_body)
    if not isinstance(payload, Mapping):
        raise TypeError
    return payload


def _pull_request(
    payload: Mapping[str, object],
    action: str,
) -> GitHubPullRequest:
    repository = _mapping(payload, "repository")
    pull_request = _mapping(payload, "pull_request")
    author = _mapping(pull_request, "user")
    state = _string(pull_request, "state").casefold()
    if state not in {"open", "closed"}:
        raise ValueError
    repository_full_name = _string(repository, "full_name").strip()
    owner, separator, repository_name = repository_full_name.partition("/")
    if not separator or not owner or not repository_name or "/" in repository_name:
        raise ValueError
    draft = _optional_bool(pull_request, "draft", default=False)
    _ = _optional_bool(pull_request, "merged", default=False)
    labels = tuple(
        _string(_mapping_value(label), "name") for label in _sequence(pull_request, "labels")
    )
    return GitHubPullRequest(
        repository_id=_positive_integer(repository, "id"),
        pr_number=_positive_integer(pull_request, "number"),
        github_pr_id=_positive_integer(pull_request, "id"),
        github_author_id=_positive_integer(author, "id"),
        repository_full_name=repository_full_name,
        url=_string(pull_request, "html_url"),
        title=_string(pull_request, "title", allow_empty=True),
        github_author_login=_string(author, "login"),
        draft=draft,
        open=state == "open",
        labels=labels,
        github_updated_at=_datetime(pull_request, "updated_at"),
        last_processed_action=action,
    )


def _assignee_logins(payload: Mapping[str, object]) -> tuple[str, ...]:
    pull_request = _mapping(payload, "pull_request")
    assignees = pull_request.get("assignees")
    if assignees is None:
        return ()
    if not isinstance(assignees, Sequence) or isinstance(
        assignees,
        (str, bytes, bytearray),
    ):
        return ()
    logins: list[str] = []
    for assignee in assignees:
        if not isinstance(assignee, Mapping):
            continue
        login = assignee.get("login")
        if isinstance(login, str) and login:
            logins.append(login)
    return tuple(logins)


def _mapping(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    return _mapping_value(parent[key])


def _mapping_value(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError
    return value


def _optional_mapping(
    parent: Mapping[str, object],
    key: str,
) -> Mapping[str, object] | None:
    value = parent.get(key)
    if value is None:
        return None
    return _mapping_value(value)


def _sequence(parent: Mapping[str, object], key: str) -> Sequence[object]:
    value = parent[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError
    return value


def _string(
    parent: Mapping[str, object],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = parent[key]
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError
    return value


def _positive_integer(parent: Mapping[str, object], key: str) -> int:
    value = parent[key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError
    return value


def _optional_bool(
    parent: Mapping[str, object],
    key: str,
    *,
    default: bool,
) -> bool:
    value = parent.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TypeError
    return value


def _datetime(parent: Mapping[str, object], key: str) -> datetime:
    value = _string(parent, key)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed
