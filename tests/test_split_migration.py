"""Splitting the mega-cards that already exist, without an LLM call.

The sections are already on disk, so the migration is pure surgery. What it
must not do is throw away review history — those intervals were earned — or
guess a subject it is not sure of, since one bad guess corrupts all four of a
cluster's sibling questions.
"""

from datetime import date

import pytest

from scripts.split_clusters import infer_subject, plan
from src.vault import Card, Vault

MEGA = """\
---
id: "kafka-retry-patterns"
topic: "Distributed Systems"
created: "2026-01-01"
next_review: "2026-08-20"
interval: 21
ease_factor: 2.7
repetition_count: 6
last_reviewed: "2026-07-30"
source_block_id: "blk-1"
source_hash: "abc123"
---

# Q: How does Kafka retry a failed message without blocking the partition?

## Direct Mechanism
Publish to a retry topic and commit the original offset via `commitSync()`.

## Decision Matrix
- **IF throughput is the priority:** route to `orders-retry-1` — *trade-off:* reordering.
- **IF strict ordering is required:** call `pause()` on the partition.

## Tipping Point (When is this WRONG?)
Wrong for per-message backoff; use RabbitMQ per-message TTL instead.

## Constraint Modifiers
- *Modifier 1 (Strict Ordering):* Pause the partition and retry synchronously.
- *Modifier 2 (Cost Limits):* Bounded in-memory buffer before the DLQ.
"""


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "vault")
    v.ensure()
    path = v.root / "distributed-systems" / "kafka-retry-patterns.md"
    path.parent.mkdir(parents=True)
    path.write_text(MEGA, encoding="utf-8")
    return v


def test_a_mega_card_is_planned_into_its_siblings(vault):
    (parent, siblings), = plan(vault)
    assert parent.id == "kafka-retry-patterns"
    assert [s.kind for s in siblings] == [
        "mechanism", "decision", "tipping", "modifier", "modifier"
    ]
    assert all(s.cluster == "kafka-retry-patterns" for s in siblings)


def test_every_sibling_inherits_the_review_history(vault):
    """Those intervals were earned — restarting all five at zero would be a
    punishment for reorganising the vault."""
    _, siblings = plan(vault)[0]
    for sibling in siblings:
        state = sibling.review_state
        assert state.repetition_count == 6
        assert state.interval == 21
        assert state.ease_factor == 2.7
        assert state.next_review == date(2026, 8, 20)


def test_siblings_keep_the_provenance_that_matches_them_to_their_note(vault):
    _, siblings = plan(vault)[0]
    for sibling in siblings:
        assert sibling.meta["source_block_id"] == "blk-1"
        assert sibling.meta["source_hash"] == "abc123"


def test_siblings_are_not_flagged_as_hand_edited(vault):
    """A stale `body_hash` would make every migrated card look edited in
    Obsidian, and `--sync` would then refuse to ever regenerate it."""
    _, siblings = plan(vault)[0]
    for sibling in siblings:
        assert not sibling.is_hand_edited()


def test_an_already_split_card_is_left_alone(vault):
    for card in [c for _, ss in plan(vault) for c in ss]:
        vault.save(card)
    vault.delete(vault.find("kafka-retry-patterns"))

    assert plan(vault) == []


def test_a_card_with_only_one_section_is_not_split(vault):
    vault.save(
        Card(
            id="lonely", topic="Databases", question="How does one thing work?",
            sections={"Direct Mechanism": "It just does, via `fsync()`."},
            meta={"id": "lonely", "topic": "Databases"},
        )
    )
    assert [p.id for p, _ in plan(vault)] == ["kafka-retry-patterns"]


# --- subject inference ----------------------------------------------------


@pytest.mark.parametrize(
    "question,expected",
    [
        ("How does Kafka avoid user-space copies?", "Kafka"),
        ("How does Spring Boot Actuator expose runtime state?", "Spring Boot Actuator"),
        ("How does `@Configuration` enforce singleton semantics?", "`@Configuration`"),
        ("How does UDP achieve lower latency than TCP?", "UDP"),
    ],
)
def test_a_named_subject_is_recognised(question, expected):
    assert infer_subject(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        # A verb crept in — "data pipeline be designed" would corrupt all four
        # sibling questions, so giving up and using the parent question is better.
        "How can a data pipeline be designed for late-arriving events?",
        "How do you implement data quality checks at the end of a run?",
        "What is the role of `raftlog` in a trading bot architecture?",
        "When to choose TCP over UDP for a network application?",
        "Mechanically, how does an ETL engine process batch records?",
    ],
)
def test_an_unsure_guess_is_declined(question):
    assert infer_subject(question) == ""


def test_an_inferred_subject_shapes_the_sibling_questions(vault):
    _, siblings = plan(vault)[0]
    tipping = next(s for s in siblings if s.kind == "tipping")
    assert tipping.question == "When is Kafka the wrong answer, and what wins instead?"


def test_without_a_subject_the_parent_question_becomes_the_stem(vault):
    path = vault.root / "distributed-systems" / "kafka-retry-patterns.md"
    path.write_text(
        MEGA.replace(
            "How does Kafka retry a failed message without blocking the partition?",
            "How can a retry mechanism be built without a hashmap?",
        ),
        encoding="utf-8",
    )

    _, siblings = plan(vault)[0]
    tipping = next(s for s in siblings if s.kind == "tipping")
    assert tipping.question == (
        "How can a retry mechanism be built without a hashmap "
        "— when is this the wrong approach, and what wins instead?"
    )
