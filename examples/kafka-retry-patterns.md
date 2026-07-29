---
id: "kafka-retry-patterns"
topic: "Distributed Systems"
created: "2026-07-29"
next_review: "2026-07-29"
interval: 0
ease_factor: 2.5
repetition_count: 0
---

# Q: If processing of one message fails in Kafka, how do you retry it without blocking consumption of the rest of the partition?

## Direct Mechanism
A Kafka consumer tracks one monotonic `offset` per partition, so a message that is retried in place stalls every message behind it — head-of-line blocking on the whole partition. The non-blocking pattern breaks retry state out of the consumption path: on failure, the consumer produces the payload plus a `retry-count` header to a separate retry topic (`orders-retry-30s`), then commits the original offset immediately, so the main partition advances. A second consumer group subscribes to the retry topic with a delay enforced by comparing the record's timestamp against `now` and calling `KafkaConsumer.pause()` until it matures. After `max.retries` the record is produced to a dead letter topic (`orders-dlq`) for manual replay. Note that `retry.backoff.ms` (default 100 ms) governs broker-fetch retries, not application-level failures — it does nothing for a poison message.

## Decision Matrix
- **IF partition progress must not stall on a poison message:** publish to a retry topic and commit the main offset immediately — *trade-off:* the message is now reordered relative to its partition-mates, and you have added a produce round-trip on the failure path.
- **IF strict per-key ordering is required:** call `pause()` on that `TopicPartition` and retry in place with backoff, leaving the offset uncommitted — *trade-off:* throughput for that partition drops to zero until the message succeeds or you give up; `max.poll.interval.ms` (default 300000) will evict the consumer from the group if you exceed it.
- **IF failures are transient and sub-second (a brief 503 from a downstream API):** retry in memory inside the poll loop with capped exponential backoff — *trade-off:* the retry budget must stay well under `max.poll.interval.ms`, and any state is lost if the consumer crashes.
- **IF the failure rate is high enough to saturate the retry topic:** apply a circuit breaker and stop consuming entirely rather than shovelling records sideways — *trade-off:* consumer lag grows, but the retry topic does not become an unbounded second queue you must later drain.

## Tipping Point (When is this WRONG?)
Kafka is the wrong substrate when retry policy needs to be per-message rather than per-topic. Because delay is encoded by *which topic* a record sits in, every distinct backoff schedule costs another topic and another consumer group; a workload needing arbitrary per-message delays or selective redelivery should use RabbitMQ (per-message TTL plus a dead-letter exchange) or a Postgres-backed queue with `SELECT ... FOR UPDATE SKIP LOCKED` and a `visible_at` column, where the delay is a row value and requires no new infrastructure per schedule.

## Constraint Modifiers
- *Modifier 1 (Strict Ordering):* Retry topics become unusable — a reordered record violates the per-key guarantee consumers depend on. You must pause the partition and retry synchronously, which converts the retry design from a throughput problem into a liveness problem: you now need a poison-message timeout, or one bad record halts that key forever.
- *Modifier 2 (Exactly-Once Semantics):* With `processing.guarantee=exactly_once_v2`, the produce-to-retry-topic and the offset commit must sit in the same transaction via `sendOffsetsToTransaction()`. That forbids the in-memory retry loop, since an uncommitted transaction held open across retries blocks read-committed consumers downstream.
- *Modifier 3 (Cost / Retention Limits):* If retry topics would exceed the broker's disk budget, drop the topic-per-delay design and keep a bounded in-memory retry buffer with a fixed count before the DLQ. You trade durability of the retry state for storage: a consumer crash loses in-flight retries, so the DLQ must be reachable from the source of truth instead.
