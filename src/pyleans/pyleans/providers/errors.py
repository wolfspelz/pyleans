"""Membership-provider error hierarchy.

Distinct exception types let the failure detector (task-02-11) discriminate
"the table itself is down" from "a peer row is down" and react
differently — the former means wait, the latter means vote.
"""

from __future__ import annotations

from pyleans.errors import MembershipError


class TableStaleError(MembershipError):
    """Optimistic-concurrency check failed; caller should re-read and retry."""


class SiloNotFoundError(MembershipError):
    """Target silo row does not exist in the membership table."""


class MembershipUnavailableError(MembershipError):
    """Membership table cannot be reached.

    Per Orleans §3.9 ("accuracy > completeness"), the silo continues
    operating against its last-known view rather than cascading a
    suspicion when the table itself is unreachable.
    """


__all__ = [
    "MembershipError",
    "MembershipUnavailableError",
    "SiloNotFoundError",
    "TableStaleError",
]
