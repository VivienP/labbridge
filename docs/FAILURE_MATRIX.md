# LabBridge — failure and recovery matrix

**Status:** normative V1 test specification  
**Purpose:** define how operational failures, data corruption, scientific-quality rejection, and unfavourable valid results are represented and proven.

The matrix is part of the product specification. A scenario is not complete until its expected records, state transitions, retry decision, retained evidence, and automated proof all agree.

---

## 1. Classification rules

### Operational failure

The execution could not complete correctly because of transport, storage, worker, database, or adapter failure.

### Data corruption

Bytes were received, but their content or metadata violates declared structural or scientific validation rules.

### Scientific-quality rejection

The observation is structurally readable but cannot support the requested metric or decision under the declared analysis protocol.

### Unfavourable valid result

The observation is valid and the experiment completed, but performance is poor. This is **not a runtime failure**.

### Required retention rule

- If bytes were received, create and retain an `Observation`.
- Every attempt creates an `AttemptOutcome`.
- A retry creates a new attempt.
- No failure rewrites or deletes a previous attempt.

---

## 2. Matrix

| ID | Scenario | Injection point | Expected attempt outcome | Observation retained? | Runtime action | Required proof |
|---|---|---|---|---:|---|---|
| F-001 | API request repeated with same idempotency key | Campaign creation | Existing campaign returned or stable conflict; no duplicate aggregate | N/A | No second campaign | Concurrent API integration test; one database row and one initial event |
| F-002 | Same durable job delivered twice | Before worker execution | One normal outcome; one `duplicate_suppressed` or no-op path | At most one accepted observation | Do not execute or accept twice | Two workers race on same job; uniqueness constraint proves at-most-one acceptance |
| F-003 | Worker dies before adapter call | After lease, before execution | Initial attempt `lease_lost` or equivalent after expiry | No | Reclaim job and create new attempt | Kill worker process; lease expires; second worker completes |
| F-004 | Worker dies after adapter returns but before object upload | Pre-upload | Attempt eventually classified retryable or lease-lost | No committed observation; temporary bytes may be lost | Retry safely | Process kill at boundary; no accepted result exists; later attempt completes |
| F-005 | Worker dies during object upload | Object staging | Retryable storage failure or lease-lost | No committed observation; partial object marked or cleaned as orphan | Retry upload or whole attempt according to adapter semantics | Incomplete upload never appears as committed artifact |
| F-006 | Worker dies after object upload but before database commit | Post-upload, pre-transaction | No accepted outcome from first attempt | Object exists as pending/orphan | Reconcile object, then retry without accepting duplicate | Restart detects pending object by checksum and either reuses or cleans it |
| F-007 | Worker dies after database commit but before acknowledgement | Post-transaction | Accepted outcome already durable | Yes | Redelivery becomes no-op or duplicate-suppressed | Restart shows one accepted outcome and one budget consumption |
| F-008 | Lease expires while slow adapter still runs | Adapter execution | Old attempt `lease_lost`; late result handled by attempt token/version | Retain late bytes only under explicit orphan/late-result policy; never silently accept | New attempt may run; late commit rejected if lease ownership invalid | Simulated delayed adapter; no two accepted outcomes |
| F-009 | Adapter timeout before bytes | Adapter call | `timed_out`, retryable according to cap | No | Schedule bounded retry | Outcome and event recorded; new attempt created |
| F-010 | Adapter returns empty response | Adapter boundary | `failed_retryable` or terminal with structured code | No, unless empty payload itself is intentionally retained as diagnostic object | Retry according to policy | Failure code and retry count visible in evidence bundle |
| F-011 | Adapter returns malformed metadata | Validation before commit | `corrupted` or schema failure | Yes, if bytes exist | Quarantine or retry based on policy | Raw bytes retained; no accepted metric |
| F-012 | Signal array length mismatch | Observation validation | `corrupted` | Yes | Quarantine; optional repeat | Observation hash and validation error in evidence bundle |
| F-013 | Signal clipping | Scientific-quality validation | `corrupted` or accepted-with-warning only if protocol allows | Yes | Quarantine or explicit warning path | Clipped bytes retained; metric rejected by default |
| F-014 | Sparse spikes | Scientific-quality validation | `corrupted` or warning according to declared threshold | Yes | Retry or quarantine | Deterministic injected spikes; classification threshold tested |
| F-015 | Missing or invalid unit | Schema validation | `corrupted` | Yes | Quarantine; no guessed unit | Validation fails explicitly; no accepted derived metric |
| F-016 | Axis reversed or non-monotonic | Schema/scientific validation | `corrupted` | Yes | Quarantine or deterministic correction only through new version | Original retained; any corrected observation supersedes it |
| F-017 | HER location unavailable or excluded | Replay adapter lookup | `failed_terminal` with source-unavailable code | No | Reject or quarantine candidate; do not fabricate | Adapter returns no measurement and preserves source reason |
| F-018 | HER source checksum mismatch | Fetch or read | Terminal source-integrity failure | Source file retained separately but not accepted into dataset | Stop ingestion/campaign | Fetch/verification test prevents use of corrupted archive |
| F-019 | Source schema changes | Ingestion parser | Explicit unsupported-schema failure | Raw source retained | Stop ingestion until adapter version added | Unknown column/version does not silently coerce |
| F-020 | Source-provided fit missing but raw LSV exists | Derived-data lookup | Observation succeeds; source-fit metric absent | Yes | Continue with raw observation; optional LabBridge analysis under separate version | Evidence distinguishes missing source metric from failed measurement |
| F-021 | Derived analysis raises numerical error | Analysis step | Attempt may succeed at acquisition; metric outcome rejected or analysis job failed | Yes | Retry analysis only if retryable; do not reacquire automatically | Raw observation remains accepted and visible |
| F-022 | Metric fit lacks required replicates | Scientific-quality validation | Observation accepted; metric `rejected` | Yes | Continue campaign according to policy; optional explicit repeat | Poor evidence quality is distinct from transport failure |
| F-023 | Valid but poor catalyst/sensor performance | Scientific result | `succeeded` | Yes | Accept result; policy may select another candidate | No failure event; poor metric remains valid evidence |
| F-024 | PostgreSQL unavailable before work starts | Job claim/API | Service unavailable; no partial state | N/A | Backoff and readiness failure | Health versus readiness behaviour tested |
| F-025 | PostgreSQL fails during outcome transaction | Commit | Transaction rolls back | Pending object may exist; no accepted outcome | Reconcile and retry | No partial budget/event/projection update |
| F-026 | Object store unavailable before upload | Storage | `failed_retryable` storage failure | No committed observation | Retry with bounded policy | Database never claims committed artifact |
| F-027 | Object deleted after evidence release | Verification | Bundle verification failure | Metadata remains | Alert; bundle considered invalid | `evidence verify` fails deterministically |
| F-028 | Artifact bytes modified | Verification | Bundle verification failure | Modified bytes remain for diagnosis | Alert; do not silently refresh checksum | Tamper test changes one byte and fails SHA-256 validation |
| F-029 | Event append expected-version conflict | Concurrent command | One transaction succeeds; other returns concurrency conflict or retries from new state | N/A | Re-read and decide | Aggregate sequence remains unique and gap policy documented |
| F-030 | Budget race between workers | Reservation transaction | At most eligible jobs reserved | N/A | Reject reservation that exceeds remaining budget | Concurrent integration test never overspends |
| F-031 | Actual cost exceeds estimate | Completion | Outcome recorded according to declared budget policy | As applicable | Consume reserved amount plus allowed adjustment, or flag overrun without scheduling new work | Reserved/incurred accounting remains explainable |
| F-032 | Retry cap reached | Attempt completion | Latest outcome retryable but work item becomes quarantined | Any received observation retained | Stop automatic retry | Evidence lists all attempts and quarantine reason |
| F-033 | Campaign cancelled with available jobs | Campaign command | Jobs cancelled; no new leases | Existing observations retained | Stop scheduling | Cancellation idempotent and terminal semantics tested |
| F-034 | Campaign cancelled with leased job | Campaign command during run | Policy-defined: allow completion but reject new work, or request cooperative cancellation | Any received observation retained and clearly classified | No new jobs; late result policy explicit | Test chosen policy across process boundary |
| F-035 | Approval required but absent | Before risky transition | Work item waits in explicit state | N/A | No execution | Attempt is not created before approval |
| F-036 | Duplicate approval command | Approval API | One approval event; subsequent call idempotent | N/A | Continue once | Idempotency key and unique approval semantics tested |
| F-037 | Observation later found invalid | Post-release or review | New invalidation relation and event | Original retained | Current views exclude it; old bundle unchanged | Historical replay still sees original release state |
| F-038 | Corrected parser produces new normalised observation | Reprocessing | New observation/metric version | Both original and corrected retained | New record supersedes old | Lineage exposes relation and code versions |
| F-039 | Unsupported event schema version during replay | Replay | Explicit replay failure | Existing artifacts unchanged | Stop; require deterministic upcaster or supported code | No silent event dropping |
| F-040 | Projection corrupted or deleted | Operational recovery | Event store remains authoritative | Observations unchanged | Rebuild projection | Rebuilt state equals pre-corruption state |
| F-041 | Backup restored to new environment | Disaster recovery | Restored campaigns and metadata valid | Object references verified | Resume or inspect safely | Restore procedure and checksum audit pass |
| F-042 | Database migration interrupted | Deployment | Transactional migration rolls back or documented recovery path used | Artifacts unchanged | Restore/repair before traffic | Migration test on production-like snapshot |
| F-043 | Simulator uses same seed and config twice | Simulation | Identical canonical observation content | Yes; content identity identical, attempts distinct if explicitly repeated | Deduplicate storage bytes; preserve attempts | Reproducibility and content-addressing tests |
| F-044 | Simulator model version changes | Simulation | New provenance and generally new observation identity | Yes | Preserve both versions | Evidence identifies model version; no accidental cache reuse |
| F-045 | Synthetic output exported without label | Report generation | Integrity-gate failure | Existing data unchanged | Refuse release | Automated report test requires visible and machine-readable synthetic labels |
| F-046 | Observed XPS and GP-predicted XPS conflated | Ingestion or report | Data-contract validation failure | Source rows retained | Refuse affected dataset release | Distinct source-type field required and tested |

