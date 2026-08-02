from datetime import date, timedelta

import pytest

from src.sm2 import QUALITY_GOOD, ReviewState, review
from src.vault import Card, Vault, slugify

SAMPLE = """\
---
id: "kafka-retry-patterns"
topic: "Distributed Systems"
created: "2026-07-29"
next_review: "2026-07-30"
interval: 1
ease_factor: 2.5
repetition_count: 0
---

# Q: How do you retry a failed Kafka message without blocking the partition?

## Direct Mechanism
Publish to a retry topic and commit the original offset via `commitSync()`.

## Decision Matrix
- **IF non-blocking throughput is the priority:** route to `orders-retry-1` — *trade-off:* reordering.
- **IF strict ordering is required:** call `pause()` on the partition.

## Tipping Point (When is this WRONG?)
Wrong when per-message backoff is needed; use RabbitMQ per-message TTL instead.

## Constraint Modifiers
- *Modifier 1 (Strict Ordering):* Pause the partition; retry synchronously.
- *Modifier 2 (Cost Limits):* Bounded in-memory buffer before the DLQ.
"""


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "vault")
    v.ensure()
    path = v.root / "distributed-systems" / "kafka-retry-patterns.md"
    path.parent.mkdir(parents=True)
    path.write_text(SAMPLE, encoding="utf-8")
    return v


def test_parses_frontmatter_and_sections(vault):
    card = vault.find("kafka-retry-patterns")
    assert card is not None
    assert card.topic == "Distributed Systems"
    assert card.question.startswith("How do you retry")
    assert "commitSync()" in card.direct_mechanism
    assert len(card.decision_matrix) == 2
    assert len(card.constraint_modifiers) == 2
    assert "RabbitMQ" in card.tipping_point


def test_review_state_reads_from_frontmatter(vault):
    state = vault.find("kafka-retry-patterns").review_state
    assert state == ReviewState(
        interval=1, ease_factor=2.5, repetition_count=0, next_review=date(2026, 7, 30)
    )


def test_round_trip_preserves_content(vault):
    card = vault.find("kafka-retry-patterns")
    reparsed = Card.from_markdown(card.to_markdown())
    assert reparsed.question == card.question
    assert reparsed.sections == card.sections
    assert reparsed.meta["ease_factor"] == card.meta["ease_factor"]


def test_round_trip_preserves_hand_added_sections_and_keys(vault):
    card = vault.find("kafka-retry-patterns")
    card.sections["My Own Notes"] = "Ask Priya about the DLQ replay tool."
    card.meta["tags"] = ["interview", "kafka"]

    reparsed = Card.from_markdown(card.to_markdown())
    assert reparsed.sections["My Own Notes"] == "Ask Priya about the DLQ replay tool."
    assert reparsed.meta["tags"] == ["interview", "kafka"]


def test_saving_a_review_updates_frontmatter_on_disk(vault):
    card = vault.find("kafka-retry-patterns")
    card.apply_review(review(card.review_state, QUALITY_GOOD, today=date(2026, 7, 30)))
    vault.save(card)

    reloaded = vault.find("kafka-retry-patterns")
    assert reloaded.review_state.repetition_count == 1
    assert reloaded.review_state.next_review == date(2026, 7, 31)
    assert reloaded.meta["last_reviewed"]


def test_due_cards_are_ordered_most_overdue_first(vault):
    for card_id, due in (("older", date(2026, 1, 1)), ("newer", date(2026, 7, 1))):
        card = Card(
            id=card_id,
            topic="Databases",
            question=f"Question {card_id}?",
            sections={"Direct Mechanism": "x"},
            meta={"id": card_id, "topic": "Databases", "next_review": due.isoformat()},
        )
        vault.save(card)

    due = vault.due_cards(today=date(2026, 7, 29))
    assert [c.id for c in due[:2]] == ["older", "newer"]


