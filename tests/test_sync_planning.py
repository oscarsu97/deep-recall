"""How `--sync` decides to create, regenerate, or skip a card.

These pin the edit-handling contract:

    note is new                       -> synthesise
    note unchanged                    -> skip (no LLM call)
    note body edited                  -> regenerate, keeping SM-2 history
    question reworded                 -> matched by block id, no duplicate
    card hand-edited in Obsidian      -> skip, protect the human's work
    card predates content hashing     -> backfill provenance, do not regenerate
"""

import json
from datetime import date

import pytest

from src.config import Config
from src.ingestion import RawNote
from src.main import _plan_sync
from src.sm2 import QUALITY_GOOD, review
from src.synthesizer import Synthesizer, render_card
from src.vault import Vault
from tests.test_synthesizer import GOOD_CARD, FakeProvider

BLOCK = "block-abc-123"
ANSWER = "sendfile(2) moves bytes from the page cache into the socket buffer, no user-space copy."


def note(answer=ANSWER, question="How does Kafka avoid user-space copies?", block_id=BLOCK):
    return RawNote(question=question, raw_answer=answer, page_title="Kafka", block_id=block_id)


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "vault")
    v.ensure()
    return v


def seed(vault, source_note, monkeypatch):
    """Generate and save a card the way a real sync would."""
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    card = Synthesizer(Config(), provider=FakeProvider(json.dumps(GOOD_CARD))).synthesize(source_note)
    vault.save(card)
    return card


# --- the six branches -----------------------------------------------------


def test_a_brand_new_note_is_scheduled_for_synthesis(vault):
    pending, backfilled = _plan_sync(vault, [note()])
    assert [existing for _, existing in pending] == [None]
    assert backfilled == 0


def test_an_unchanged_note_is_skipped_entirely(vault, monkeypatch):
    seed(vault, note(), monkeypatch)
    pending, _ = _plan_sync(vault, [note()])
    assert pending == [], "an unchanged note must not cost an LLM call"


def test_an_edited_answer_triggers_regeneration(vault, monkeypatch):
    card = seed(vault, note(), monkeypatch)
    edited = note(answer=ANSWER + " Enabling TLS defeats this and costs ~30% throughput.")

    pending, _ = _plan_sync(vault, [edited])

    assert len(pending) == 1
    _, existing = pending[0]
    assert existing is not None and existing.id == card.id


def test_a_reworded_question_matches_by_block_id_and_does_not_duplicate(vault, monkeypatch):
    card = seed(vault, note(), monkeypatch)
    reworded = note(question="How does Kafka avoid userspace copies on the read path?")

    # The slug changed, so only the block id can save us from a duplicate.
    assert reworded.suggested_id != note().suggested_id

    pending, _ = _plan_sync(vault, [reworded])
    assert pending == [], "same block, same content -> nothing to do"

    # And when the body also changes, it regenerates the *same* card.
    both_changed = note(question="How does Kafka avoid userspace copies?", answer=ANSWER + " More.")
    pending, _ = _plan_sync(vault, [both_changed])
    assert len(pending) == 1 and pending[0][1].id == card.id


def test_a_hand_edited_card_is_protected_from_being_overwritten(vault, monkeypatch, caplog):
    card = seed(vault, note(), monkeypatch)
    card.sections["Direct Mechanism"] += "\n\nMy own note: ask Priya about the DLQ replay tool."
    vault.save(card)

    pending, _ = _plan_sync(vault, [note(answer=ANSWER + " changed upstream")])

    assert pending == []
    assert any("edited in Obsidian" in r.message for r in caplog.records)


def test_dry_run_does_not_write_backfilled_provenance(vault, monkeypatch):
    """`--dry-run` promises to touch nothing, including the backfill path."""
    card = seed(vault, note(), monkeypatch)
    for key in ("source_hash", "source_block_id", "body_hash"):
        card.meta.pop(key, None)
    vault.save(card)
    before = (vault.root / card.path.name if card.path else None)
    original = card.path.read_text(encoding="utf-8")

    pending, backfilled = _plan_sync(vault, [note()], backfill=False)

    assert pending == []
    assert backfilled == 0
    assert card.path.read_text(encoding="utf-8") == original, "dry run must not write"


