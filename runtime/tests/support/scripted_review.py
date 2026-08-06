"""The reviewer the engine scenarios declare, next to the constant reply.

`AGENTS_REVIEWER` is required of the process, so the kill harness has to name
one — the same move `constant_reply` made when the responder became required.

This double judges nothing, and that is deliberate rather than lazy:

  * cenário 1 asserts that a burst of five messages costs **exactly one** LLM
    call. An auditor billing a second call per reply would answer that count
    with noise from a seam the scenario is not about;
  * the real reviewer is exercised where it belongs — in-process, against a
    scripted LLM, in `tests/pipeline/test_scenario_post_hoc.py`, and unit by
    unit in `tests/unit/test_evals_handler.py`.

It reports `skipped_low_risk`, which is not a polite fiction: the E1 constant
reply carries no money, no deadline and no commitment, so the real risk gate
would reach the same verdict on it without spending a token.
"""

from agents_runtime.agent_core.review import SKIPPED_LOW_RISK, Reviewer, ReviewOutcome
from agents_runtime.queueing.jobs import EvalJob


def create_reviewer(dsn: str) -> Reviewer:
    async def review(job: EvalJob) -> ReviewOutcome:
        return ReviewOutcome(status=SKIPPED_LOW_RISK)

    return review
