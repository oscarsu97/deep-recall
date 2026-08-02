from src import topics


def test_canonical_topics_pass_through():
    for topic in topics.TOPICS:
        assert topics.canonical(topic) == topic


def test_aliases_collapse_onto_one_shelf():
    for label in ("Spring Framework", "Java Frameworks", "Framework Internals",
                  "Framework Architecture", "Frameworks & Runtimes", "Java Systems"):
        assert topics.canonical(label) == "Java & Spring"


def test_alias_beats_keyword_for_ambiguous_compounds():
    # "Framework Architecture" matches keywords for two topics; the alias decides.
    assert topics.canonical("Framework Architecture") == "Java & Spring"


def test_unseen_labels_fall_back_to_keywords():
    assert topics.canonical("Spring Boot Internals") == "Java & Spring"
    assert topics.canonical("Kafka Streaming Internals") == "Distributed Systems"
    assert topics.canonical("MongoDB Storage Internals") == "Databases"


def test_longest_keyword_wins():
    # "trading bot" (Distributed Systems) must beat the "trading" it contains.
    assert topics.canonical("Trading Bot Internals") == "Distributed Systems"


def test_keywords_anchor_at_word_start():
    # "lock" must not match inside "blockchain", but must match "deadlock"
    # because that is a keyword in its own right.
    assert topics.canonical("Blockchain Consensus Voodoo") != "Concurrency"
    assert topics.canonical("Deadlock Detection") == "Concurrency"


def test_keywords_may_run_on_past_the_match():
    assert topics.canonical("Network Protocols") == "Networking"
    assert topics.canonical("NoSQL Databases") == "Databases"


def test_unrecognisable_and_empty_topics_are_flagged_not_guessed():
    assert topics.canonical("Quantum Basketry") == topics.FALLBACK
    assert topics.canonical("") == topics.FALLBACK
    assert topics.canonical(None) == topics.FALLBACK


def test_case_and_punctuation_are_ignored():
    assert topics.canonical("distributed systems") == "Distributed Systems"
    assert topics.canonical("  Java   &   Spring  ") == "Java & Spring"


def test_every_alias_target_is_canonical():
    for target in topics.ALIASES.values():
        assert target in topics.TOPICS


def test_every_keyword_group_is_canonical():
    for topic in topics.KEYWORDS:
        assert topic in topics.TOPICS


def test_prompt_list_names_every_topic():
    listed = topics.prompt_list()
    for topic in topics.TOPICS:
        assert f"'{topic}'" in listed
