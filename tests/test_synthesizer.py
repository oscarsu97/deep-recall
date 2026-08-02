import json

import pytest

from src.config import Config
from src.ingestion import RawNote
from src.synthesizer import (
    SynthesisError,
    Synthesizer,
    extract_json,
    lint_card,
    render_card,
)

GOOD_CARD = {
    "id": "kafka-retry-patterns",
    "topic": "Distributed Systems",
    "scenario": (
        "Your `orders` consumer lag alert fires at 03:00. One malformed record has been "
        "failing for twenty minutes and 40k messages are stacked behind it on partition 3."
    ),
    "question": "How do you retry a failed Kafka message without blocking the partition?",
    "direct_mechanism": (
        "A consumer tracks one monotonic offset per partition, so retrying in place stalls "
        "everything behind it. Produce the payload to `orders-retry-30s` with a retry-count "
        "header and commit the original offset. A delay consumer calls `pause()` until the "
        "record matures, then `orders-dlq` takes it after max.retries."
    ),
    "decision_matrix": [
        {"condition": "partition progress must not stall",
         "choice": "publish to a retry topic and commit the offset",
         "tradeoff": "the record is reordered relative to its partition-mates"},
        {"condition": "strict per-key ordering is required",
         "choice": "call `pause()` on the TopicPartition and retry in place",
         "tradeoff": "throughput for that partition is 0 until it succeeds"},
    ],
    "tipping_point": (
        "Wrong when retry delay must vary per message: the delay is encoded by topic, so "
        "every schedule costs a topic. Use RabbitMQ per-message TTL with a dead-letter exchange."
    ),
    "constraint_modifiers": [
        {"name": "Strict Ordering", "effect": "Retry topics break the per-key guarantee; "
                                              "pause the partition and add a poison-pill "
                                              "timeout instead."},
        {"name": "Exactly-Once", "effect": "The produce and the offset commit must share one "
                                           "transaction via `sendOffsetsToTransaction()`."},
    ],
    "anchor": "Kafka Streams ships this as its `DeserializationExceptionHandler` dead-letter path.",
}


# --- linting --------------------------------------------------------------


def test_a_well_formed_card_passes_the_lint():
    assert lint_card(GOOD_CARD) == []


def test_unexplained_buzzword_is_rejected():
    card = dict(GOOD_CARD, tipping_point="Wrong when you need something more scalable and fast.")
    problems = lint_card(card)
    assert any("buzzword" in p for p in problems)


def test_buzzword_is_allowed_when_the_sentence_explains_the_mechanism():
    card = dict(
        GOOD_CARD,
        direct_mechanism=(
            "The read path is fast because `sendfile(2)` moves bytes from the page cache "
            "straight into the socket buffer with no user-space copy. Enabling TLS defeats "
            "this and costs roughly 30% throughput. Offsets stay monotonic per partition."
        ),
    )
    assert not any("buzzword" in p for p in lint_card(card))


def test_thin_mechanism_is_rejected():
    assert any("direct_mechanism" in p for p in lint_card(dict(GOOD_CARD, direct_mechanism="Use a DLQ.")))


def test_mechanism_without_any_concrete_marker_is_rejected():
    prose = (
        "You take the message that did not work and you put it somewhere else so that the "
        "other messages can carry on being handled by the system as they normally would be. "
        "Later on someone can come back and look at the ones that did not work out."
    )
    problems = lint_card(dict(GOOD_CARD, direct_mechanism=prose))
    assert any("no concrete mechanism" in p for p in problems)


def test_yes_no_questions_are_rejected():
    problems = lint_card(dict(GOOD_CARD, question="Is Kafka good for retrying failed messages?"))
    assert any("yes/no" in p for p in problems)


@pytest.mark.parametrize("key", ["decision_matrix", "constraint_modifiers"])
def test_structural_sections_must_have_at_least_two_entries(key):
    problems = lint_card(dict(GOOD_CARD, **{key: GOOD_CARD[key][:1]}))
    assert any(key in p for p in problems)


def test_incomplete_matrix_row_is_reported():
    row = [dict(GOOD_CARD["decision_matrix"][0], tradeoff=""), GOOD_CARD["decision_matrix"][1]]
    assert any("tradeoff" in p for p in lint_card(dict(GOOD_CARD, decision_matrix=row)))


# --- word budgets ---------------------------------------------------------
#
# The linter used to enforce only minimums, so the model faced one-sided length
# pressure and inflated: the first 90 cards averaged 424 words of body prose.
# At that size a review is re-reading, and ~8 separately-forgettable claims sit
# under one SM-2 ease factor.


def test_an_overlong_mechanism_is_rejected():
    bloated = GOOD_CARD["direct_mechanism"] + " " + " ".join(f"word{i}" for i in range(40))
    problems = lint_card(dict(GOOD_CARD, direct_mechanism=bloated))
    assert any("60-word budget" in p for p in problems)


