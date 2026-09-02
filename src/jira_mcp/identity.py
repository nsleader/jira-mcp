"""User identity: the login ↔ user-key pair behind every Tempo call.

A Jira Server/DC user has two stable-ish handles: the *login* (``acutina``,
which changes when the person is renamed) and the *user key* (``JIRAUSER18613``,
which never changes). Jira 6.0 and earlier made the key equal to the login, so
long-serving accounts look identical either way; everyone hired since gets a
``JIRAUSER…`` key that has nothing to do with their login.

The two Tempo APIs this server talks to disagree about which one they want:

- Tempo **Timesheets** filters worklogs by ``worker``, which is the *key*.
- Tempo **Planner** filters allocations by ``assigneeKeys`` and reads schedules
  by ``user``, both of which are the *login*.

Neither rejects the other's handle — they match nothing and answer ``0``. That
is the whole reason this module exists: a wrong handle here is not an error, it
is a confident, plausible, wrong report ("Кутина logged 0 hours this week").
So the tools take whatever the caller has and resolve it here, once, against
``rest/api/2/user``.

Resolution is cached for the life of the process: logins and keys change about
as often as people are renamed, and a report over a team otherwise pays one
extra request per member.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .client import get_jira

# The shape Jira 6.1+ mints for a new account. Old accounts keep key == login,
# which is exactly why we probe both orders instead of trusting the pattern.
_KEY_RE = re.compile(r"^JIRAUSER\d+$", re.IGNORECASE)


@dataclass(frozen=True)
class Identity:
    """Both handles of one Jira user, plus a name worth printing."""

    login: str
    key: str
    display: str | None = None


def _lookup(params: dict[str, str]) -> dict[str, Any] | None:
    """One ``rest/api/2/user`` probe; ``None`` for anything but a real user.

    A miss is the normal case here (we probe by key *and* by login), and Jira
    answers it with 404 → ``HTTPError``. The client also returns a plain string
    when it dislikes the parameter combination, hence the shape check.
    """
    try:
        user = get_jira().user(**params)
    except Exception:
        return None
    return user if isinstance(user, dict) and user.get("key") else None


@lru_cache(maxsize=1024)
def resolve(user: str) -> Identity | None:
    """Resolve a login *or* a user key into both handles; ``None`` if unknown.

    Probes the likelier parameter first, then the other one: a ``JIRAUSER…``
    string is almost certainly a key, anything else almost certainly a login —
    but "almost" is not "certainly" while old accounts have key == login.
    """
    by_key = {"key": user}
    by_login = {"username": user}
    order = [by_key, by_login] if _KEY_RE.match(user) else [by_login, by_key]
    for params in order:
        found = _lookup(params)
        if found:
            return Identity(
                login=str(found.get("name") or user),
                key=str(found["key"]),
                display=found.get("displayName") or found.get("displayname"),
            )
    return None


def worker_key(user: str) -> str:
    """The handle Tempo Timesheets filters worklogs by (``worker``).

    Unresolvable input is passed through untouched: Tempo then answers what it
    would have answered anyway, and the caller is no worse off than before.
    """
    identity = resolve(user)
    return identity.key if identity else user


def planner_login(user: str) -> str:
    """The handle Tempo Planner filters allocations and schedules by."""
    identity = resolve(user)
    return identity.login if identity else user


def display_name(user: str) -> str | None:
    """Readable name for a login or key, when Jira knows the account."""
    identity = resolve(user)
    return identity.display if identity else None


# Said out loud in the result, because the alternative is worse than an error:
# Tempo answers an unmatched filter with zero, and "0 hours" reads as a fact
# about the person rather than a fact about the handle we sent.
_UNKNOWN_WARNING = (
    "no Jira account matches {users} — an empty result here means the handle is "
    "wrong, not that the person logged/planned nothing. These tools take a login "
    "('acutina') or a user key ('JIRAUSER18613'); a display name ('Кутина "
    "Анастасия') is neither — resolve it with search_users first."
)


def unknown_users_warning(users: list[str]) -> str | None:
    """Warning text for inputs Jira knows no account for, or ``None`` if all resolve."""
    unknown = [u for u in users if resolve(u) is None]
    if not unknown:
        return None
    return _UNKNOWN_WARNING.format(users=", ".join(f"'{u}'" for u in unknown))
