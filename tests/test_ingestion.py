"""Ingestion tests against a fake Notion client — no network, no token."""

import pytest

from src.config import Config
from src.ingestion import IngestionError, NotionIngestor, RawNote, rich_text_to_plain

LONG = "sendfile(2) moves bytes from the page cache straight into the socket buffer."


def rt(text):
    return [{"plain_text": text}]


def block(block_id, btype, text, has_children=False, **extra):
    return {"id": block_id, "type": btype, btype: {"rich_text": rt(text), **extra},
            "has_children": has_children}


class _Namespace:
    """Turns keyword callables into attributes, like the notion_client resources."""

    def __init__(self, **members):
        self.__dict__.update(members)


class FakeNotion:
    """Mimics the notion_client surface DeepRecall actually touches."""

    def __init__(self, page, children):
        self._children = children
        self.calls = 0
        self.blocks = _Namespace(children=_Namespace(list=self._list))
        self.pages = _Namespace(retrieve=lambda **kw: page)
        self.databases = _Namespace(query=lambda **kw: {"results": [page]})
        self.search = lambda **kw: {"results": [page]}

    def _list(self, block_id, **kwargs):
        self.calls += 1
        return {"results": self._children.get(block_id, []), "has_more": False}


PAGE = {
    "id": "page-1",
    "url": "https://notion.so/page-1",
    "last_edited_time": "2099-01-01T00:00:00.000Z",
    "properties": {"Name": {"type": "title", "title": rt("Kafka Deep Dive")}},
}


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Keep retry tests instant."""
    monkeypatch.setattr("src.ingestion.BASE_BACKOFF_SECONDS", 0)


def ingestor(children):
    config = Config(notion_token="secret", notion_database_id="db-1")
    return NotionIngestor(config, client=FakeNotion(PAGE, children))


# --- helpers --------------------------------------------------------------


def test_rich_text_concatenates_styled_runs():
    assert rich_text_to_plain([{"plain_text": "max."}, {"plain_text": "poll.ms"}]) == "max.poll.ms"


def test_rich_text_handles_empty_input():
    assert rich_text_to_plain(None) == ""


# --- structure recognition ------------------------------------------------


def test_toggle_becomes_a_question_with_its_children_as_the_answer():
    notes = ingestor({
        "page-1": [block("t1", "toggle", "How does Kafka avoid user-space copies?", True)],
        "t1": [block("b1", "bulleted_list_item", LONG)],
    }).fetch_notes()

    assert len(notes) == 1
    assert notes[0].question.startswith("How does Kafka")
    assert "sendfile(2)" in notes[0].raw_answer
    assert notes[0].page_title == "Kafka Deep Dive"
    assert notes[0].source_url == "https://notion.so/page-1"


def test_heading_owns_siblings_until_the_next_heading_of_equal_rank():
    notes = ingestor({
        "page-1": [
            block("h1", "heading_2", "When is Kafka the wrong choice?"),
            block("p1", "paragraph", LONG),
            block("h2", "heading_2", "How does rebalancing work under the hood?"),
            block("p2", "paragraph", "The group coordinator revokes partitions via JoinGroup."),
        ],
    }).fetch_notes()

    by_q = {n.question: n.raw_answer for n in notes}
    assert len(by_q) == 2
    assert "sendfile(2)" in by_q["When is Kafka the wrong choice?"]
    assert "JoinGroup" in by_q["How does rebalancing work under the hood?"]


def test_a_subheading_does_not_terminate_its_parent_heading():
    notes = ingestor({
        "page-1": [
            block("h1", "heading_1", "Consumer group internals in Kafka"),
            block("h2", "heading_3", "How is the group coordinator elected?"),
            block("p1", "paragraph", LONG),
        ],
    }).fetch_notes()

    # The h3 is a nested question, so it wins over the h1 container.
    assert [n.question for n in notes] == ["How is the group coordinator elected?"]


def test_nested_bullets_are_flattened_with_indentation():
    notes = ingestor({
        "page-1": [block("t1", "toggle", "How does the retry topic pattern work?", True)],
        "t1": [block("b1", "bulleted_list_item", "produce to retry topic", True)],
        "b1": [block("b2", "bulleted_list_item", f"then commit the offset. {LONG}")],
    }).fetch_notes()

    answer = notes[0].raw_answer
    assert "produce to retry topic" in answer
    assert "  - then commit the offset" in answer


def test_question_bullets_are_recognised_by_their_trailing_question_mark():
    notes = ingestor({
        "page-1": [block("b1", "bulleted_list_item", "Why is sendfile faster here?", True)],
        "b1": [block("b2", "paragraph", LONG)],
    }).fetch_notes()

    assert [n.question for n in notes] == ["Why is sendfile faster here?"]


def test_code_blocks_are_fenced_in_the_answer():
    notes = ingestor({
        "page-1": [block("t1", "toggle", "How do you commit offsets manually?", True)],
        "t1": [block("c1", "code", "consumer.commitSync();", language="java"),
               block("p1", "paragraph", LONG)],
    }).fetch_notes()

    assert "```java\nconsumer.commitSync();\n```" in notes[0].raw_answer


# --- filtering ------------------------------------------------------------


def test_thin_notes_are_dropped():
    notes = ingestor({
        "page-1": [block("t1", "toggle", "Why?", True)],
        "t1": [block("b1", "bulleted_list_item", "dunno")],
    }).fetch_notes()
    assert notes == []


def test_empty_toggles_produce_nothing():
    assert ingestor({"page-1": [block("t1", "toggle", "How does Kafka work?", False)]}).fetch_notes() == []


def test_duplicate_questions_keep_the_richer_answer():
    notes = ingestor({
        "page-1": [
            block("t1", "toggle", "How does Kafka retry work?", True),
            block("t2", "toggle", "How does Kafka retry work?", True),
        ],
        "t1": [block("b1", "bulleted_list_item", LONG)],
        "t2": [block("b2", "bulleted_list_item", LONG + " " + LONG)],
    }).fetch_notes()

    assert len(notes) == 1
    assert notes[0].raw_answer.count("sendfile(2)") == 2


# --- resilience -----------------------------------------------------------


def test_transport_failures_are_wrapped_and_retried(monkeypatch):
    monkeypatch.setattr("src.ingestion.BASE_BACKOFF_SECONDS", 0)
    ing = ingestor({})

    def boom(**kwargs):
        raise RuntimeError("503 upstream")

    ing._client.blocks.children.list = boom
    with pytest.raises(IngestionError):
        ing._children("page-1")


def test_missing_token_is_rejected_up_front():
    from src.config import ConfigError

    with pytest.raises(ConfigError):
        NotionIngestor(Config(notion_token=""), client=object())


# --- RawNote --------------------------------------------------------------


def test_suggested_id_is_slugged_and_bounded():
    note = RawNote(question="How do you retry a failed Kafka message safely and correctly?", raw_answer="x")
    assert note.suggested_id.startswith("how-do-you-retry")
    assert len(note.suggested_id) <= 48


def test_topic_falls_back_when_the_page_has_no_title():
    assert RawNote(question="q", raw_answer="a").suggested_topic == "Uncategorised"