---

## 3. Retry policy

Retryability is explicit by failure code. V1 SHOULD follow these defaults:

### Normally retryable

- transient transport failure;
- timeout;
- object-store unavailability;
- database conflict after safe rollback;
- worker lease loss;
- explicitly transient adapter error.

### Normally terminal or quarantined

- unsupported source schema;
- checksum mismatch;
- invalid or missing units;
- impossible candidate;
- unavailable HER location;
- repeated corruption after cap;
- scientific-quality failure requiring a changed protocol rather than repetition.

A broad exception class MUST NOT determine retryability by itself. The failure classifier records a stable code and rationale.

Backoff is bounded and test-configurable. Retry schedules MUST not make tests depend on real long waits.

---

## 4. Late-result policy

A result returned after lease ownership is lost is dangerous because another worker may already be executing the same work.

V1 MUST choose and document one policy:

1. reject the late result from acceptance while optionally retaining it as a diagnostic orphan; or
2. accept it only through a compare-and-set transaction proving that no later attempt has been accepted and the work item still permits it.

The default SHOULD be policy 1 because it is easier to reason about. Any retained late bytes must be labelled and excluded from accepted scientific datasets unless explicitly reconciled.

---

## 5. Object-store reconciliation

The system MUST distinguish:

- `pending` object metadata;
- `committed` object metadata;
- unreferenced or orphaned objects;
- released immutable objects.

