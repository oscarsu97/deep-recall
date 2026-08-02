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
from src.synthesizer import Synthesizer, render_cards
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
    """Generate and save the cluster of cards a real sync would."""
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    cards = Synthesizer(Config(), provider=FakeProvider(json.dumps(GOOD_CARD))).synthesize(source_note)
    for card in cards:
        vault.save(card)
    return cards


# --- the six branches -----------------------------------------------------


def test_a_brand_new_note_is_scheduled_for_synthesis(vault):
    pending, backfilled = _plan_sync(vault, [note()])
    assert [existing for _, existing in pending] == [[]]
    assert backfilled == 0


def test_an_unchanged_note_is_skipped_entirely(vault, monkeypatch):
    seed(vault, note(), monkeypatch)
    pending, _ = _plan_sync(vault, [note()])
    assert pending == [], "an unchanged note must not cost an LLM call"


def test_an_edited_answer_triggers_regeneration_of_the_whole_cluster(vault, monkeypatch):
    cards = seed(vault, note(), monkeypatch)
    edited = note(answer=ANSWER + " Enabling TLS defeats this and costs ~30% throughput.")

    pending, _ = _plan_sync(vault, [edited])

    assert len(pending) == 1
    _, existing = pending[0]
    assert {c.id for c in existing} == {c.id for c in cards}


def test_a_reworded_question_matches_by_block_id_and_does_not_duplicate(vault, monkeypatch):
    cards = seed(vault, note(), monkeypatch)
    reworded = note(question="How does Kafka avoid userspace copies on the read path?")

    # The slug changed, so only the block id can save us from a duplicate.
    assert reworded.suggested_id != note().suggested_id

    pending, _ = _plan_sync(vault, [reworded])
    assert pending == [], "same block, same content -> nothing to do"

    # And when the body also changes, it regenerates the *same* cluster.
    both_changed = note(question="How does Kafka avoid userspace copies?", answer=ANSWER + " More.")
    pending, _ = _plan_sync(vault, [both_changed])
    assert len(pending) == 1
    assert {c.id for c in pending[0][1]} == {c.id for c in cards}


def test_a_hand_edit_to_any_sibling_protects_the_whole_cluster(vault, monkeypatch, caplog):
    """Regeneration replaces every sibling, so one edited card puts them all at risk."""
    cards = seed(vault, note(), monkeypatch)
    edited = next(c for c in cards if c.direct_mechanism)
    edited.sections["Direct Mechanism"] += "\n\nMy own note: ask Priya about the DLQ replay tool."
    vault.save(edited)

    pending, _ = _plan_sync(vault, [note(answer=ANSWER + " changed upstream")])

    assert pending == []
    assert any("edited in Obsidian" in r.message for r in caplog.records)


def test_dry_run_does_not_write_backfilled_provenance(vault, monkeypatch):
    """`--dry-run` promises to touch nothing, including the backfill path."""
    cards = seed(vault, note(), monkeypatch)
    for card in cards:
        for key in ("source_hash", "source_block_id", "body_hash"):
            card.meta.pop(key, None)
        vault.save(card)
    originals = {c.path: c.path.read_text(encoding="utf-8") for c in cards}

    pending, backfilled = _plan_sync(vault, [note()], backfill=False)

    assert pending == []
    assert backfilled == 0
    for path, text in originals.items():
        assert path.read_text(encoding="utf-8") == text, "dry run must not write"


def test_force_overrides_every_skip(vault, monkeypatch):
    seed(vault, note(), monkeypatch)
    pending, backfilled = _plan_sync(vault, [note()], force=True)
    assert len(pending) == 1 and pending[0][1] == []
    assert backfilled == 0


def test_a_legacy_cluster_gets_provenance_backfilled_without_regenerating(vault, monkeypatch):
    cards = seed(vault, note(), monkeypatch)
    # Simulate cards written before hashing existed.
    for card in cards:
        for key in ("source_hash", "source_block_id", "body_hash"):
            card.meta.pop(key, None)
        vault.save(card)

    pending, backfilled = _plan_sync(vault, [note()])

    assert pending == [], "upgrading must not regenerate the whole vault"
    assert backfilled == len(cards)
    for card in cards:
        reloaded = vault.find(card.id)
        assert reloaded.meta["source_hash"] == note().content_hash
        assert reloaded.meta["source_block_id"] == BLOCK


# --- what regeneration preserves -----------------------------------------