def test_force_overrides_every_skip(vault, monkeypatch):
    seed(vault, note(), monkeypatch)
    pending, backfilled = _plan_sync(vault, [note()], force=True)
    assert len(pending) == 1 and pending[0][1] is None
    assert backfilled == 0


def test_a_legacy_card_gets_provenance_backfilled_without_regenerating(vault, monkeypatch):
    card = seed(vault, note(), monkeypatch)
    # Simulate a card written before hashing existed.
    for key in ("source_hash", "source_block_id", "body_hash"):
        card.meta.pop(key, None)
    vault.save(card)

    pending, backfilled = _plan_sync(vault, [note()])

    assert pending == [], "upgrading must not regenerate the whole vault"
    assert backfilled == 1
    reloaded = vault.find(card.id)
    assert reloaded.meta["source_hash"] == note().content_hash
    assert reloaded.meta["source_block_id"] == BLOCK


# --- what regeneration preserves -----------------------------------------


def test_regeneration_keeps_review_history_identity_and_file_location(vault, monkeypatch):
    card = seed(vault, note(), monkeypatch)

    # Build up some review history first.
    card.apply_review(review(card.review_state, QUALITY_GOOD, today=date(2026, 7, 29)))
    card.apply_review(review(card.review_state, QUALITY_GOOD, today=date(2026, 7, 30)))
    original_path = vault.save(card)
    state_before = card.review_state
    assert state_before.repetition_count == 2

    # Now regenerate with different content.
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    revised = dict(GOOD_CARD, id="totally-different-slug", topic="Some Other Topic",
                   direct_mechanism=GOOD_CARD["direct_mechanism"] + " Plus a new sentence about `pause()`.")
    fresh = Synthesizer(Config(), provider=FakeProvider(json.dumps(revised))).synthesize(note())
    fresh.carry_review_state_from(card)
    new_path = vault.save(fresh)

    # Same file, same id, same schedule — only the content moved on.
    assert new_path == original_path, "must not leave a duplicate behind"
    assert fresh.id == card.id
    assert fresh.topic == card.topic
    after = vault.find(card.id).review_state
    assert after.repetition_count == state_before.repetition_count
    assert after.ease_factor == state_before.ease_factor
    assert after.next_review == state_before.next_review
    assert "pause()" in vault.find(card.id).direct_mechanism


def test_only_one_card_file_exists_after_a_regeneration(vault, monkeypatch):
    card = seed(vault, note(), monkeypatch)
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)

    revised = dict(GOOD_CARD, topic="Wildly Different Topic")
    fresh = Synthesizer(Config(), provider=FakeProvider(json.dumps(revised))).synthesize(note())
    fresh.carry_review_state_from(card)
    vault.save(fresh)

    assert len(list(vault.root.rglob("*.md"))) == 1


# --- digests --------------------------------------------------------------


def test_cosmetic_whitespace_changes_do_not_count_as_edits():
    assert note(answer=ANSWER).content_hash == note(answer=f"  {ANSWER}   \n\n").content_hash


def test_a_freshly_generated_card_is_not_flagged_as_hand_edited():
    card = render_card(GOOD_CARD, note=note())
    assert not card.is_hand_edited()


def test_editing_a_card_body_flags_it(vault, monkeypatch):
    card = seed(vault, note(), monkeypatch)
    assert not card.is_hand_edited()
    card.sections["Tipping Point (When is this WRONG?)"] = "I rewrote this myself."
    assert card.is_hand_edited()


def test_rating_a_card_does_not_look_like_a_hand_edit(vault, monkeypatch):
    """`body_hash` is written by the synthesizer only, so `save()` cannot reset it."""
    card = seed(vault, note(), monkeypatch)
    card.apply_review(review(card.review_state, QUALITY_GOOD))
    vault.save(card)
    assert not vault.find(card.id).is_hand_edited()
