"""Telegram interface: a two-stage reveal flow over inline keyboards.

The flow deliberately makes you commit before you see anything:

    ┌ scenario + Q: <question>            [🗝 Cue Me]
    │
    ├ + cue tokens, matrix conditions     [⚡ Shift Constraint] [📖 Show Full Answer]
    │   redacted                          (shift injects a constraint modifier)
    │
    └ + full card                         [🔴 Hard] [🟡 Good] [🟢 Easy]
                                          -> SM-2 update -> commit to git

Stage two hands back the *skeleton* of the answer — the identifiers, syscalls
and magnitudes — rather than the mechanism paragraph itself. A hint that can be
read instead of recalled is not a hint.

Every stage re-renders from the card file, and `callback_data` carries the
card id, so the flow survives a bot restart: `--notify` can push cards from a
GitHub Actions run that has long since exited, and a later `--bot-poll` session
still handles the button presses correctly.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from dataclasses import dataclass
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Conflict, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from . import git_sync, topics
from .config import Config
from .sm2 import (
    QUALITY_EASY,
    QUALITY_GOOD,
    QUALITY_HARD,
    humanise_interval,
    preview_intervals,
    review,
)
from .vault import SECTION_MATRIX, SECTION_MODIFIERS, Card, Vault

log = logging.getLogger(__name__)

#: Telegram hard-caps messages at 4096 characters.
MAX_MESSAGE_CHARS = 4000
#: ...and `callback_data` at 64 bytes.
MAX_CALLBACK_BYTES = 64

#: python-telegram-bot defaults to a 5s read timeout, which is easily exceeded
#: on the first TLS handshake to api.telegram.org over a slow link or a VPN —
#: the symptom is `TimedOut` during Application bootstrap, before any polling.
CONNECT_TIMEOUT = 20.0
READ_TIMEOUT = 20.0
#: Long polling holds the connection open, so it needs its own longer budget.
GET_UPDATES_READ_TIMEOUT = 40.0

CB_PREFIX = "dr"
STAGE_REVEAL = "rev"
STAGE_SHIFT = "shf"
STAGE_FULL = "full"
STAGE_RATE = "rate"
STAGE_PRACTICE = "prac"

QUALITY_LABELS = {QUALITY_HARD: "🔴 Hard", QUALITY_GOOD: "🟡 Good", QUALITY_EASY: "🟢 Easy"}


# ---------------------------------------------------------------------------
# Callback data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Callback:
    stage: str
    card_id: str
    arg: int = 0

    def encode(self) -> str:
        data = f"{CB_PREFIX}|{self.stage}|{self.arg}|{self.card_id}"
        if len(data.encode()) > MAX_CALLBACK_BYTES:
            # Card ids are slugged to 48 chars precisely to avoid this; if a
            # hand-written card overruns, degrade rather than crash the keyboard.
            budget = MAX_CALLBACK_BYTES - len(f"{CB_PREFIX}|{self.stage}|{self.arg}|".encode())
            data = f"{CB_PREFIX}|{self.stage}|{self.arg}|{self.card_id[:budget]}"
        return data

    @classmethod
    def decode(cls, data: str) -> "Callback | None":
        parts = data.split("|", 3)
        if len(parts) != 4 or parts[0] != CB_PREFIX:
            return None
        try:
            arg = int(parts[2])
        except ValueError:
            arg = 0
        return cls(stage=parts[1], card_id=parts[3], arg=arg)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def md_to_html(text: str) -> str:
    """Convert the card's Markdown subset to Telegram HTML.

    Escaping happens first so that user content can never inject tags; the
    inline-style regexes then run over already-escaped text.
    """
    out = html.escape(text or "")
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.DOTALL)
    out = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)\*(?!\*)", r"<i>\1</i>", out)
    out = re.sub(r"(?<!\w)_(?!\s)([^_\n]+?)_(?!\w)", r"<i>\1</i>", out)
    return out


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Trim to the Telegram limit on a line boundary, closing any open tag."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 40]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    if cut.count("<code>") > cut.count("</code>"):
        cut += "</code>"
    return cut + "\n\n<i>… (truncated — see the full card in Obsidian)</i>"


def resolve_topic_request(request: str, available: set[str]) -> str | None:
    """Map a typed topic request onto a topic that actually has cards.

    `/review kafka` and `/review spring boot` should work without the user
    knowing the folder is called "Distributed Systems" or "Java & Spring", so
    the request goes through the same canonicaliser the synthesiser uses.
    Returns None when nothing in the vault matches.
    """
    # An exact name wins outright: it is unambiguous, and it keeps a vault
    # carrying a topic from an older taxonomy drillable even though the
    # canonicaliser would map those same words onto the current vocabulary.
    lowered = request.casefold()
    for topic in sorted(available):
        if topic.casefold() == lowered:
            return topic

    canonical = topics.canonical(request)
    return canonical if canonical in available else None


def _header(card: Card, with_scenario: bool = True) -> str:
    """Topic, the scenario that sets the problem up, then the question.

    The scenario comes *before* the question deliberately: a concrete situation
    is a retrieval cue and a transfer test, and it only works if it is read
    while the answer is still unknown.
    """
    parts = [f"🧠 <b>{html.escape(card.topic)}</b>", ""]
    if card.scenario and with_scenario:
        parts += [f"<i>{md_to_html(card.scenario)}</i>", ""]
    parts.append(f"<b>Q: {html.escape(card.question)}</b>")
    return "\n".join(parts)


def render_stage_question(card: Card) -> tuple[str, InlineKeyboardMarkup]:
    state = card.review_state
    footprint = (
        f"rep {state.repetition_count} · ease {state.ease_factor:.2f} · "
        f"last interval {humanise_interval(max(state.interval, 1))}"
    )
    text = f"{_header(card)}\n\n<i>Answer it out loud first.</i>\n<code>{footprint}</code>"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "🗝 Cue Me",
            callback_data=Callback(STAGE_REVEAL, card.id).encode(),
        )]]
    )
    return _truncate(text), keyboard


def render_stage_checkpoints(card: Card, modifier_index: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    parts = [
        _header(card), "",
        "🗝 <b>Cue</b> <i>(the pieces the answer is built from — keep going)</i>",
        md_to_html(card.retrieval_cues()),
    ]

    modifiers = card.constraint_modifiers
    next_index = 0
    if modifier_index is not None and modifiers:
        index = modifier_index % len(modifiers)
        next_index = (index + 1) % len(modifiers)
        parts += ["", "⚡ <b>Constraint Shift</b>", md_to_html(modifiers[index])]
    elif modifiers:
        # First press lands on a random modifier; later presses cycle from there.
        next_index = random.randrange(len(modifiers))

    shift_label = "⚡ Shift Constraint" if modifier_index is None else "⚡ Shift Again"
    row = []
    if modifiers:
        row.append(InlineKeyboardButton(
            shift_label, callback_data=Callback(STAGE_SHIFT, card.id, next_index).encode()
        ))
    row.append(InlineKeyboardButton(
        "📖 Show Full Answer", callback_data=Callback(STAGE_FULL, card.id).encode()
    ))

    return _truncate("\n".join(parts)), InlineKeyboardMarkup([row])


def render_stage_full(card: Card) -> tuple[str, InlineKeyboardMarkup]:
    body = [_header(card), ""]
    for title, content in (
        ("⚙️ Direct Mechanism", card.direct_mechanism),
        ("🧭 Decision Matrix", card.sections.get(SECTION_MATRIX, "")),
        ("🚫 Tipping Point (when this is WRONG)", card.tipping_point),
        ("🔀 Constraint Modifiers", card.sections.get(SECTION_MODIFIERS, "")),
        ("📍 Seen In", card.anchor),
    ):
        if content.strip():
            body += [f"<b>{title}</b>", md_to_html(content.strip()), ""]

    intervals = preview_intervals(card.review_state)
    buttons = [
        InlineKeyboardButton(
            f"{QUALITY_LABELS[q]} ({humanise_interval(intervals[q])})",
            callback_data=Callback(STAGE_RATE, card.id, q).encode(),
        )
        for q in (QUALITY_HARD, QUALITY_GOOD, QUALITY_EASY)
    ]
    return _truncate("\n".join(body).rstrip()), InlineKeyboardMarkup([buttons])


def render_rated(card: Card, quality: int) -> str:
    state = card.review_state
    return (
        f"{_header(card, with_scenario=False)}\n\n"
        f"✅ Rated <b>{QUALITY_LABELS.get(quality, quality)}</b>\n"
        f"Next review: <b>{state.next_review}</b> "
        f"(+{humanise_interval(state.interval)})\n"
        f"<code>rep {state.repetition_count} · ease {state.ease_factor:.2f}</code>"
    )


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class DeepRecallBot:
    """Handlers plus the shared vault/git plumbing they need."""

    def __init__(self, config: Config):
        config.require_telegram(need_chat_id=False)
        self.config = config
        self.vault = Vault(config.vault_dir)

    # -- helpers ----------------------------------------------------------

    def _load(self, card_id: str) -> Card | None:
        try:
            return self.vault.find(card_id)
        except Exception as exc:  # noqa: BLE001 - never kill the handler
            log.error("Failed to load card %r: %s", card_id, exc)
            return None

    def _commit(self, card: Card, quality: int) -> None:
        if not self.config.git_auto_commit or not card.path:
            return
        try:
            git_sync.commit_paths(
                [card.path],
                f"review({card.id}): quality={quality} next={card.review_state.next_review}",
                repo=self.config.vault_dir,
            )
        except Exception as exc:  # noqa: BLE001 - a git failure must not lose the rating
            log.error("Could not commit review for %s: %s", card.id, exc)

    # -- command handlers -------------------------------------------------

    async def cmd_start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id if update.effective_chat else "?"
        await update.effective_message.reply_text(
            "🧠 <b>DeepRecall</b> is listening.\n\n"
            "/review — push the cards due today\n"
            "/review &lt;topic&gt; — drill one topic, due or not\n"
            "/practice — choose a topic to drill from a menu\n"
            "/topics — what is in the vault\n"
            "/stats — vault overview\n"
            "/due — how many are waiting\n\n"
            f"This chat's id is <code>{chat_id}</code> — put it in <code>TELEGRAM_CHAT_ID</code>.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_stats(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        s = self.vault.stats()
        await update.effective_message.reply_text(
            f"📊 <b>Vault</b>\n"
            f"total: <b>{s['total']}</b> across {s['topics']} topic(s)\n"
            f"due today: <b>{s['due']}</b>\n"
            f"never reviewed: {s['new']}\n"
            f"mature (≥21d): {s['mature']}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_due(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        due = self.vault.due_cards()
        if not due:
            await update.effective_message.reply_text("🎉 Nothing due. Go write some notes.")
            return
        listing = "\n".join(f"• {html.escape(c.question[:80])}" for c in due[:15])
        await update.effective_message.reply_text(
            f"⏰ <b>{len(due)} card(s) due</b>\n\n{listing}\n\nSend /review to start.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_topics(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        rows = self.vault.topic_counts()
        if not rows:
            await update.effective_message.reply_text("The vault is empty.")
            return
        listing = "\n".join(
            f"• <b>{html.escape(topic)}</b> — {total} card(s)"
            + (f", {due} due" if due else "")
            for topic, total, due in rows
        )
        await update.effective_message.reply_text(
            f"📚 <b>Topics</b>\n\n{listing}\n\n"
            "Send <code>/review &lt;topic&gt;</code> to drill one — "
            "<code>/review kafka</code> and <code>/review spring</code> both work.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        request = " ".join(context.args or []).strip()

        if not request:
            sent = await self.push_due(chat_id)
            if not sent:
                await update.effective_message.reply_text(
                    "🎉 Nothing due today. Send /topics to drill a topic anyway."
                )
            return

        available = {t for t, _total, _due in self.vault.topic_counts()}
        topic = resolve_topic_request(request, available)
        if topic is None:
            topic = topics.canonical(request)
            await update.effective_message.reply_text(
                f"No cards for <b>{html.escape(request)}</b> yet"
                + (f" (read as <i>{html.escape(topic)}</i>)." if topic != request else ".")
                + "\n\nSend /topics to see what is in the vault.",
                parse_mode=ParseMode.HTML,
            )
            return

        sent = await self.push_due(chat_id, topic=topic)
        if sent:
            await update.effective_message.reply_text(
                f"📖 {sent} card(s) from <b>{html.escape(topic)}</b> — "
                "due ones first, then what is coming up.",
                parse_mode=ParseMode.HTML,
            )

    async def cmd_practice(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        rows = self.vault.topic_counts()
        if not rows:
            await update.effective_message.reply_text("The vault is empty.")
            return

        keyboard = []
        row = []
        for topic, _total, _due in rows:
            row.append(InlineKeyboardButton(topic, callback_data=Callback(STAGE_PRACTICE, topic).encode()))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        await update.effective_message.reply_text(
            "Choose a topic to practice:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # -- callback handler -------------------------------------------------

    async def on_callback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return

        callback = Callback.decode(query.data)
        if callback is None:
            await query.answer("Unrecognised button.", show_alert=True)
            return

        if callback.stage == STAGE_PRACTICE:
            topic = callback.card_id
            await query.answer(f"Loading {topic}...")
            chat_id = update.effective_chat.id
            sent = await self.push_due(chat_id, topic=topic)
            if sent:
                await update.effective_message.reply_text(
                    f"📖 {sent} card(s) from <b>{html.escape(topic)}</b> — "
                    "due ones first, then what is coming up.",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.effective_message.reply_text(
                    f"🎉 Nothing due for <b>{html.escape(topic)}</b>.",
                    parse_mode=ParseMode.HTML,
                )
            return

        card = self._load(callback.card_id)
        if card is None:
            await query.answer(
                f"Card '{callback.card_id}' is no longer in the vault.", show_alert=True
            )
            return

        try:
            if callback.stage == STAGE_REVEAL:
                await query.answer("Checkpoints revealed")
                text, keyboard = render_stage_checkpoints(card)
            elif callback.stage == STAGE_SHIFT:
                await query.answer("⚡ Constraint shifted")
                text, keyboard = render_stage_checkpoints(card, modifier_index=callback.arg)
            elif callback.stage == STAGE_FULL:
                await query.answer("Full answer")
                text, keyboard = render_stage_full(card)
            elif callback.stage == STAGE_RATE:
                text, keyboard = await self._handle_rating(query, card, callback.arg)
            else:
                await query.answer("Unknown action.", show_alert=True)
                return

            await query.edit_message_text(
                text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        except TelegramError as exc:
            # "Message is not modified" is benign — the user double-tapped.
            if "not modified" in str(exc).lower():
                return
            log.error("Telegram edit failed for %s: %s", card.id, exc)

    async def _handle_rating(self, query, card: Card, quality: int):
        if quality not in QUALITY_LABELS:
            await query.answer("Unknown rating.", show_alert=True)
            return render_stage_full(card)

        new_state = review(card.review_state, quality, today=date.today())
        card.apply_review(new_state)

        try:
            self.vault.save(card)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not save %s: %s", card.id, exc)
            await query.answer("Saved rating failed — check the bot logs.", show_alert=True)
            return render_stage_full(card)

        # Blocking subprocess work; keep the event loop free for other cards.
        await asyncio.to_thread(self._commit, card, quality)

        await query.answer(f"Next review in {humanise_interval(new_state.interval)}")
        return render_rated(card, quality), None

    # -- pushing ----------------------------------------------------------

    async def push_due(
        self, chat_id: str | int, limit: int | None = None, topic: str | None = None
    ) -> int:
        """Send one message per card. Returns how many were delivered.

        With `topic`, drills that topic (due cards first, then upcoming ones);
        otherwise sends whatever is due across the whole vault.
        """
        from telegram import Bot
        from telegram.request import HTTPXRequest

        limit = limit or self.config.max_cards_per_session
        due = (
            self.vault.topic_cards(topic, limit=limit)
            if topic
            else self.vault.due_cards(
                limit=limit, new_limit=self.config.max_new_cards_per_session
            )
        )
        if not due:
            log.info("No cards to send%s.", f" for topic {topic!r}" if topic else " today")
            return 0

        bot = Bot(
            token=self.config.telegram_bot_token,
            request=HTTPXRequest(
                connect_timeout=CONNECT_TIMEOUT,
                read_timeout=READ_TIMEOUT,
                write_timeout=READ_TIMEOUT,
                pool_timeout=CONNECT_TIMEOUT,
            ),
        )
        sent = 0
        async with bot:
            for card in due:
                text, keyboard = render_stage_question(card)
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                    sent += 1
                except TelegramError as exc:
                    log.error("Could not send card %s: %s", card.id, exc)
                # Telegram throttles bulk sends at ~30 msg/s; be polite.
                await asyncio.sleep(0.4)

        log.info("Pushed %d/%d due card(s) to chat %s", sent, len(due), chat_id)
        return sent

    # -- app --------------------------------------------------------------

    def build_application(self) -> Application:
        app = (
            Application.builder()
            .token(self.config.telegram_bot_token)
            .connect_timeout(CONNECT_TIMEOUT)
            .read_timeout(READ_TIMEOUT)
            .write_timeout(READ_TIMEOUT)
            .pool_timeout(CONNECT_TIMEOUT)
            .get_updates_read_timeout(GET_UPDATES_READ_TIMEOUT)
            .build()
        )
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_start))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("due", self.cmd_due))
        app.add_handler(CommandHandler("topics", self.cmd_topics))
        app.add_handler(CommandHandler("review", self.cmd_review))
        app.add_handler(CommandHandler("practice", self.cmd_practice))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_error_handler(self._on_error)
        return app

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Telegram permits exactly one getUpdates consumer per token. A second
        # poller makes both fail on a ~6s loop forever, so retrying is pointless
        # — say what is wrong once and shut down. Left unhandled this would
        # spam a CI log for the entire poll window while achieving nothing.
        if isinstance(context.error, Conflict):
            log.error(
                "Another process is already polling this bot token — Telegram allows "
                "only one. Stop the other `--bot-poll` session (or the GitHub Actions "
                "run, if one is live) and start again. Shutting down."
            )
            context.application.stop_running()
            return

        log.error("Unhandled bot error: %s", context.error, exc_info=context.error)


# ---------------------------------------------------------------------------
# Entry points used by main.py
# ---------------------------------------------------------------------------


def resolve_chat_id(config: Config) -> int | None:
    """Print the chat id(s) that have messaged this bot, and flag the classic mistake.

    Deliberately uses plain HTTP rather than starting an `Application`: this is
    the command you reach for when the bot won't start, so it must not depend
    on the polling machinery working.
    """
    import httpx

    config.require_telegram(need_chat_id=False)
    token = config.telegram_bot_token
    api = f"https://api.telegram.org/bot{token}"
    bot_id = token.split(":")[0]

    try:
        me = httpx.get(f"{api}/getMe", timeout=CONNECT_TIMEOUT).json()
    except httpx.HTTPError as exc:
        log.error("Cannot reach api.telegram.org: %s", exc)
        return None

    if not me.get("ok"):
        log.error("Telegram rejected the token: %s", me.get("description"))
        return None

    username = me.get("result", {}).get("username")
    log.info("Bot: @%s (bot id %s)", username, bot_id)

    if config.telegram_chat_id == bot_id:
        log.warning(
            "TELEGRAM_CHAT_ID is set to %s, which is the BOT's own id (the number "
            "before ':' in the token). Sends will fail with 'the bot can't send "
            "messages to the bot'. You need YOUR chat id, below.",
            bot_id,
        )
    elif config.telegram_chat_id:
        # Validate what is already configured. This path does not touch
        # getUpdates, so it keeps working while `--bot-poll` is running and
        # draining the update queue.
        try:
            chat = httpx.get(
                f"{api}/getChat",
                params={"chat_id": config.telegram_chat_id},
                timeout=CONNECT_TIMEOUT,
            ).json()
        except httpx.HTTPError as exc:
            log.error("getChat failed: %s", exc)
            return None

        if chat.get("ok"):
            result = chat.get("result", {})
            name = result.get("first_name") or result.get("title") or ""
            log.info(
                "✅ TELEGRAM_CHAT_ID=%s is valid (%s%s). Nothing to change.",
                config.telegram_chat_id,
                result.get("type", "?"),
                f", {name}" if name else "",
            )
            return int(config.telegram_chat_id)

        log.warning(
            "TELEGRAM_CHAT_ID=%s is not usable: %s. Looking for alternatives…",
            config.telegram_chat_id,
            chat.get("description"),
        )

    try:
        payload = httpx.get(f"{api}/getUpdates", timeout=CONNECT_TIMEOUT).json()
    except httpx.HTTPError as exc:
        log.error("getUpdates failed: %s", exc)
        return None

    chats: dict[int, tuple[str, str]] = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = (
                chat.get("type", "?"),
                chat.get("first_name") or chat.get("title") or "",
            )

    if not chats:
        log.warning(
            "No pending messages. Either (a) a `--bot-poll` session is running and "
            "has already consumed them — stop it and retry, or (b) you have not "
            "messaged @%s yet. Send it any message, then re-run. Telegram only "
            "retains updates for 24h.",
            username,
        )
        return None

    first: int | None = None
    for chat_id, (kind, name) in chats.items():
        log.info("Your chat id: %s  (%s%s)", chat_id, kind, f", {name}" if name else "")
        first = first if first is not None else chat_id

    # Don't tell someone to set a value they have already set — that reads like
    # a failure when it is in fact the success case.
    if config.telegram_chat_id and str(first) == config.telegram_chat_id:
        log.info("✅ TELEGRAM_CHAT_ID already matches. Nothing to change.")
    else:
        log.info("Put this in .env:  TELEGRAM_CHAT_ID=%s", first)
    return first


def notify(config: Config, limit: int | None = None) -> int:
    """One-shot push of due cards. Safe to run from cron; exits immediately."""
    config.require_telegram()
    bot = DeepRecallBot(config)
    return asyncio.run(bot.push_due(config.telegram_chat_id, limit=limit))


def poll(config: Config, duration_seconds: int | None = None) -> None:
    """Run the interactive bot. Stops after `duration_seconds` if given.

    A bounded run is what makes this free: GitHub Actions starts a session after
    the daily push, serves the buttons for a while, then exits.
    """
    bot = DeepRecallBot(config)
    app = bot.build_application()

    if duration_seconds:
        async def _stop(_: ContextTypes.DEFAULT_TYPE) -> None:
            log.info("Poll window of %ds elapsed; shutting down.", duration_seconds)
            app.stop_running()

        app.job_queue.run_once(_stop, duration_seconds)
        log.info("Polling for %d seconds…", duration_seconds)
    else:
        log.info("Polling indefinitely (Ctrl-C to stop)…")

    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)
