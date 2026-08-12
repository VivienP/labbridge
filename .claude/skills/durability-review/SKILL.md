---
name: durability-review
description: Use when writing or reviewing anything that persists state, coordinates concurrent actors, handles at-least-once delivery, leases work, retries, recovers from a crash, or reconstructs state by replay. The systems checklist for LabBridge's durable runtime.
paths: src/labbridge/infrastructure/**, src/labbridge/worker/**, src/labbridge/application/**, src/labbridge/domain/events.py, src/labbridge/domain/budgets.py
---

# Durability review

Authority: `AI_CONTRACT.md` invariants 2, 4, 5, 6, 9 and §6; `docs/SPEC.md` §4–§8;
`docs/ARCHITECTURE_DECISIONS.md` ADR-001, ADR-002; `docs/FAILURE_MATRIX.md`.

The purpose of this checklist is to make it impossible to write code that merely *appears* durable.
Work through the questions that apply. An unanswered question is an unproven guarantee.

## 1. Transaction boundaries

For every code path with more than one durable effect:

- What is inside the transaction? What is outside? Write it down explicitly.
- What inconsistency would partial success create?
- Recording an attempt outcome, appending its event, updating the required projection, and finalising
  budget consumption must be **one** transaction wherever partial success would corrupt the campaign
  (`AI_CONTRACT.md` §6).
- Is there a `commit()`, a session boundary, or an `await` on an external service between two effects
  that must be atomic?
- Object bytes may be uploaded outside the database transaction — but then the workflow must use
  staged / `pending` / `committed` / `failed` artifact states so the database never marks missing bytes
  as committed evidence.
- On rollback, what durable state remains? Is it reconcilable, or is it an orphan nobody will find?

Anti-patterns: two sessions in one logical operation; an event appended and a projection updated
separately; a budget decrement outside the outcome transaction; a `try/except` that logs a rollback and
continues as though it succeeded.

## 2. Idempotency

- Where does the idempotency key come from? It must be derived from stable canonical inputs — never a
  timestamp, a per-call UUID, an unordered mapping, or a `repr`.
- Which **database uniqueness constraint** enforces it? Name it. An in-memory set, an application
  cache, a `SELECT`-then-`INSERT`, or an `if not exists` check is not an implementation of invariant 5.
- What happens on the second delivery? It must produce one accepted outcome plus a
  `duplicate_suppressed` record — not a second acceptance, not an unhandled `IntegrityError`.
- Is the constraint violation caught specifically and translated into the duplicate path, or does it
  escape as a generic failure?
- Does at-least-once delivery combined with lease recovery still yield at most one accepted observation
  per intended attempt (invariant 5, PO-02)?

## 3. Concurrency

Enumerate the concurrent actors before reasoning: two workers; a worker and an API request; a worker and
a reconciler; a retry and a lease reclaim; a cancellation and an in-flight attempt.

- Event append: does it use an expected aggregate version? What happens on conflict — retry from the
  new state, or a stable concurrency error (F-029)?
- Job claiming: is it a single atomic conditional statement? A read followed by an update lets two
  workers claim the same job.
- Budget reservation: row lock, or an atomic conditional update that cannot pass the hard limit?
  Sequential arithmetic in Python is not concurrency control (invariant 9, F-030).
- Aggregate `sequence`: unique and monotonic per aggregate. Is the gap policy documented?
- Can you write the interleaving that breaks it? If you cannot construct one, do not claim a race; if
  you can, it is real.

## 4. Leases, heartbeats, and crash recovery

- Does the durable job record status, available-at, lease owner, lease expiry, heartbeat time, attempt
  count, idempotency key, and command payload version (`AI_CONTRACT.md` §6)?
- Is the lease clock injectable so a test can expire a lease without waiting?
- On reclaim: can the reclaiming worker duplicate an already committed outcome? Prove not.
- If the original worker returns after losing its lease, can it commit? The answer must be no, enforced
  by a compare-and-set on lease ownership or attempt token — not by a comment (F-008).
- Which late-result policy does this repository use (`docs/FAILURE_MATRIX.md` §4)? Does the code
  implement the one the documentation states?
- For each of the six kill points in `docs/FAILURE_MATRIX.md` §6 — after lease, after adapter response,
  during upload, after upload, before the outcome transaction, after commit — what is the durable state,
  and what does restart do with it?

## 5. Retry safety

- Does a retry create a **new** attempt, leaving the previous outcome intact (`docs/SPEC.md` §7.3)?
- Does retryability come from an explicit stable failure code with a recorded rationale, rather than
  from a broad exception class (`docs/FAILURE_MATRIX.md` §3)?
- Is the retry cap enforced, and does it lead to quarantine rather than unbounded retry (F-032)?
- Is backoff bounded and test-configurable so no test depends on a real long wait?
- Is a retry safe to perform after a partial effect — that is, is the retried operation idempotent?

## 6. State machines

- Campaign, work item, attempt, and job are four separate machines (`docs/SPEC.md` §7). Did the change
  merge two of them?
- Are illegal transitions rejected in one testable place, or opportunistically at call sites?
- Are terminal states genuinely terminal? `BUDGET_EXHAUSTED` is a valid terminal state, not an
  exception.
- Does cancellation stop new reservations and handle leased work by the declared policy (F-033, F-034)?

## 7. Replay and determinism

- Does replay order by aggregate `sequence`, never by timestamp?
- Does an unknown event type or an unsupported `schema_version` fail explicitly, rather than being
  skipped or coerced (F-039)?
- Is any upcaster deterministic and tested?
- Does anything in the reconstruction path read the wall clock, use unseeded randomness, or depend on
  `dict` / `set` iteration order?
- Does the reconstructed state get compared against the **persisted** projection, or against the
  in-memory object it was built from? Only the former proves PO-01.

## 8. Restart safety and partial failure

For each failure point, answer: what is durable, what is lost, what is reconcilable?

- bytes received but not uploaded → lost temporary bytes are acceptable; a phantom accepted outcome is
  not (F-004);
- object uploaded but transaction not committed → the object is `pending`/orphaned and reconciliation
  must resolve it by checksum without accepting a duplicate (F-006);
- transaction committed but acknowledgement lost → redelivery must be a no-op or
  `duplicate_suppressed` (F-007).

## 9. Retention

- If bytes were received, is an `Observation` created and content-addressed **before** any validation
  can reject it? The commonest way this project loses evidence is a validation `return` placed before
  persistence (invariant 2, ADR-005).
- Does every attempt produce a durable `AttemptOutcome`, including timeout, pre-data failure, lease
  loss, cancellation, and duplicate suppression?
- Is an expected failed attempt ever swallowed into a log without a durable outcome? That is forbidden.

## 10. Proof adequacy

Before claiming any of these, check that the test proves it at the right layer:

| Guarantee | Adequate proof |
|---|---|
| durable | integration test against real PostgreSQL |
| crash-safe | real process termination and restart |
| idempotent | two real actors racing the real unique constraint |
| budget-safe | concurrent transactions against real PostgreSQL |
| replayable | replay from persisted events compared to persisted state |
| reconcilable | a real object left `pending`, then reconciled |

`AI_CONTRACT.md` §9: *"A test that mocks away the database transaction, object store, or process
boundary does not prove the corresponding operational guarantee."*

## 11. Wording

When documenting what you built, use the substitutions in the `evidence-status-discipline` skill. In
particular: this system provides **at-least-once delivery with idempotent effect handling**, not
exactly-once execution; and **deterministic state reconstruction**, not deterministic execution.