A reconciliation command SHOULD:

- verify pending objects by checksum;
- commit objects referenced by a completed transaction when safe;
- identify unreferenced objects older than a configured threshold;
- quarantine rather than immediately delete ambiguous objects;
- never mutate a released evidence object.

---

## 6. Campaign fault experiment

The release experiment MUST inject process termination at random durable boundaries, including:

- after job lease;
- after adapter response;
- during or after object upload;
- immediately before database outcome transaction;
- immediately after commit;
- during evidence export.

For at least 100 seeded campaigns, record:

- campaign seed and fault point;
- process exit and restart timestamps;
- attempts created;
- lease recoveries;
- observations staged and committed;
- accepted outcomes;
- duplicate suppressions;
- replay comparison;
- evidence verification result.

Acceptance targets:

- zero lost accepted observations;
- zero unintended duplicate accepted observations;
- zero hard-budget overspends;
- exact agreement between replayed and persisted terminal state;
- all released bundles verify.

These values are targets until measured. The public report MUST publish the actual results and any exclusions.

---

## 7. Evidence requirements per scenario

A passed scenario SHOULD leave enough information to answer:

1. What was requested?
2. Which process or dependency failed?
3. What bytes, if any, were received?
4. Which outcome and failure code were recorded?
5. Was the failure retryable?
6. Which new attempt, state transition, or quarantine action followed?
7. Was budget reserved, consumed, or released?
8. Can the campaign be replayed to the same state?
9. Does the evidence bundle expose the incident?
10. Which automated test proves the behaviour?

A green unit test without inspectable state and artifact evidence is insufficient for the major process-boundary scenarios.
