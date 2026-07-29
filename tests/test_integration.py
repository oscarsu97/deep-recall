"""End-to-end: Notion blocks → LLM → vault file → Telegram stages → SM-2 → disk.

Uses fakes for the two network boundaries (Notion, LLM) and exercises every
real module in between, which is where the glue bugs live.
"""

import json
from datetime import date, timedelta

import pytest

from src.config import Config
from src.ingestion import NotionIngestor
from src.sm2 import QUALITY_GOOD
from src.synthesizer import Synthesizer
from src.telegram_bot import (
    Callback,
    render_stage_checkpoints,
    render_stage_full,
    render_stage_question,
)
from src.vault import Vault
from tests.test_ingestion import FakeNotion, PAGE, block
from tests.test_synthesizer import GOOD_CARD, FakeProvider

TODAY = date(2026, 7, 29)

NOTION_BLOCKS = {
    "page-1": [
        block("t1", "toggle", "How do you retry a failed Kafka message?", has_children=True)
    ],
    "t1": [
        block("b1", "bulleted_list_item", "retry topic + commit offset, sendfile(2) on read path"),
        block("b2", "bulleted_list_item", "DLQ after max retries, ordering breaks though"),
    ],
}


@pytest.fixture(autouse=True)
def instant(monkeypatch):
    monkeypatch.setattr("src.synthesizer.MIN_SECONDS_BETWEEN_CALLS", 0)
    monkeypatch.setattr("src.ingestion.BASE_BACKOFF_SECONDS", 0)


def test_full_pipeline(tmp_path):
    config = Config(notion_token="x", notion_database_id="db-1", vault_dir=tmp_path / "vault")
    vault = Vault(config.vault_dir)
    vault.ensure()

    # 1. Ingest from Notion.
    notes = NotionIngestor(config, client=FakeNotion(PAGE, NOTION_BLOCKS)).fetch_notes()
    assert len(notes) == 1
    assert "sendfile(2)" in notes[0].raw_answer

    # 2. Synthesise into a card.
    synthesizer = Synthesizer(config, provider=FakeProvider(json.dumps(GOOD_CARD)))
    card = synthesizer.synthesize(notes[0])
    path = vault.save(card)

    assert path.exists()
    assert path.parent.name == "distributed-systems"
    assert path.read_text(encoding="utf-8").startswith("---\n")

    # 3. A brand-new card is due immediately.
    due = vault.due_cards(today=TODAY)
    assert [c.id for c in due] == ["kafka-retry-patterns"]

    # 4. Walk the Telegram reveal flow, reloading from disk at each stage the
    #    way a restarted bot process would.
    loaded = vault.find("kafka-retry-patterns")
    question_text, keyboard = render_stage_question(loaded)
    assert "orders-retry-30s" not in question_text

    reveal = Callback.decode(keyboard.inline_keyboard[0][0].callback_data)
    checkpoint_text, keyboard = render_stage_checkpoints(vault.find(reveal.card_id))
    assert "orders-retry-30s" in checkpoint_text

    shift = Callback.decode(keyboard.inline_keyboard[0][0].callback_data)
    shifted_text, _ = render_stage_checkpoints(vault.find(shift.card_id), modifier_index=shift.arg)
    assert "Constraint Shift" in shifted_text

    full_text, keyboard = render_stage_full(vault.find(shift.card_id))
    assert "RabbitMQ" in full_text

    # 5. Rate it "Good" and persist.
    rating = Callback.decode(keyboard.inline_keyboard[0][1].callback_data)
    assert rating.arg == QUALITY_GOOD

    graded = vault.find(rating.card_id)
    from src.sm2 import review

    graded.apply_review(review(graded.review_state, rating.arg, today=TODAY), reviewed_on=TODAY)
    vault.save(graded)

    # 6. It is no longer due, and the schedule landed on disk.
    reloaded = vault.find("kafka-retry-patterns")
    assert reloaded.review_state.next_review == TODAY + timedelta(days=1)
    assert reloaded.review_state.repetition_count == 1
    assert vault.due_cards(today=TODAY) == []
    assert vault.due_cards(today=TODAY + timedelta(days=1))


def test_resync_skips_notes_already_in_the_vault(tmp_path):
    config = Config(notion_token="x", notion_database_id="db-1", vault_dir=tmp_path / "vault")
    vault = Vault(config.vault_dir)
    vault.ensure()

    notes = NotionIngestor(config, client=FakeNotion(PAGE, NOTION_BLOCKS)).fetch_notes()
    card = Synthesizer(config, provider=FakeProvider(json.dumps(GOOD_CARD))).synthesize(notes[0])
    vault.save(card)

    # The LLM rewrote the question, so the card id differs from the note slug…
    assert card.id != notes[0].suggested_id
    # …and only `source_note_id` makes the second run recognise it.
    assert notes[0].suggested_id in vault.known_ids()
    assert card.id in vault.known_ids()

    # A second identical ingest must therefore produce nothing to synthesise.
    again = NotionIngestor(config, client=FakeNotion(PAGE, NOTION_BLOCKS)).fetch_notes()
    known = vault.known_ids()
    assert [n for n in again if n.suggested_id not in known] == []
