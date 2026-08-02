---
id: "kafka-retry-patterns-mech"
cluster: "kafka-retry-patterns"
kind: "mechanism"
topic: "Distributed Systems"
subject: "Kafka retry topics"
created: "2026-07-29"
next_review: "2026-07-29"
interval: 0
ease_factor: 2.5
repetition_count: 0
body_hash: "f8b91bf033764919"
---

# Q: How do you retry a failed Kafka message without blocking the partition?

## Scenario
Your `orders` consumer lag alert fires at 03:00. One malformed record has been failing for twenty minutes and 40k messages are stacked behind it on partition 3.

## Direct Mechanism
A consumer tracks one monotonic offset per partition, so retrying in place stalls everything behind it. Produce the payload to `orders-retry-30s` with a retry-count header and commit the original offset. A delay consumer calls `pause()` until the record matures, then `orders-dlq` takes it after max.retries.

## Seen In
Kafka Streams ships this as its `DeserializationExceptionHandler` dead-letter path.
