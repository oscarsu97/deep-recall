---
id: "kafka-retry-patterns-mod1"
cluster: "kafka-retry-patterns"
kind: "modifier"
ordinal: 1
modifier: "Strict Ordering"
topic: "Distributed Systems"
subject: "Kafka retry topics"
created: "2026-07-29"
next_review: "2026-07-29"
interval: 0
ease_factor: 2.5
repetition_count: 0
body_hash: "be9bdc0984315218"
---

# Q: Strict Ordering now holds. How must Kafka retry topics change, and why?

## Scenario
Your `orders` consumer lag alert fires at 03:00. One malformed record has been failing for twenty minutes and 40k messages are stacked behind it on partition 3.

## Constraint Modifiers
Retry topics break the per-key guarantee; pause the partition and add a poison-pill timeout instead.
