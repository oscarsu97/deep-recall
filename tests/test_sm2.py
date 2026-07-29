from datetime import date, timedelta

import pytest

from src.sm2 import (
    DEFAULT_EASE_FACTOR,
    MIN_EASE_FACTOR,
    QUALITY_EASY,
    QUALITY_GOOD,
    QUALITY_HARD,
    ReviewState,
    humanise_interval,
    is_due,
    preview_intervals,
    review,
    update_ease_factor,
)

TODAY = date(2026, 7, 29)


def test_first_successful_review_schedules_one_day():
    result = review(ReviewState(), QUALITY_GOOD, today=TODAY)
    assert result.interval == 1
    assert result.repetition_count == 1
    assert result.next_review == TODAY + timedelta(days=1)


def test_second_successful_review_jumps_to_six_days():
    state = review(ReviewState(), QUALITY_GOOD, today=TODAY)
    result = review(state, QUALITY_GOOD, today=TODAY)
    assert result.interval == 6
    assert result.repetition_count == 2


def test_third_review_multiplies_by_ease_factor():
    state = ReviewState(interval=6, ease_factor=2.5, repetition_count=2)
    result = review(state, QUALITY_EASY, today=TODAY)
    assert result.interval == round(6 * result.ease_factor)


def test_hard_rating_resets_the_chain_but_keeps_the_penalty():
    state = ReviewState(interval=30, ease_factor=2.5, repetition_count=5)
    result = review(state, QUALITY_HARD, today=TODAY)
    assert result.interval == 1
    assert result.repetition_count == 0
    assert result.ease_factor < state.ease_factor


def test_ease_factor_never_drops_below_the_floor():
    ease = DEFAULT_EASE_FACTOR
    for _ in range(20):
        ease = update_ease_factor(ease, QUALITY_HARD)
    assert ease == MIN_EASE_FACTOR


def test_easy_rating_raises_ease_factor():
    assert update_ease_factor(2.5, QUALITY_EASY) > 2.5


def test_good_rating_decays_ease_factor_slightly():
    # Faithful SM-2: q=4 is neutral, so q=3 applies a small penalty.
    assert update_ease_factor(2.5, QUALITY_GOOD) == pytest.approx(2.36)


def test_quality_outside_the_scale_is_rejected():
    with pytest.raises(ValueError):
        review(ReviewState(), 9)


def test_preview_matches_an_actual_review():
    state = ReviewState(interval=10, ease_factor=2.3, repetition_count=4)
    preview = preview_intervals(state)
    for quality, interval in preview.items():
        assert review(state, quality, today=TODAY).interval == interval


def test_unscheduled_cards_are_due():
    assert is_due(None, TODAY)
    assert is_due(TODAY, TODAY)
    assert not is_due(TODAY + timedelta(days=1), TODAY)


@pytest.mark.parametrize(
    "days,expected", [(1, "1d"), (6, "6d"), (29, "29d"), (30, "1mo"), (45, "1.5mo"), (400, "1.1y")]
)
def test_humanise_interval(days, expected):
    assert humanise_interval(days) == expected