def test_the_budget_complaint_says_to_delete_not_paraphrase():
    problems = lint_card(dict(GOOD_CARD, tipping_point=GOOD_CARD["tipping_point"] * 3))
    assert any("do not paraphrase" in p for p in problems)


@pytest.mark.parametrize("field,limit", [("condition", 12), ("choice", 15), ("tradeoff", 20)])
def test_each_matrix_cell_has_its_own_budget(field, limit):
    row = dict(GOOD_CARD["decision_matrix"][0], **{field: "word " * (limit + 1)})
    problems = lint_card(dict(GOOD_CARD, decision_matrix=[row, GOOD_CARD["decision_matrix"][1]]))
    assert any(f"decision_matrix[1].{field}" in p for p in problems)


def test_a_mechanism_running_past_three_sentences_is_rejected():
    four = "It uses `a`. Then it uses `b`. Then `c` happens. Finally `d` is written."
    assert any("3 sentences" in p for p in lint_card(dict(GOOD_CARD, direct_mechanism=four)))


def test_a_card_without_a_scenario_is_rejected():
    """A concrete situation is a retrieval cue and a transfer test, not decoration."""
    problems = lint_card({k: v for k, v in GOOD_CARD.items() if k != "scenario"})
    assert any("`scenario`" in p for p in problems)


def test_a_generic_scenario_is_too_thin_to_pass():
    problems = lint_card(dict(GOOD_CARD, scenario="Consider a Kafka consumer."))
    assert any("`scenario`" in p for p in problems)


def test_nominalised_phrasing_is_rejected():
    card = dict(GOOD_CARD, tipping_point=(
        "Wrong when offset compaction is performed on every poll: the broker rewrites "
        "the segment. Use a log-compacted topic with `min.cleanable.dirty.ratio` instead."
    ))
    assert any("Nominalised" in p for p in lint_card(card))


# --- JSON extraction ------------------------------------------------------


def test_extracts_json_from_a_fenced_block():
    assert extract_json(f"```json\n{json.dumps(GOOD_CARD)}\n```")["id"] == "kafka-retry-patterns"


def test_extracts_json_despite_leading_prose():
    assert extract_json(f"Sure! Here is the card:\n{json.dumps(GOOD_CARD)}\nHope that helps.")["topic"]


def test_non_json_output_raises():
    with pytest.raises(SynthesisError):
        extract_json("I'm sorry, I can't help with that.")


def test_json_array_is_rejected():
    with pytest.raises(SynthesisError):
        extract_json("[1, 2, 3]")


# --- rendering ------------------------------------------------------------


def test_render_produces_the_canonical_sections():
    card = render_card(GOOD_CARD)
    assert card.id == "kafka-retry-patterns"
    assert card.topic == "Distributed Systems"
    assert len(card.decision_matrix) == 2
    assert len(card.constraint_modifiers) == 2
    assert card.meta["interval"] == 0
    assert card.meta["ease_factor"] == 2.5
    # New cards are due immediately.
    assert card.meta["next_review"] == card.meta["created"]


def test_render_normalises_conditions_to_IF_form():
    assert card_matrix_text(render_card(GOOD_CARD)).count("IF ") == 2


def test_render_does_not_double_prefix_existing_IF():
    data = dict(GOOD_CARD, decision_matrix=[
        {"condition": "IF ordering matters", "choice": "pause", "tradeoff": "throughput"},
        {"condition": "throughput matters", "choice": "retry topic", "tradeoff": "reordering"},
    ])
    assert "IF IF" not in card_matrix_text(render_card(data))


def card_matrix_text(card):
    return card.sections["Decision Matrix"]


def test_render_survives_a_missing_id():
    card = render_card({k: v for k, v in GOOD_CARD.items() if k != "id"})
    assert card.id  # derived from the question


def test_render_puts_the_scenario_and_anchor_on_the_card():
    card = render_card(GOOD_CARD)
    assert "lag alert fires at 03:00" in card.scenario
    assert "DeserializationExceptionHandler" in card.anchor


def test_empty_sections_are_dropped_so_the_body_hash_survives_a_round_trip():
    """A blank section is omitted by `to_markdown`, so storing one would make
    every card read back off disk look hand-edited."""
    from src.vault import Card

    card = render_card({k: v for k, v in GOOD_CARD.items() if k != "anchor"})
    assert "Seen In" not in card.sections
    assert Card.from_markdown(card.to_markdown()).body_digest() == card.meta["body_hash"]


def test_render_records_the_notion_source_url():
    note = RawNote(question="q", raw_answer="a", source_url="https://notion.so/abc")
    assert render_card(GOOD_CARD, note=note).meta["source_url"] == "https://notion.so/abc"


# --- orchestration --------------------------------------------------------


