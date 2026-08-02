"""Canonical topic vocabulary.

A card's topic decides which folder it lands in (`vault/<topic-slug>/`), so a
model that invents a fresh label per card scatters one subject across a dozen
directories — "Spring Framework", "Java Frameworks", "Framework Internals" and
"Frameworks & Runtimes" are all the same shelf.

The model is *asked* to pick from `TOPICS` (the prompt embeds the list), but
`canonical()` is what actually guarantees it: every topic is snapped to the
vocabulary before the card is written. Prompt compliance is a nicety; this is
the enforcement.

To reshape the taxonomy, edit `TOPICS` and `ALIASES` here, then re-run the
retopic migration to move existing cards.
"""

from __future__ import annotations

import re

#: The only topics a card may carry. Title Case; the folder name is the slug.
TOPICS: tuple[str, ...] = (
    "Distributed Systems",
    "Data Engineering",
    "Databases",
    "Networking",
    "Java & Spring",
    "Software Architecture",
    "Concurrency",
    "Operating Systems",
    "Security",
    "Performance",
    "Financial Engineering",
)

FALLBACK = "Uncategorised"

#: Exact remaps for labels already seen in the wild. Checked before the keyword
#: heuristic, because a compound like "Framework Architecture" matches keywords
#: for two different topics and only the human knows which one it meant.
ALIASES: dict[str, str] = {
    # One subject, six labels.
    "spring framework": "Java & Spring",
    "java frameworks": "Java & Spring",
    "java systems": "Java & Spring",
    "framework internals": "Java & Spring",
    "framework architecture": "Java & Spring",
    "frameworks runtimes": "Java & Spring",
    # Narrower slices of data engineering.
    "data warehousing": "Data Engineering",
    "data processing": "Data Engineering",
    # Trading-bot cards are distributed-systems cards.
    "trading bot architecture": "Distributed Systems",
    # Domain finance notes, split across three near-identical labels.
    "financial derivatives": "Financial Engineering",
    "financial systems": "Financial Engineering",
    "application security": "Security",
}

#: Fallback for labels never seen before. Longest keyword wins, so a specific
#: phrase ("trading bot") beats the generic word it contains ("trading").
KEYWORDS: dict[str, tuple[str, ...]] = {
    "Distributed Systems": (
        "distributed", "kafka", "message queue", "messaging", "streaming",
        "event driven", "microservice", "consensus", "replication",
        "trading bot", "disruptor", "ringbuffer", "pub sub",
    ),
    "Data Engineering": (
        "data engineering", "data warehouse", "warehousing", "etl", "elt",
        "data pipeline", "data processing", "spark", "airflow", "ingestion",
        "star schema", "slowly changing dimension",
    ),
    "Databases": (
        "database", "sql", "index", "transaction", "mongo", "postgres",
        "storage engine", "lsm tree", "b tree", "acid", "sharding",
    ),
    "Networking": (
        "network", "tcp", "udp", "http", "protocol", "dns", "socket", "grpc",
    ),
    "Java & Spring": (
        "java", "spring", "jvm", "garbage collection", "framework", "runtime",
        "oop", "object oriented", "dependency injection", "hibernate", "bean",
    ),
    "Software Architecture": (
        "software architecture", "design pattern", "design principle", "solid",
        "api design", "rest", "clean architecture", "domain driven",
    ),
    "Concurrency": (
        "concurrency", "thread", "lock", "deadlock", "mutex", "semaphore",
        "async", "parallel", "race condition", "atomic",
    ),
    "Operating Systems": (
        "operating system", "kernel", "syscall", "system call", "scheduler",
        "memory management", "page fault", "process isolation",
    ),
    "Security": (
        "security", "authentication", "authorization", "encryption", "tls",
        "oauth", "jwt", "vulnerability", "cryptograph",
    ),
    "Performance": (
        "performance", "latency", "throughput", "optimization", "profiling",
        "benchmark", "caching",
    ),
    "Financial Engineering": (
        "financial", "finance", "derivative", "option pricing", "settlement",
        "exchange", "market data", "dual currency", "hedging",
    ),
}

_CANONICAL_BY_KEY = {t.lower().replace("&", "").replace("  ", " ").strip(): t for t in TOPICS}

#: Keywords anchor at a word start but may run on: "network" matches
#: "networking" and "database" matches "databases", while "lock" does not match
#: "blockchain". A plain substring test gets that last one wrong.
_MATCHERS: tuple[tuple[re.Pattern[str], int, str], ...] = tuple(
    (re.compile(r"\b" + re.escape(keyword)), len(keyword), topic)
    for topic, keywords in KEYWORDS.items()
    for keyword in keywords
)


def _normalise(value: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for matching only."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def canonical(raw: str | None) -> str:
    """Snap an arbitrary topic label onto `TOPICS`.

    Falls back to `FALLBACK` rather than guessing wildly: an obviously
    uncategorised card is easier to spot and fix than one filed plausibly but
    wrongly.
    """
    key = _normalise(raw or "")
    if not key:
        return FALLBACK

    if key in _CANONICAL_BY_KEY:
        return _CANONICAL_BY_KEY[key]
    if key in ALIASES:
        return ALIASES[key]

    best: tuple[int, str] | None = None
    for pattern, weight, topic in _MATCHERS:
        if pattern.search(key) and (best is None or weight > best[0]):
            best = (weight, topic)
    return best[1] if best else FALLBACK


def prompt_list() -> str:
    """The vocabulary as the system prompt should present it."""
    return ", ".join(f"'{t}'" for t in TOPICS)
