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

#: Must track the wall clock: `render_card` stamps new cards with the real
#: `date.today()`, so pinning this to a literal makes the test fail whenever
#: the date rolls past it.
TODAY = date.today()

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

    # 2. Synthesise into a cluster of sibling cards.
    synthesizer = Synthesizer(config, provider=FakeProvider(json.dumps(GOOD_CARD)))
    cards = synthesizer.synthesize(notes[0])
    paths = [vault.save(card) for card in cards]

    assert len(cards) == 5
    for path in paths:
        assert path.exists()
        assert path.parent.name == "distributed-systems"
        assert path.read_text(encoding="utf-8").startswith("---\n")

    # 3. All five are new, but only one surfaces today — siblings are buried so
    #    the second is recall rather than pattern-completion.
    due = vault.due_cards(today=TODAY)
    assert [c.id for c in due] == ["kafka-retry-patterns-dec"]
    assert len(vault.due_cards(today=TODAY, bury_siblings=False)) == 5

    # 4. Walk the Telegram reveal flow on the mechanism sibling, reloading from
    #    disk at each stage the way a restarted bot process would.
    loaded = vault.find("kafka-retry-patterns-mech")
    question_text, keyboard = render_stage_question(loaded)
    assert "lag alert fires at 03:00" in question_text   # the scenario sets it up
    assert "orders-retry-30s" not in question_text

    reveal = Callback.decode(keyboard.inline_keyboard[0][0].callback_data)
    cue_text, keyboard = render_stage_checkpoints(vault.find(reveal.card_id))
    assert "orders-retry-30s" in cue_text                 # the identifier is the cue
    assert "commit the original offset" not in cue_text   # the prose is not

    full_text, keyboard = render_stage_full(vault.find(reveal.card_id))
    assert "commit the original offset" in full_text
    # This sibling answers the mechanism only; the tipping point is its own card.
    assert "RabbitMQ" not in full_text

    # 5. Rate it "Good" and persist.
    rating = Callback.decode(keyboard.inline_keyboard[0][1].callback_data)
    assert rating.arg == QUALITY_GOOD

    graded = vault.find(rating.card_id)
    from src.sm2 import review

    graded.apply_review(review(graded.review_state, rating.arg, today=TODAY), reviewed_on=TODAY)
    vault.save(graded)

    # 6. It is no longer due, and the schedule landed on disk — while its
    #    siblings kept their own, untouched.
    reloaded = vault.find("kafka-retry-patterns-mech")
    assert reloaded.review_state.next_review == TODAY + timedelta(days=1)
    assert reloaded.review_state.repetition_count == 1
    assert vault.find("kafka-retry-patterns-tip").review_state.repetition_count == 0
    assert "kafka-retry-patterns-mech" not in [c.id for c in vault.due_cards(today=TODAY)]


def test_resync_skips_notes_already_in_the_vault(tmp_path):
    config = Config(notion_token="x", notion_database_id="db-1", vault_dir=tmp_path / "vault")
    vault = Vault(config.vault_dir)
    vault.ensure()

    notes = NotionIngestor(config, client=FakeNotion(PAGE, NOTION_BLOCKS)).fetch_notes()
    cards = Synthesizer(config, provider=FakeProvider(json.dumps(GOOD_CARD))).synthesize(notes[0])
    for card in cards:
        vault.save(card)

    # The LLM rewrote the question, so the card ids differ from the note slug…
    assert all(c.id != notes[0].suggested_id for c in cards)
    # …and only `source_note_id` makes the second run recognise them.
    assert notes[0].suggested_id in vault.known_ids()
    assert all(c.id in vault.known_ids() for c in cards)
    # One note, one cluster, however many siblings it was cut into.
    found = vault.index_clusters_by_source()[notes[0].suggested_id]
    assert {c.id for c in found} == {c.id for c in cards}

    # A second identical ingest must therefore produce nothing to synthesise.
    again = NotionIngestor(config, client=FakeNotion(PAGE, NOTION_BLOCKS)).fetch_notes()
    known = vault.known_ids()
    assert [n for n in again if n.suggested_id not in known] == []