class FakeProvider:
    """Returns canned responses in order, recording the prompts it received."""

    name = "fake"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, system, messages):
        self.calls.append(messages)
        return self.responses.pop(0) if self.responses else "{}"


@pytest.fixture
def note():
    return RawNote(question="Kafka retries?", raw_answer="x" * 60, page_title="Kafka")


def test_a_clean_first_draft_needs_no_retry(note, monkeypatch):
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    provider = FakeProvider(json.dumps(GOOD_CARD))
    card = Synthesizer(Config(), provider=provider).synthesize(note)
    assert len(provider.calls) == 1
    assert card.id == "kafka-retry-patterns"


def test_a_buzzword_draft_triggers_one_corrective_retry(note, monkeypatch):
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    bad = dict(GOOD_CARD, tipping_point="Wrong if you need it to be more scalable and robust.")
    provider = FakeProvider(json.dumps(bad), json.dumps(GOOD_CARD))

    card = Synthesizer(Config(), provider=provider).synthesize(note)

    assert len(provider.calls) == 2
    assert "rejected by the quality linter" in provider.calls[1][1]
    assert "RabbitMQ" in card.tipping_point


def test_the_first_draft_is_kept_when_the_rewrite_is_no_better(note, monkeypatch):
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    bad = dict(GOOD_CARD, tipping_point="Too short.")
    worse = dict(bad, direct_mechanism="Nope.")
    provider = FakeProvider(json.dumps(bad), json.dumps(worse))

    card = Synthesizer(Config(), provider=provider).synthesize(note)
    assert "Nope." not in card.direct_mechanism


GEMINI_DAILY_429 = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current "
    "quota', 'status': 'RESOURCE_EXHAUSTED', 'details': [{'@type': 'type.googleapis.com/"
    "google.rpc.QuotaFailure', 'violations': [{'quotaMetric': 'generativelanguage.googleapis"
    ".com/generate_content_free_tier_requests', 'quotaId': 'GenerateRequestsPerDayPerProject"
    "PerModel-FreeTier', 'quotaValue': '20'}]}, {'@type': 'type.googleapis.com/google.rpc."
    "RetryInfo', 'retryDelay': '43s'}]}}"
)

GEMINI_PER_MINUTE_429 = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'quotaId': "
    "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier', 'retryDelay': '7s'}}"
)


def test_a_daily_quota_error_is_recognised():
    from src.synthesizer import is_daily_quota_error

    assert is_daily_quota_error(GEMINI_DAILY_429)


def test_a_per_minute_limit_is_not_treated_as_a_daily_quota():
    from src.synthesizer import is_daily_quota_error

    assert not is_daily_quota_error(GEMINI_PER_MINUTE_429)


def test_groq_daily_phrasing_is_recognised():
    from src.synthesizer import is_daily_quota_error

    assert is_daily_quota_error(
        "429 rate_limit_exceeded: Limit 1000 requests per day reached for model X"
    )


def test_the_providers_retry_hint_is_honoured():
    from src.synthesizer import retry_after_seconds

    assert retry_after_seconds(GEMINI_PER_MINUTE_429, default=2.0) == 8.0
    assert retry_after_seconds("no hint here", default=2.0) == 2.0


def test_a_daily_quota_aborts_immediately_without_retrying(note, monkeypatch):
    """161 doomed calls is what this prevents."""
    from src.synthesizer import QuotaExhaustedError

    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    monkeypatch.setattr("src.synthesizer.BASE_BACKOFF_SECONDS", 0)

    calls = []

    class Exhausted:
        name = "gemini"

        def generate(self, system, messages):
            calls.append(1)
            raise RuntimeError(GEMINI_DAILY_429)

    with pytest.raises(QuotaExhaustedError):
        Synthesizer(Config(), provider=Exhausted()).synthesize(note)

    assert len(calls) == 1, "a daily quota must not be retried"


def test_a_per_minute_limit_is_still_retried(note, monkeypatch):
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    monkeypatch.setattr("src.synthesizer.BASE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr("src.synthesizer.retry_after_seconds", lambda text, default: 0)

    calls = []

    class Flaky:
        name = "gemini"

        def generate(self, system, messages):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError(GEMINI_PER_MINUTE_429)
            return json.dumps(GOOD_CARD)

    card = Synthesizer(Config(), provider=Flaky()).synthesize(note)
    assert len(calls) == 2
    assert card.id == "kafka-retry-patterns"


def test_provider_failure_surfaces_as_synthesis_error(note, monkeypatch):
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    monkeypatch.setattr("src.synthesizer.BASE_BACKOFF_SECONDS", 0)

    class Broken:
        name = "broken"

        def generate(self, system, messages):
            raise RuntimeError("503 service unavailable")

    with pytest.raises(SynthesisError):
        Synthesizer(Config(), provider=Broken()).synthesize(note)
