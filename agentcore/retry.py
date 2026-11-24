"""Retry helpers for calls that return ApiResult."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .results import ApiResult


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3            # total tries, including the first
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0
    jitter: float = 0.1          # fraction of the delay added at random

    def delay(self, attempt: int) -> float:
        """Seconds to wait after failed attempt number `attempt` (1-based)."""
        base = min(self.max_delay_s, self.base_delay_s * (2 ** (attempt - 1)))
        return base * (1 + random.random() * self.jitter)


Sleep = Callable[[float], Awaitable[None]]
Call = Callable[[], Awaitable[ApiResult[Any]]]
Reconcile = Callable[[], Awaitable["ApiResult[Any] | None"]]


async def call_with_retry(fn: Call, policy: RetryPolicy = RetryPolicy(), sleep: Sleep = asyncio.sleep) -> ApiResult[Any]:
    """Call `fn` until it succeeds, fails for good, or the attempts run out. Retries only `retryable` failures."""
    result = await fn()
    for attempt in range(1, policy.attempts):
        if result.ok or not result.retryable:
            break
        await sleep(policy.delay(attempt))
        result = await fn()
    return result


async def execute_write(
    fn: Call,
    reconcile: Reconcile | None = None,
    policy: RetryPolicy = RetryPolicy(),
    sleep: Sleep = asyncio.sleep,
) -> ApiResult[Any]:
    """Run a write that may fail after it was applied.

    When a failure has `outcome_unknown`, `reconcile()` checks the source of truth first:
      - it returns an ApiResult  -> that is the answer (success or a confirmed failure), no retry;
      - it returns None          -> still unknown; retry `fn` (only safe if `fn` is idempotent).
    If still unknown when attempts run out, the last ambiguous failure is returned.
    """
    result = await fn()
    for attempt in range(1, policy.attempts):
        if result.ok:
            break
        if result.outcome_unknown and reconcile is not None:
            checked = await reconcile()
            if checked is not None:
                return checked
        elif not result.retryable:
            break
        await sleep(policy.delay(attempt))
        result = await fn()
    return result
