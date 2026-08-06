"""Per-tenant enforcement points. Default is unlimited.

RF-073 asks for the enforcement points to exist in code with an unlimited
default, so that the day a plan rule is defined it is an integration and not a
rewrite. Decision D9 of the E3 plan draws the line: the `quota_rules` table of
`dicionario-de-dados §7.2` is deliberately NOT created — a placeholder table
nobody tests is worse than no table — while the *call*, made from the protection
ladder before every proactive touch, is created now and covered by a test that
asserts it is made. An enforcement point nobody exercises rots in silence.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Allowance:
    """What the tenant's plan still permits.

    `None` on a dimension means unlimited, which is every dimension by default:
    there is no plan rule yet, and the absence of one has to read as "yes",
    never as "zero". New dimensions (concurrency, features — RF-073) are added
    here as further optional fields; the ladder's call site does not change.
    """

    proactive_touches_remaining: int | None = None


def has_headroom(allowance: Allowance) -> bool:
    """Does this tenant's plan still allow one more proactive touch?

    Not `> 0` on a bare number by accident: a counter that overshot into the
    negatives is an exhausted plan, not an unlimited one.
    """
    remaining = allowance.proactive_touches_remaining
    return remaining is None or remaining > 0