def _regenerate(vault, monkeypatch, data):
    """Regenerate a note's cluster the way `cmd_sync` does, matching by kind."""
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    existing = vault.index_clusters_by_source()[BLOCK]

    fresh = Synthesizer(Config(), provider=FakeProvider(json.dumps(data))).synthesize(note())
    previous = {c.sibling_key: c for c in existing}
    for card in fresh:
        twin = previous.pop(card.sibling_key, None)
        if twin is not None:
            card.carry_review_state_from(twin)
        vault.save(card)
    for orphan in previous.values():
        vault.delete(orphan)
    return fresh


def test_regeneration_keeps_each_siblings_own_history_identity_and_file(vault, monkeypatch):
    cards = seed(vault, note(), monkeypatch)
    mechanism = next(c for c in cards if c.kind == "mechanism")

    # Build up some review history on one sibling only.
    mechanism.apply_review(review(mechanism.review_state, QUALITY_GOOD, today=date(2026, 7, 29)))
    mechanism.apply_review(review(mechanism.review_state, QUALITY_GOOD, today=date(2026, 7, 30)))
    original_path = vault.save(mechanism)
    state_before = mechanism.review_state
    assert state_before.repetition_count == 2

    # Regenerate with a reworded question, so every id would otherwise change.
    revised = dict(GOOD_CARD, id="totally-different-slug", topic="Some Other Topic",
                   direct_mechanism=GOOD_CARD["direct_mechanism"] + " Then `pause()` runs.")
    fresh = _regenerate(vault, monkeypatch, revised)

    regenerated = next(c for c in fresh if c.kind == "mechanism")
    assert regenerated.path == original_path, "must not leave a duplicate behind"
    assert regenerated.id == mechanism.id
    assert regenerated.topic == mechanism.topic

    after = vault.find(mechanism.id).review_state
    assert after.repetition_count == state_before.repetition_count
    assert after.ease_factor == state_before.ease_factor
    assert after.next_review == state_before.next_review
    assert "pause()" in vault.find(mechanism.id).direct_mechanism


def test_the_untouched_siblings_keep_their_own_fresh_schedules(vault, monkeypatch):
    """Rating the mechanism must not advance the tipping point's interval."""
    cards = seed(vault, note(), monkeypatch)
    mechanism = next(c for c in cards if c.kind == "mechanism")
    mechanism.apply_review(review(mechanism.review_state, QUALITY_GOOD, today=date(2026, 7, 29)))
    vault.save(mechanism)

    tipping = next(c for c in cards if c.kind == "tipping")
    assert vault.find(tipping.id).review_state.repetition_count == 0


def test_no_duplicate_files_are_left_after_a_regeneration(vault, monkeypatch):
    cards = seed(vault, note(), monkeypatch)
    _regenerate(vault, monkeypatch, dict(GOOD_CARD, topic="Wildly Different Topic"))

    assert len(list(vault.root.rglob("*.md"))) == len(cards)


def test_a_sibling_the_note_no_longer_answers_is_pruned(vault, monkeypatch):
    cards = seed(vault, note(), monkeypatch)
    assert len(cards) == 5

    # The note loses its second constraint modifier.
    thinner = dict(GOOD_CARD, constraint_modifiers=GOOD_CARD["constraint_modifiers"][:1])
    fresh = _regenerate(vault, monkeypatch, thinner)

    assert len(fresh) == 4
    # Leaving it behind would keep scheduling a question the note stopped asking.
    assert vault.find("kafka-retry-patterns-mod2") is None
    assert len(list(vault.root.rglob("*.md"))) == 4


# --- digests --------------------------------------------------------------


def test_cosmetic_whitespace_changes_do_not_count_as_edits():
    assert note(answer=ANSWER).content_hash == note(answer=f"  {ANSWER}   \n\n").content_hash


def test_a_freshly_generated_card_is_not_flagged_as_hand_edited():
    for card in render_cards(GOOD_CARD, note=note()):
        assert not card.is_hand_edited()


def test_editing_a_card_body_flags_it(vault, monkeypatch):
    card = next(c for c in seed(vault, note(), monkeypatch) if c.kind == "tipping")
    assert not card.is_hand_edited()
    card.sections["Tipping Point (When is this WRONG?)"] = "I rewrote this myself."
    assert card.is_hand_edited()


def test_rating_a_card_does_not_look_like_a_hand_edit(vault, monkeypatch):
    """`body_hash` is written by the synthesizer only, so `save()` cannot reset it."""
    card = seed(vault, note(), monkeypatch)[0]
    card.apply_review(review(card.review_state, QUALITY_GOOD))
    vault.save(card)
    assert not vault.find(card.id).is_hand_edited()
