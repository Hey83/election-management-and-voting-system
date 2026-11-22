# P(14) — Allocation Verification and Acceptance Scenarios

**Status:** Proposed test cases. **Not executed** as part of this contribution. Do not mark them passed without reproducible evidence. Run against the team's approved test branch with fictional data and separate role accounts. Reset the demo between destructive scenarios.

## A. Unit/model tests

| Test ID | Input / stimulus | Expected behavior | Evidence status |
| --- | --- | --- | --- |
| P14-U01 | Series DEMO; first `00401`, last `00500` | Accepted; calculated quantity = 100 | Not run |
| P14-U02 | First `00500`, last `00401` | Invalid reversed range rejected | Not run |
| P14-U03 | Non-ASCII or alphabetic serial digits | Serial rejected | Not run |
| P14-U04 | More than 18 serial digits | Serial rejected | Not run |
| P14-U05 | Empty or whitespace-only series | Validation error | Not run |
| P14-U06 | Time without timezone | Invalid as API allocation input | Not run |
| P14-U07 | Allocation correction missing reason | Validation error | Not run |
| P14-U08 | Same allocation payload, different request UUID | Equivalent digest for same station/payload | Not run |

The accompanying `tests/test_p14_allocation_validation.py` implements focused model-level checks only; these do not require the live Qt client or MySQL, although the project's Python dependencies must be installed.

## B. API and end-to-end checks

| Test ID | Setup and action | Expected behavior | Evidence status |
| --- | --- | --- | --- |
| P14-I01 | Polling Officer allocates available ordinary range to own station counter | Record saved; exact range, quantity and revision displayed | Not run |
| P14-I02 | Allocate serial interval outside received stock | Request blocked; stock unchanged | Not run |
| P14-I03 | Allocate range overlapping another live allocation | Conflict; no duplicate ownership | Not run |
| P14-I04 | Select a counter at another station | Access/counter validation blocks the operation | Not run |
| P14-I05 | Re-submit same request ID and identical payload | No duplicate allocation created | Not run |
| P14-I06 | Re-submit same request ID with changed fields | Conflict rather than silent overwrite | Not run |
| P14-I07 | Correct record with current revision and a reason | New revision saved; previous revision retrievable | Not run |
| P14-I08 | Correct record using stale revision | `revision_conflict`; previous data preserved | Not run |
| P14-I09 | Non-Polling-Officer attempts to create allocation | Authorization prevents state change | Not run |
| P14-I10 | Connection drops during save, then reconnect | Operator checks server result before retry; no duplicate allocation | Not run |
| P14-I11 | Both counters receive 200; both retain 50 | Issued ordinary stock derives to 300 in P(12); compare P(15) and P(18) separately | Demo arithmetic documented; full run not verified |

## C. Manual execution record template

For each test, record **Git commit SHA**, OS, Python version, API/DB environment, role/station, **expected result**, **actual result**, Pass/Fail/Blocked, screenshot/log path, tester, and date. Store only fictional election data and redact credentials/tokens.

### Example: P14-I03 overlapping allocation

- **Precondition:** Received ordinary stock includes `00401–00600`; first allocation to Counter 1 uses `00401–00500`.
- **Action:** Try to allocate `00450–00550` to Counter 2.
- **Expected:** Server rejects the overlap; Counter 1 retains its original allocation; the availability count does not double-count stock.
- **Actual:** _To be recorded by tester._
- **Status:** **Not run**.
- **Evidence:** _Attach screenshot of error, database/API result, and final allocation list._

### Example: P14-I08 stale correction

- **Precondition:** Allocation revision 1 is open in two accounts/sessions.
- **Action:** Session A corrects it to revision 2; Session B tries correcting using revision 1.
- **Expected:** Session B receives a revision conflict; no change to revision 2.
- **Actual:** _To be recorded by tester._
- **Status:** **Not run**.

## D. Relation to physical operations

The external P(14) procedure includes fixed bundle sizes, automatically populated ending serials and time-specific upload practices. Those should be treated as **candidate requirements for discussion**, not presumed current features. Do not report these tests as covering those external requirements unless the group has explicitly adopted and implemented them.
