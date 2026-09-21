# Ingest pipeline: architecture decision record

## Context

The ingest pipeline currently processes roughly 40,000 events per minute at
peak. We need to support a new tenant expected to add a comparable volume.

## Decision

We will replace the single-writer Postgres ingest path with a partitioned
queue-backed writer. The system will be highly scalable and secure.

## Consequences

- Throughput increases substantially.
- Operational complexity increases: there is now a queue to run.
- Migration requires a dual-write window.

## Rollback

If throughput regresses, revert to the single-writer path.
