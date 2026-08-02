---
id: "kafka-retry-patterns-dec"
cluster: "kafka-retry-patterns"
kind: "decision"
topic: "Distributed Systems"
subject: "Kafka retry topics"
created: "2026-07-29"
next_review: "2026-07-29"
interval: 0
ease_factor: 2.5
repetition_count: 0
body_hash: "8fc4b3f8d952bc97"
---

# Q: Which constraint changes how you use Kafka retry topics, and what does each choice cost?

## Scenario
Your `orders` consumer lag alert fires at 03:00. One malformed record has been failing for twenty minutes and 40k messages are stacked behind it on partition 3.

## Decision Matrix
- **IF partition progress must not stall:** publish to a retry topic and commit the offset — *trade-off:* the record is reordered relative to its partition-mates
- **IF strict per-key ordering is required:** call `pause()` on the TopicPartition and retry in place — *trade-off:* throughput for that partition is 0 until it succeeds
