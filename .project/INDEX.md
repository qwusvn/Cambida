# Project Memory Index

Project: cambida
Source of truth: Git + filesystem + tests; this index is navigation only.

## Reading order
1. INDEX.md + NOW.md
2. Relevant documents linked below
3. CodeGraph (if indexed), Git, logs and history for deeper investigation

## Existing documents
- [PROJECT.md](PROJECT.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [DECISIONS.md](DECISIONS.md)
- [STATE.md](STATE.md)
- [TASKS.md](TASKS.md)
- [HANDOFF.md](HANDOFF.md)
- [CHANGELOG.md](CHANGELOG.md)

## Archive
- [history/](history/) - older immutable checkpoints; never delete material decisions.

## Rules
- Do not read entire memory by default.
- Only update an active project that is explicitly authorized; never overwrite concurrent writer.
- Compare NOW to Git and current task/process evidence; treat stale or conflicting memory as UNVERIFIED.
- Preserve DECISIONS.md; archive large chronological detail with a link back here. 