def test_future_cards_are_not_due(vault):
    card = vault.find("kafka-retry-patterns")
    card.meta["next_review"] = (date(2026, 7, 29) + timedelta(days=5)).isoformat()
    vault.save(card)
    assert vault.due_cards(today=date(2026, 7, 29)) == []


def test_retrieval_cues_redact_the_matrix_choices(vault):
    cues = vault.find("kafka-retry-patterns").retrieval_cues()
    assert "commitSync()" in cues
    assert "IF strict ordering is required" in cues
    assert "pause()" not in cues  # the answer stays hidden


def test_retrieval_cues_hint_rather_than_reprint_the_mechanism(vault):
    """The cue is the skeleton of the answer, not the answer.

    Printing `direct_mechanism` verbatim here would make stage two the full
    reveal and turn the review into re-reading.
    """
    card = vault.find("kafka-retry-patterns")
    cues = card.retrieval_cues()

    assert "`commitSync()`" in cues          # the identifier comes back
    assert "Publish to a retry topic" not in cues   # the prose does not
    assert card.direct_mechanism not in cues


def test_retrieval_cues_fall_back_to_opening_words_without_identifiers():
    card = Card(
        id="x", topic="Databases", question="Why?",
        sections={"Direct Mechanism": "The planner walks the join tree and picks an order."},
    )
    cues = card.retrieval_cues()
    assert cues.startswith("The planner walks the join tree and")
    assert cues.endswith("…")
    assert "picks an order" not in cues


def test_retrieval_cues_are_capped(vault):
    card = Card(
        id="x", topic="Databases", question="Why?",
        sections={"Direct Mechanism": " ".join(f"`ident{i}`" for i in range(20))},
    )
    assert card.retrieval_cues().count("`ident") == 6


def _due_card(vault, card_id, next_review, repetition_count):
    vault.save(
        Card(
            id=card_id,
            topic="Databases",
            question=f"Question {card_id}?",
            sections={"Direct Mechanism": "x"},
            meta={
                "id": card_id, "topic": "Databases",
                "next_review": next_review.isoformat(),
                "repetition_count": repetition_count,
            },
        )
    )


def test_new_limit_caps_first_passes_without_displacing_reviews(vault):
    """A first pass costs minutes; a repeat costs seconds.

    A queue capped only in cards is not capped in effort, so the new-card
    budget is separate — and spending it never pushes out a due review.
    """
    for i in range(6):
        _due_card(vault, f"new-{i}", date(2026, 1, 1), repetition_count=0)
    for i in range(4):
        _due_card(vault, f"rep-{i}", date(2026, 1, 2), repetition_count=3)

    due = vault.due_cards(today=date(2026, 7, 29), new_limit=2)
    ids = [c.id for c in due]

    assert sum(1 for i in ids if i.startswith("new-")) == 2
    assert sum(1 for i in ids if i.startswith("rep-")) == 4


def test_new_limit_keeps_the_most_overdue_new_cards(vault):
    _due_card(vault, "new-old", date(2026, 1, 1), repetition_count=0)
    _due_card(vault, "new-recent", date(2026, 7, 1), repetition_count=0)

    due = vault.due_cards(today=date(2026, 7, 29), new_limit=1)
    assert [c.id for c in due] == ["new-old"]


def test_due_cards_without_a_new_limit_are_unfiltered(vault):
    for i in range(3):
        _due_card(vault, f"new-{i}", date(2026, 1, 1), repetition_count=0)
    # The fixture card is scheduled for the 30th, so only the three new ones.
    assert len(vault.due_cards(today=date(2026, 7, 29))) == 3


def test_unparseable_cards_are_skipped_not_fatal(vault, caplog):
    (vault.root / "broken.md").write_text("---\n: : not yaml : :\n---\nbody", encoding="utf-8")
    assert vault.load_all()  # the good card still loads


