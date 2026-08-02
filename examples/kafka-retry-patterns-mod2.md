---
id: "kafka-retry-patterns-mod2"
cluster: "kafka-retry-patterns"
kind: "modifier"
ordinal: 2
modifier: "Exactly-Once"
topic: "Distributed Systems"
subject: "Kafka retry topics"
created: "2026-07-29"
next_review: "2026-07-29"
interval: 0
ease_factor: 2.5
repetition_count: 0
body_hash: "08bb807f072aef91"
---

# Q: Exactly-Once now holds. How must Kafka retry topics change, and why?

## Scenario
Your `orders` consumer lag alert fires at 03:00. One malformed record has been failing for twenty minutes and 40k messages are stacked behind it on partition 3.

## Constraint Modifiers
The produce and the offset commit must share one transaction via `sendOffsetsToTransaction()`.
