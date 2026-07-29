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


def test_key_checkpoints_redact_the_matrix_choices(vault):
    checkpoints = vault.find("kafka-retry-patterns").key_checkpoints()
    assert "commitSync()" in checkpoints
    assert "IF strict ordering is required" in checkpoints
    assert "pause()" not in checkpoints  # the answer stays hidden


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
