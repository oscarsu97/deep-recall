"""Rendering and callback-encoding tests. No Telegram network calls."""

import asyncio
import types

import pytest

from src.sm2 import QUALITY_EASY, QUALITY_GOOD, QUALITY_HARD
from src.telegram_bot import (
    MAX_CALLBACK_BYTES,
    MAX_MESSAGE_CHARS,
    STAGE_RATE,
    STAGE_REVEAL,
    Callback,
    md_to_html,
    render_stage_checkpoints,
    render_stage_full,
    render_stage_question,
)
from src.vault import Card


@pytest.fixture
def card():
    return Card(
        id="kafka-retry-patterns",
        topic="Distributed Systems",
        question="How do you retry a failed Kafka message without blocking the partition?",
        sections={
            "Direct Mechanism": "Publish to a retry topic and commit via `commitSync()`.",
            "Decision Matrix": (
                "- **IF throughput is the priority:** route to `orders-retry-1` — *trade-off:* reordering.\n"
                "- **IF strict ordering is required:** call `pause()` on the partition."
            ),
            "Tipping Point (When is this WRONG?)": "Wrong for per-message backoff; use RabbitMQ.",
            "Constraint Modifiers": (
                "- *Modifier 1 (Strict Ordering):* Pause the partition.\n"
                "- *Modifier 2 (Cost Limits):* Bounded in-memory buffer."
            ),
        },
        meta={"interval": 6, "ease_factor": 2.5, "repetition_count": 2},
    )


# --- callback data --------------------------------------------------------


def test_callback_round_trips():
    original = Callback(STAGE_RATE, "kafka-retry-patterns", 5)
    assert Callback.decode(original.encode()) == original


def test_callback_stays_within_the_telegram_limit():
    data = Callback(STAGE_RATE, "a" * 48, 5).encode()
    assert len(data.encode()) <= MAX_CALLBACK_BYTES


def test_oversized_card_id_is_truncated_rather_than_crashing():
    assert len(Callback(STAGE_RATE, "z" * 200, 1).encode().encode()) <= MAX_CALLBACK_BYTES


def test_foreign_callback_data_is_ignored():
    assert Callback.decode("some-other-bot|x") is None
    assert Callback.decode("") is None


# --- markdown -> html -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("**bold**", "<b>bold</b>"),
        ("`code`", "<code>code</code>"),
        ("*italic*", "<i>italic</i>"),
        ("_italic_", "<i>italic</i>"),
    ],
)
def test_inline_markdown_becomes_html(raw, expected):
    assert md_to_html(raw) == expected


def test_html_in_card_content_is_escaped_not_executed():
    assert md_to_html("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_angle_brackets_inside_code_are_escaped():
    assert md_to_html("`List<String>`") == "<code>List&lt;String&gt;</code>"


# --- stage rendering ------------------------------------------------------


def test_stage_one_shows_only_the_question(card):
    text, keyboard = render_stage_question(card)
    assert "How do you retry" in text
    assert "commitSync" not in text
    assert keyboard.inline_keyboard[0][0].callback_data == Callback(STAGE_REVEAL, card.id).encode()


def test_stage_two_reveals_mechanism_but_hides_the_choices(card):
    text, keyboard = render_stage_checkpoints(card)
    assert "commitSync()" in text
    assert "IF strict ordering is required" in text
    assert "pause()" not in text

    labels = [b.text for b in keyboard.inline_keyboard[0]]
    assert any("Shift Constraint" in label for label in labels)
    assert any("Show Full Answer" in label for label in labels)


def test_shift_constraint_shows_one_modifier_and_advances_the_index(card):
    text, keyboard = render_stage_checkpoints(card, modifier_index=0)
    assert "Pause the partition" in text
    assert "Bounded in-memory buffer" not in text
    assert Callback.decode(keyboard.inline_keyboard[0][0].callback_data).arg == 1


def test_shift_constraint_wraps_around(card):
    _, keyboard = render_stage_checkpoints(card, modifier_index=1)
    assert Callback.decode(keyboard.inline_keyboard[0][0].callback_data).arg == 0


def test_shift_button_is_hidden_when_a_card_has_no_modifiers(card):
    card.sections["Constraint Modifiers"] = ""
    _, keyboard = render_stage_checkpoints(card)
    assert [b.text for b in keyboard.inline_keyboard[0]] == ["📖 Show Full Answer"]


def test_stage_three_shows_everything_plus_rating_buttons(card):
    text, keyboard = render_stage_full(card)
    assert "pause()" in text
    assert "RabbitMQ" in text
    assert "Bounded in-memory buffer" in text

    buttons = keyboard.inline_keyboard[0]
    assert [Callback.decode(b.callback_data).arg for b in buttons] == [
        QUALITY_HARD, QUALITY_GOOD, QUALITY_EASY
    ]


def test_rating_labels_show_the_real_sm2_intervals(card):
    # rep=2, interval=6, ease=2.5 -> Good yields round(6 * 2.36) = 14 days.
    labels = [b.text for b in render_stage_full(card)[1].inline_keyboard[0]]
    assert labels[0] == "🔴 Hard (1d)"
    assert "14d" in labels[1]


def test_long_cards_are_truncated_below_the_telegram_limit(card):
    card.sections["Direct Mechanism"] = "word " * 3000
    text, _ = render_stage_full(card)
    assert len(text) <= MAX_MESSAGE_CHARS
    assert "truncated" in text


def test_truncation_closes_an_open_code_tag(card):
    card.sections["Direct Mechanism"] = "`" + ("x" * 5000) + "`"
    text, _ = render_stage_full(card)
    assert text.count("<code>") == text.count("</code>")


# --- error handling -------------------------------------------------------


def error_context(error):
    """Minimal stand-in for PTB's CallbackContext."""
    stopped = []
    application = types.SimpleNamespace(stop_running=lambda: stopped.append(True))
    return types.SimpleNamespace(error=error, application=application), stopped


def make_bot(tmp_path):
    from src.config import Config
    from src.telegram_bot import DeepRecallBot

    return DeepRecallBot(Config(telegram_bot_token="123:abc", vault_dir=tmp_path))


def test_a_second_poller_shuts_down_instead_of_looping_forever(tmp_path, caplog):
    """Telegram allows one getUpdates consumer per token; retrying cannot help."""
    from telegram.error import Conflict

    bot = make_bot(tmp_path)
    context, stopped = error_context(Conflict("terminated by other getUpdates request"))

    asyncio.run(bot._on_error(None, context))

    assert stopped == [True], "the application should stop, not keep retrying"
    assert any("only one" in r.message for r in caplog.records)
    # A known, actionable state should not dump a traceback.
    assert all(r.exc_info is None for r in caplog.records)


def test_other_errors_are_logged_with_a_traceback_and_keep_running(tmp_path, caplog):
    from telegram.error import TelegramError

    bot = make_bot(tmp_path)
    context, stopped = error_context(TelegramError("something transient"))

    asyncio.run(bot._on_error(None, context))

    assert stopped == [], "an ordinary error must not kill the poll session"
    assert any(r.exc_info for r in caplog.records)
