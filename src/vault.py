"""The Obsidian vault: card model, Markdown (de)serialisation, and due queries.

A card is a plain Markdown file with YAML frontmatter, readable and editable in
Obsidian with no plugins:

    vault/<topic-slug>/<card-id>.md

Parsing is deliberately *structure preserving*: unknown frontmatter keys and
unknown `##` sections survive a read/write round trip untouched, so hand-edits
in Obsidian are never silently destroyed by the bot writing back a rating.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import yaml

from .sm2 import DEFAULT_EASE_FACTOR, ReviewState, is_due

log = logging.getLogger(__name__)

FRONTMATTER_DELIM = "---"

SECTION_MECHANISM = "Direct Mechanism"
SECTION_MATRIX = "Decision Matrix"
SECTION_TIPPING_POINT = "Tipping Point (When is this WRONG?)"
SECTION_MODIFIERS = "Constraint Modifiers"

#: Canonical section order when writing a card.
SECTION_ORDER = (
    SECTION_MECHANISM,
    SECTION_MATRIX,
    SECTION_TIPPING_POINT,
    SECTION_MODIFIERS,
)

#: Frontmatter key order — matches the documented schema.
FRONTMATTER_ORDER = (
    "id",
    "topic",
    "created",
    "next_review",
    "interval",
    "ease_factor",
    "repetition_count",
    "source_block_id",
    "source_note_id",
    "source_hash",
    "body_hash",
    "source_url",
    "last_reviewed",
    "revised",
)

#: Frontmatter carried across a regeneration — review history must survive an
#: edit to the source note, or every Notion tweak would reset the schedule.
REVIEW_STATE_KEYS = (
    "created",
    "interval",
    "ease_factor",
    "repetition_count",
    "next_review",
    "last_reviewed",
)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_BOLD_LEAD = re.compile(r"^\*\*(?P<lead>.+?):?\*\*:?\s*(?P<rest>.*)$")
_ITALIC_LEAD = re.compile(r"^\*(?P<lead>.+?):?\*:?\s*(?P<rest>.*)$")


class VaultError(RuntimeError):
    """Raised for unrecoverable vault I/O or parse problems."""


def slugify(value: str, max_length: int = 60) -> str:
    """`"Distributed Systems!" -> "distributed-systems"`, safe as a filename.

    Kept short because card ids travel inside Telegram `callback_data`, which
    is capped at 64 bytes by the Bot API.
    """
    normalised = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = _SLUG_STRIP.sub("-", normalised.lower()).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "untitled"


def content_digest(text: str) -> str:
    """Short stable digest of some text.

    Trailing whitespace and blank-line churn are normalised away so that
    cosmetic edits in Notion or Obsidian don't read as content changes.
    """
    normalised = "\n".join(line.rstrip() for line in (text or "").strip().splitlines())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip().strip('"'))
    except ValueError:
        log.warning("Unparseable date %r in frontmatter; treating as unset", value)
        return None


# ---------------------------------------------------------------------------
# Card
# ---------------------------------------------------------------------------


@dataclass
class Card:
    """One flashcard. `meta` is the full frontmatter; `sections` the `##` bodies."""

    id: str
    topic: str
    question: str
    sections: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    # -- SM-2 view over the frontmatter ----------------------------------

    @property
    def review_state(self) -> ReviewState:
        return ReviewState(
            interval=int(self.meta.get("interval", 0) or 0),
            ease_factor=float(self.meta.get("ease_factor", DEFAULT_EASE_FACTOR) or DEFAULT_EASE_FACTOR),
            repetition_count=int(self.meta.get("repetition_count", 0) or 0),
            next_review=_parse_date(self.meta.get("next_review")),
        )

    def apply_review(self, state: ReviewState, reviewed_on: date | None = None) -> None:
        """Write an SM-2 result back into the frontmatter."""
        self.meta["interval"] = state.interval
        self.meta["ease_factor"] = round(state.ease_factor, 2)
        self.meta["repetition_count"] = state.repetition_count
        if state.next_review:
            self.meta["next_review"] = state.next_review.isoformat()
        self.meta["last_reviewed"] = (reviewed_on or date.today()).isoformat()

    def is_due(self, today: date | None = None) -> bool:
        return is_due(self.review_state.next_review, today)

    # -- provenance -------------------------------------------------------

    def body_digest(self) -> str:
        """Digest of the question and sections — everything a human edits."""
        parts = [self.question.strip()]
        for title in sorted(self.sections):
            parts.append(f"## {title}\n{self.sections[title].strip()}")
        return content_digest("\n\n".join(parts))

    def is_hand_edited(self) -> bool:
        """True if the body changed since DeepRecall last generated it.

        `body_hash` is written only by the synthesizer, never by `save()`, so
        rating a card does not reset it. A mismatch therefore means you edited
        the card in Obsidian — and regenerating would destroy that work.
        """
        stored = self.meta.get("body_hash")
        return bool(stored) and str(stored) != self.body_digest()

    def carry_review_state_from(self, previous: "Card") -> None:
        """Adopt a previous card's identity and SM-2 history.

        Used when a source note changed and the card was regenerated: the
        content is new, but it is the *same* card as far as scheduling and the
        vault filename are concerned.
        """
        self.id = previous.id
        self.meta["id"] = previous.id
        # Keep the original topic so the file does not move to a new folder and
        # leave a duplicate behind.
        self.topic = previous.topic
        self.meta["topic"] = previous.topic
        self.path = previous.path

        for key in REVIEW_STATE_KEYS:
            if key in previous.meta:
                self.meta[key] = previous.meta[key]

    # -- Content accessors used by the Telegram flow ----------------------

    @property
    def direct_mechanism(self) -> str:
        return self.sections.get(SECTION_MECHANISM, "").strip()

    @property
    def decision_matrix(self) -> list[str]:
        return _bullets(self.sections.get(SECTION_MATRIX, ""))

    @property
    def tipping_point(self) -> str:
        return self.sections.get(SECTION_TIPPING_POINT, "").strip()

    @property
    def constraint_modifiers(self) -> list[str]:
        return _bullets(self.sections.get(SECTION_MODIFIERS, ""))

    def key_checkpoints(self) -> str:
        """A partial reveal: the mechanics, plus decision-matrix *conditions*
        with their answers redacted, so recall is still being tested."""
        parts: list[str] = []
        if self.direct_mechanism:
            parts.append(self.direct_mechanism)

        conditions = [cond for cond in (_condition_of(b) for b in self.decision_matrix) if cond]
        if conditions:
            hidden = "\n".join(f"• {cond} → ❓" for cond in conditions)
            parts.append(f"Constraints in play (choices still hidden):\n{hidden}")

        return "\n\n".join(parts) if parts else "_No mechanism recorded on this card._"

    # -- Serialisation ----------------------------------------------------

    def to_markdown(self) -> str:
        meta = dict(self.meta)
        meta.setdefault("id", self.id)
        meta.setdefault("topic", self.topic)
        meta.setdefault("created", date.today().isoformat())

        body = [f"# Q: {self.question.strip()}", ""]
        written: set[str] = set()
        for title in SECTION_ORDER:
            content = self.sections.get(title, "").strip()
            if not content:
                continue
            written.add(title)
            body.extend([f"## {title}", content, ""])
        # Preserve any sections a human added in Obsidian.
        for title, content in self.sections.items():
            if title in written or not content.strip():
                continue
            body.extend([f"## {title}", content.strip(), ""])

        return f"{_dump_frontmatter(meta)}\n{chr(10).join(body).rstrip()}\n"

    @classmethod
    def from_markdown(cls, text: str, path: Path | None = None) -> "Card":
        meta, body = _split_frontmatter(text)
        question, sections = _parse_body(body)

        card_id = str(meta.get("id") or (path.stem if path else slugify(question)))
        topic = str(meta.get("topic") or (path.parent.name if path else "Uncategorised"))
        meta["id"] = card_id
        meta["topic"] = topic

        return cls(id=card_id, topic=topic, question=question, sections=sections, meta=meta, path=path)


def _bullets(section: str) -> list[str]:
    """Extract top-level bullet lines, keeping their inline Markdown."""
    out: list[str] = []
    for line in section.splitlines():
        match = _BULLET.match(line)
        if match and not line.startswith(("  ", "\t")):
            out.append(match.group(1).strip())
    return out


def _condition_of(bullet: str) -> str:
    """`"**IF ordering matters:** pause the consumer" -> "IF ordering matters"`."""
    match = _BOLD_LEAD.match(bullet) or _ITALIC_LEAD.match(bullet)
    if match:
        return match.group("lead").strip()
    # Fall back to text before the first colon, if it looks like a condition.
    head, sep, _ = bullet.partition(":")
    if sep and len(head) < 120:
        return head.strip().strip("*")
    return ""


# ---------------------------------------------------------------------------
# Markdown <-> data
# ---------------------------------------------------------------------------


def is_card_markdown(text: str) -> bool:
    """True if this file is a flashcard rather than an ordinary note.

    A vault is a normal Obsidian folder: READMEs, index notes, daily notes and
    scratch files live beside the cards. Without this check a README gets
    scheduled for review — it has no `next_review`, so it looks permanently
    due and never leaves the queue.

    The discriminator is YAML frontmatter carrying an `id`, which every
    generated card has and ordinary prose does not.
    """
    try:
        meta, _ = _split_frontmatter(text)
    except VaultError:
        return False
    return bool(meta.get("id"))


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    stripped = text.lstrip("﻿")
    if not stripped.startswith(FRONTMATTER_DELIM):
        return {}, stripped

    lines = stripped.splitlines()
    for idx in range(1, len(lines)):
        if lines[idx].strip() == FRONTMATTER_DELIM:
            raw = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :])
            try:
                meta = yaml.safe_load(raw) or {}
            except yaml.YAMLError as exc:
                raise VaultError(f"Malformed YAML frontmatter: {exc}") from exc
            if not isinstance(meta, dict):
                raise VaultError("Frontmatter must be a YAML mapping")
            return meta, body
    # Opening delimiter with no close — treat the whole file as body.
    return {}, stripped


def _parse_body(body: str) -> tuple[str, dict[str, str]]:
    question = ""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is not None:
            sections[current] = "\n".join(buffer).strip()

    for line in body.splitlines():
        if line.startswith("## "):
            flush()
            current = line[3:].strip()
            buffer = []
        elif line.startswith("# ") and current is None:
            heading = line[2:].strip()
            question = heading[2:].strip() if heading.upper().startswith("Q:") else heading
        else:
            buffer.append(line)
    flush()

    return question, sections


def _dump_frontmatter(meta: dict[str, Any]) -> str:
    """Emit YAML with a stable key order and quoted string scalars.

    Hand-rolled rather than `yaml.safe_dump` so the on-disk schema matches the
    documented example exactly (quoted dates, unquoted numerics) and diffs stay
    minimal across runs.
    """
    ordered: list[str] = []
    keys = [k for k in FRONTMATTER_ORDER if k in meta]
    keys += [k for k in meta if k not in FRONTMATTER_ORDER]

    for key in keys:
        value = meta[key]
        if value is None:
            continue
        ordered.append(f"{key}: {_scalar(value)}")

    return f"{FRONTMATTER_DELIM}\n" + "\n".join(ordered) + f"\n{FRONTMATTER_DELIM}\n"


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return f'"{value.isoformat()[:10]}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


class Vault:
    """Filesystem-backed collection of cards rooted at `vault/`."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, card: Card) -> Path:
        return self.root / slugify(card.topic) / f"{card.id}.md"

    def iter_paths(self) -> Iterator[Path]:
        if not self.root.exists():
            return
        yield from sorted(self.root.rglob("*.md"))

    def load_all(self) -> list[Card]:
        """Load every card, skipping non-card notes and anything unparseable."""
        cards: list[Card] = []
        for path in self.iter_paths():
            card = self.try_load(path)
            if card is not None:
                cards.append(card)
        return cards

    def load(self, path: Path) -> Card:
        return Card.from_markdown(path.read_text(encoding="utf-8"), path=path)

    def try_load(self, path: Path) -> Card | None:
        """Load a card, or return None if the file is not one (or is broken)."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.error("Skipping unreadable file %s: %s", path, exc)
            return None

        if not is_card_markdown(text):
            log.debug("Skipping non-card note %s (no `id` in frontmatter)", path)
            return None

        try:
            return Card.from_markdown(text, path=path)
        except VaultError as exc:
            log.error("Skipping malformed card %s: %s", path, exc)
            return None

    def find(self, card_id: str) -> Card | None:
        """Look up by id. Tries the conventional path first, then scans."""
        direct = list(self.root.glob(f"*/{card_id}.md")) + list(self.root.glob(f"{card_id}.md"))
        for path in direct:
            card = self.try_load(path)
            if card is not None:
                return card

        for card in self.load_all():
            if card.id == card_id:
                return card
        return None

    def exists(self, card_id: str) -> bool:
        return self.find(card_id) is not None

    def known_ids(self) -> set[str]:
        """Every id a note could already be filed under.

        The LLM rewrites the question, so a card's own id rarely matches the
        slug of the Notion note it came from. Cards therefore also record
        `source_note_id`, and `--sync` checks both — otherwise every daily run
        would re-synthesise the same notes under slightly different ids.
        """
        return set(self.index_by_source())

    def index_by_source(self) -> dict[str, Card]:
        """Map every identifier a note could match on to its existing card.

        Keys are the Notion block id (stable when you reword a question), the
        slug of the original question (legacy cards predating block ids), and
        the card's own id.
        """
        index: dict[str, Card] = {}
        for card in self.load_all():
            for key in (
                card.meta.get("source_block_id"),
                card.meta.get("source_note_id"),
                card.id,
            ):
                if key:
                    index.setdefault(str(key), card)
        return index

    def due_cards(self, today: date | None = None, limit: int | None = None) -> list[Card]:
        """Cards with `next_review <= today`, most overdue first."""
        due = [card for card in self.load_all() if card.is_due(today)]
        due.sort(key=lambda c: (c.review_state.next_review or date.min, c.id))
        return due[:limit] if limit else due

    def save(self, card: Card) -> Path:
        """Write a card atomically, so a crash mid-write cannot truncate it."""
        path = card.path or self.path_for(card)
        path.parent.mkdir(parents=True, exist_ok=True)

        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(card.to_markdown(), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise VaultError(f"Could not write card to {path}: {exc}") from exc

        card.path = path
        return path

    def stats(self, today: date | None = None) -> dict[str, int]:
        cards = self.load_all()
        today = today or date.today()
        return {
            "total": len(cards),
            "due": sum(1 for c in cards if c.is_due(today)),
            "new": sum(1 for c in cards if c.review_state.repetition_count == 0),
            "mature": sum(1 for c in cards if c.review_state.interval >= 21),
            "topics": len({c.topic for c in cards}),
        }
