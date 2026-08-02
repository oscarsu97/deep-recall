---
id: "kafka-retry-patterns-tip"
cluster: "kafka-retry-patterns"
kind: "tipping"
topic: "Distributed Systems"
subject: "Kafka retry topics"
created: "2026-07-29"
next_review: "2026-07-29"
interval: 0
ease_factor: 2.5
repetition_count: 0
body_hash: "da9afbccd69d7e3a"
---

# Q: When is Kafka retry topics the wrong answer, and what wins instead?

## Scenario
Your `orders` consumer lag alert fires at 03:00. One malformed record has been failing for twenty minutes and 40k messages are stacked behind it on partition 3.

## Tipping Point (When is this WRONG?)
Wrong when retry delay must vary per message: the delay is encoded by topic, so every schedule costs a topic. Use RabbitMQ per-message TTL with a dead-letter exchange.