def test_ordinary_notes_in_the_vault_are_not_treated_as_cards(vault):
    """A vault is a normal Obsidian folder — a README must never be reviewed."""
    (vault.root / "README.md").write_text(
        "# My vault\n\nNotes generated from Notion.\n", encoding="utf-8"
    )
    (vault.root / "daily" / "2026-07-29.md").parent.mkdir(parents=True)
    (vault.root / "daily" / "2026-07-29.md").write_text(
        "---\ntags: [journal]\n---\n\n# Tuesday\n\nRead about Kafka.\n", encoding="utf-8"
    )

    ids = {c.id for c in vault.load_all()}
    assert ids == {"kafka-retry-patterns"}
    assert vault.find("README") is None
    # And they never surface as permanently-due cards.
    assert all(c.id == "kafka-retry-patterns" for c in vault.due_cards())


def test_a_note_with_frontmatter_but_no_id_is_not_a_card(vault):
    (vault.root / "index.md").write_text(
        "---\ntags: [moc]\n---\n\n# Index\n", encoding="utf-8"
    )
    assert [c.id for c in vault.load_all()] == ["kafka-retry-patterns"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Distributed Systems", "distributed-systems"),
        ("  C++ / Memory Models!  ", "c-memory-models"),
        ("", "untitled"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_slugify_bounds_length_for_callback_data():
    assert len(slugify("word " * 50, max_length=48)) <= 48


# --- topic drilling -------------------------------------------------------


def _card(vault, card_id, topic, next_review):
    vault.save(
        Card(
            id=card_id,
            topic=topic,
            question=f"Question {card_id}?",
            sections={"Direct Mechanism": "x"},
            meta={"id": card_id, "topic": topic, "next_review": next_review.isoformat()},
        )
    )


def test_topic_cards_filters_to_one_topic(vault):
    _card(vault, "db-a", "Databases", date(2026, 1, 1))
    _card(vault, "net-a", "Networking", date(2026, 1, 1))

    assert [c.id for c in vault.topic_cards("Databases", today=date(2026, 7, 29))] == ["db-a"]


def test_topic_cards_include_cards_that_are_not_due(vault):
    # The point of asking for a topic is to drill it now, so a card scheduled
    # for next year still has to come back.
    _card(vault, "future", "Databases", date(2027, 1, 1))

    ids = [c.id for c in vault.topic_cards("Databases", today=date(2026, 7, 29))]
    assert ids == ["future"]
    assert vault.due_cards(today=date(2026, 7, 29)) == []


def test_topic_cards_put_due_before_upcoming(vault):
    _card(vault, "future", "Databases", date(2027, 1, 1))
    _card(vault, "overdue", "Databases", date(2026, 1, 1))
    _card(vault, "due-today", "Databases", date(2026, 7, 29))

    ids = [c.id for c in vault.topic_cards("Databases", today=date(2026, 7, 29))]
    assert ids == ["overdue", "due-today", "future"]


def test_topic_cards_respects_limit(vault):
    for i in range(5):
        _card(vault, f"db-{i}", "Databases", date(2026, 1, 1))

    assert len(vault.topic_cards("Databases", limit=2, today=date(2026, 7, 29))) == 2


def test_topic_cards_for_unknown_topic_is_empty(vault):
    assert vault.topic_cards("Basketry", today=date(2026, 7, 29)) == []


def test_topic_counts_report_total_and_due_largest_first(vault):
    _card(vault, "db-a", "Databases", date(2026, 1, 1))
    _card(vault, "db-b", "Databases", date(2027, 1, 1))
    _card(vault, "net-a", "Networking", date(2026, 1, 1))

    counts = dict((topic, (total, due)) for topic, total, due in
                  vault.topic_counts(today=date(2026, 7, 29)))
    assert counts["Databases"] == (2, 1)
    assert counts["Networking"] == (1, 1)

    ordered = [topic for topic, _total, _due in vault.topic_counts(today=date(2026, 7, 29))]
    assert ordered.index("Databases") < ordered.index("Networking")
