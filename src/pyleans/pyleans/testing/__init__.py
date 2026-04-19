"""Test-support utilities for pyleans consumers.

Ships functional in-process doubles (membership, storage, streaming) so
every workspace that depends on pyleans can share the same fixtures
instead of copying adapter implementations across test roots.
"""

from pyleans.testing.membership_fake import InMemoryMembershipProvider

__all__ = ["InMemoryMembershipProvider"]
