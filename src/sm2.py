"""The SM-2 spaced repetition algorithm (SuperMemo 2, Wozniak 1987).

Pure functions over plain data — no I/O, no globals, no clock reads except the
`today` argument that callers may override. That makes the scheduler trivially
testable and lets the Telegram bot preview *actual* next intervals on its
rating buttons instead of hardcoding "1d / 3d / 7d".

Quality scale (0-5 in the original paper). DeepRecall exposes three:

    1 = Hard  -> recall failed or was painful; the card resets.
    3 = Good  -> recalled correctly but with real effort.
    5 = Easy  -> recalled instantly.

Note that under *pure* SM-2 a quality of 3 still nudges the ease factor down
slightly (q=4 is the neutral point). That is intentional and faithful to the
paper: cards you consistently only barely recall should slowly get denser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

QUALITY_HARD = 1
QUALITY_GOOD = 3
QUALITY_EASY = 5

#: Below this quality the repetition chain resets.
PASSING_QUALITY = 3

MIN_EASE_FACTOR = 1.3
DEFAULT_EASE_FACTOR = 2.5

#: Guard against runaway scheduling on very mature cards.
MAX_INTERVAL_DAYS = 365 * 2


@dataclass(frozen=True)
class ReviewState:
    """The SM-2 state carried in each card's YAML frontmatter."""

    interval: int = 0
    ease_factor: float = DEFAULT_EASE_FACTOR
    repetition_count: int = 0
    next_review: date | None = None


def _clamp_quality(quality: int) -> int:
    if quality < 0 or quality > 5:
        raise ValueError(f"quality must be in 0..5, got {quality!r}")
    return quality


def update_ease_factor(ease_factor: float, quality: int) -> float:
    """Apply the SM-2 ease-factor update, floored at 1.3.

    EF' = EF + (0.1 - (5-q) * (0.08 + (5-q) * 0.02))
    """
    q = _clamp_quality(quality)
    delta = 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)
    return max(MIN_EASE_FACTOR, round(ease_factor + delta, 4))


def review(
    state: ReviewState,
    quality: int,
    today: date | None = None,
) -> ReviewState:
    """Return the new `ReviewState` after grading a card with `quality`.

    A failed review (quality < 3) resets the repetition chain and reschedules
    for tomorrow, but keeps the (now-penalised) ease factor — a card that has
    proven hard historically stays hard.
    """
    q = _clamp_quality(quality)
    today = today or date.today()

    ease_factor = update_ease_factor(state.ease_factor, q)

    if q < PASSING_QUALITY:
        repetition_count = 0
        interval = 1
    else:
        repetition_count = state.repetition_count + 1
        if repetition_count == 1:
            interval = 1
        elif repetition_count == 2:
            interval = 6
        else:
            interval = round(max(1, state.interval) * ease_factor)

    interval = max(1, min(interval, MAX_INTERVAL_DAYS))

    return ReviewState(
        interval=interval,
        ease_factor=ease_factor,
        repetition_count=repetition_count,
        next_review=today + timedelta(days=interval),
    )


def preview_intervals(
    state: ReviewState,
    qualities: tuple[int, ...] = (QUALITY_HARD, QUALITY_GOOD, QUALITY_EASY),
) -> dict[int, int]:
    """Map each quality to the interval it would produce, without mutating state.

    Used to label the Telegram rating buttons with the real schedule
    (`🟡 Good (6d)`) rather than a fixed guess.
    """
    return {q: review(state, q).interval for q in qualities}


def is_due(next_review: date | None, today: date | None = None) -> bool:
    """A card with no `next_review` has never been scheduled, so it is due."""
    if next_review is None:
        return True
    return next_review <= (today or date.today())


def humanise_interval(days: int) -> str:
    """`1 -> '1d'`, `45 -> '1.5mo'`, `400 -> '1.1y'` — for compact button labels."""
    if days < 30:
        return f"{days}d"
    if days < 365:
        return f"{days / 30:.1f}".rstrip("0").rstrip(".") + "mo"
    return f"{days / 365:.1f}".rstrip("0").rstrip(".") + "y"
