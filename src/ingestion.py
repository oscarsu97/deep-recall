"""Notion ingestion: pull raw Q&A notes out of pages edited recently.

DeepRecall expects notes written the way people actually take them — a heading
or toggle that poses a question, with messy nested bullets underneath as the
answer:

    ▸ How does Kafka avoid copying message bytes into user space?     <- toggle
        - sendfile(2), page cache straight to socket
        - breaks if TLS is on, then it's back to userspace copies
    ## When would you not use Kafka?                                  <- heading
    - per-message routing, granular retries -> Rabbit

Three shapes are recognised as questions: toggle blocks, headings, and bullets
whose text ends in `?`. Everything nested beneath (to any depth) becomes the
raw answer, flattened to indented plain text for the LLM.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, TypeVar

from .config import Config
from .vault import slugify

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Blocks that can pose a question.
HEADING_TYPES = ("heading_1", "heading_2", "heading_3")
QUESTION_TYPES = ("toggle", *HEADING_TYPES)

#: Blocks whose text we flatten into an answer body.
TEXT_TYPES = (
    "paragraph",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
    "quote",
    "callout",
    "code",
    "toggle",
    *HEADING_TYPES,
)

MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 1.5
MAX_BLOCK_DEPTH = 6


class IngestionError(RuntimeError):
    """Raised when Notion cannot be reached or returns something unusable."""


@dataclass
class RawNote:
    """One question plus its unstructured answer text, straight from Notion."""

    question: str
    raw_answer: str
    page_title: str = ""
    page_id: str = ""
    source_url: str = ""
    breadcrumbs: list[str] = field(default_factory=list)

    @property
    def suggested_id(self) -> str:
        return slugify(self.question, max_length=48)

    @property
    def suggested_topic(self) -> str:
        """Topic defaults to the Notion page title — a natural folder name."""
        return self.page_title or "Uncategorised"

    def is_substantive(self, min_chars: int = 40) -> bool:
        """Skip empty toggles and one-word stubs; they waste LLM quota."""
        return len(self.raw_answer.strip()) >= min_chars and len(self.question.strip()) >= 8


# ---------------------------------------------------------------------------
# Rich text helpers
# ---------------------------------------------------------------------------


def rich_text_to_plain(rich_text: Iterable[dict[str, Any]] | None) -> str:
    if not rich_text:
        return ""
    return "".join(part.get("plain_text", "") for part in rich_text).strip()


def _block_text(block: dict[str, Any]) -> str:
    btype = block.get("type", "")
    payload = block.get(btype) or {}

    if btype == "code":
        code = rich_text_to_plain(payload.get("rich_text"))
        language = payload.get("language", "")
        return f"```{language}\n{code}\n```" if code else ""

    text = rich_text_to_plain(payload.get("rich_text"))
    if btype == "to_do":
        mark = "x" if payload.get("checked") else " "
        return f"[{mark}] {text}" if text else ""
    return text


def _page_title(page: dict[str, Any]) -> str:
    """Notion puts the title under a differently-named property per database."""
    props = page.get("properties") or {}
    for prop in props.values():
        if prop.get("type") == "title":
            title = rich_text_to_plain(prop.get("title"))
            if title:
                return title
    # Pages (not database rows) expose the title directly.
    title_prop = props.get("title") or {}
    return rich_text_to_plain(title_prop.get("title")) or "Untitled"


# ---------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------


class NotionIngestor:
    """Reads Q&A structures out of Notion. Instantiating it requires a token."""

    def __init__(self, config: Config, client: Any | None = None):
        config.require_notion()
        self.config = config
        self._client = client or self._build_client(config.notion_token)

    @staticmethod
    def _build_client(token: str) -> Any:
        try:
            from notion_client import Client
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise IngestionError(
                "notion-client is not installed. Run: pip install -r requirements.txt"
            ) from exc
        return Client(auth=token)

    # -- transport --------------------------------------------------------

    def _retrying(self, fn: Callable[[], T], what: str) -> T:
        """Notion rate-limits at ~3 req/s; retry those and 5xx, fail fast on 4xx."""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - normalised below
                status = getattr(exc, "status", None) or getattr(exc, "code", None)
                retryable = status in (409, 429, 500, 502, 503, 504) or status is None
                last_exc = exc
                if not retryable or attempt == MAX_RETRIES - 1:
                    break
                delay = BASE_BACKOFF_SECONDS * (2**attempt)
                log.warning(
                    "Notion %s failed (%s); retrying in %.1fs [%d/%d]",
                    what, exc, delay, attempt + 1, MAX_RETRIES,
                )
                time.sleep(delay)
        raise IngestionError(f"Notion {what} failed: {last_exc}") from last_exc

    def _paginate(self, method: Callable[..., dict[str, Any]], what: str, **kwargs: Any) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            payload = self._retrying(
                lambda: method(**kwargs, start_cursor=cursor) if cursor else method(**kwargs),
                what,
            )
            results.extend(payload.get("results", []))
            if not payload.get("has_more"):
                return results
            cursor = payload.get("next_cursor")
            if not cursor:
                return results

    # -- page discovery ---------------------------------------------------

    def recent_pages(self, lookback_hours: int | None = None) -> list[dict[str, Any]]:
        """Pages edited within the lookback window, plus any explicitly pinned ids."""
        hours = lookback_hours if lookback_hours is not None else self.config.ingest_lookback_hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        pages: list[dict[str, Any]] = []
        seen: set[str] = set()

        for page_id in self.config.notion_page_ids:
            page = self._retrying(lambda pid=page_id: self._client.pages.retrieve(page_id=pid), "page fetch")
            if page.get("id") not in seen:
                seen.add(page["id"])
                pages.append(page)

        if self.config.notion_database_id:
            found = self._paginate(
                self._client.databases.query,
                "database query",
                database_id=self.config.notion_database_id,
                filter={
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"on_or_after": cutoff.isoformat()},
                },
            )
        else:
            found = self._paginate(
                self._client.search,
                "search",
                filter={"property": "object", "value": "page"},
                sort={"direction": "descending", "timestamp": "last_edited_time"},
            )

        for page in found:
            page_id = page.get("id")
            if not page_id or page_id in seen:
                continue
            if page.get("archived") or page.get("in_trash"):
                continue
            edited = page.get("last_edited_time")
            # The search endpoint cannot filter by time, so filter client-side.
            if edited and not self.config.notion_database_id:
                try:
                    if datetime.fromisoformat(edited.replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    pass
            seen.add(page_id)
            pages.append(page)

        log.info("Found %d Notion page(s) edited in the last %dh", len(pages), hours)
        return pages

    # -- block traversal --------------------------------------------------

    def _children(self, block_id: str) -> list[dict[str, Any]]:
        return self._paginate(
            self._client.blocks.children.list, "block children", block_id=block_id
        )

    def _flatten(self, blocks: list[dict[str, Any]], depth: int = 0) -> str:
        """Render a block subtree as indented plain text."""
        if depth > MAX_BLOCK_DEPTH:
            return ""

        lines: list[str] = []
        indent = "  " * depth
        for block in blocks:
            if block.get("type") not in TEXT_TYPES:
                continue
            text = _block_text(block)
            if text:
                prefix = "- " if depth or block.get("type").endswith("list_item") else ""
                lines.append(f"{indent}{prefix}{text}")
            if block.get("has_children"):
                try:
                    nested = self._flatten(self._children(block["id"]), depth + 1)
                except IngestionError as exc:
                    log.warning("Could not read children of block %s: %s", block.get("id"), exc)
                    nested = ""
                if nested:
                    lines.append(nested)
        return "\n".join(line for line in lines if line.strip())

    def _extract(
        self,
        blocks: list[dict[str, Any]],
        page: dict[str, Any],
        breadcrumbs: list[str],
        depth: int = 0,
    ) -> list[RawNote]:
        """Pull `RawNote`s out of one level of blocks, recursing into containers.

        Headings own the siblings that follow them until the next heading of the
        same or higher level; toggles and question-bullets own their children.
        """
        if depth > MAX_BLOCK_DEPTH:
            return []

        notes: list[RawNote] = []
        title = _page_title(page)
        url = page.get("url", "")

        def emit(question: str, answer: str, trail: list[str]) -> None:
            note = RawNote(
                question=question.strip(),
                raw_answer=answer.strip(),
                page_title=title,
                page_id=page.get("id", ""),
                source_url=url,
                breadcrumbs=list(trail),
            )
            if note.is_substantive():
                notes.append(note)
            else:
                log.debug("Skipping thin note: %r", question[:60])

        index = 0
        while index < len(blocks):
            block = blocks[index]
            btype = block.get("type", "")
            text = _block_text(block)

            if btype in HEADING_TYPES and text:
                level = int(btype[-1])
                # Consume siblings until the next heading of same-or-higher rank.
                end = index + 1
                while end < len(blocks):
                    nxt = blocks[end].get("type", "")
                    if nxt in HEADING_TYPES and int(nxt[-1]) <= level:
                        break
                    end += 1
                body_blocks = blocks[index + 1 : end]

                own_children = self._children(block["id"]) if block.get("has_children") else []
                scoped = own_children + body_blocks

                # Nested questions win; otherwise the heading itself is the question.
                nested = self._extract(scoped, page, breadcrumbs + [text], depth + 1)
                if nested:
                    notes.extend(nested)
                else:
                    emit(text, self._flatten(scoped), breadcrumbs + [text])
                index = end
                continue

            if btype == "toggle" and text and block.get("has_children"):
                children = self._children(block["id"])
                nested = self._extract(children, page, breadcrumbs + [text], depth + 1)
                # A toggle full of sub-toggles is a container, not a question.
                if nested and not _looks_like_question(text):
                    notes.extend(nested)
                else:
                    emit(text, self._flatten(children), breadcrumbs)
                index += 1
                continue

            if btype == "bulleted_list_item" and _looks_like_question(text) and block.get("has_children"):
                emit(text, self._flatten(self._children(block["id"])), breadcrumbs)
                index += 1
                continue

            if block.get("has_children") and btype in ("column_list", "column", "callout", "quote"):
                notes.extend(
                    self._extract(self._children(block["id"]), page, breadcrumbs, depth + 1)
                )

            index += 1

        return notes

    # -- public API -------------------------------------------------------

    def fetch_notes(self, lookback_hours: int | None = None) -> list[RawNote]:
        """Ingest every recognisable Q&A pair from recently-edited pages."""
        notes: list[RawNote] = []
        for page in self.recent_pages(lookback_hours):
            page_id = page.get("id", "")
            try:
                blocks = self._children(page_id)
            except IngestionError as exc:
                log.error("Could not read page %s: %s", _page_title(page), exc)
                continue

            found = self._extract(blocks, page, breadcrumbs=[])
            log.info("  %-45s -> %d note(s)", _page_title(page)[:45], len(found))
            notes.extend(found)

        deduped = _dedupe(notes)
        log.info("Ingested %d note(s) (%d after dedupe)", len(notes), len(deduped))
        return deduped


def _looks_like_question(text: str) -> bool:
    if text.strip().endswith("?"):
        return True
    lowered = text.strip().lower()
    return lowered.startswith(("q:", "question:", "how ", "why ", "what ", "when ", "which "))


def _dedupe(notes: list[RawNote]) -> list[RawNote]:
    """Same question in two places: keep whichever has more answer material."""
    best: dict[str, RawNote] = {}
    for note in notes:
        key = note.suggested_id
        incumbent = best.get(key)
        if incumbent is None or len(note.raw_answer) > len(incumbent.raw_answer):
            best[key] = note
    return list(best.values())
