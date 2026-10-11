Closes #18.

## What
- A maker side change is now a cancel plus a new post-only order. Hyperliquid refuses a side-changing modify (`Attempted to modify to invalid new order`, measured on testnet 2026-10-11; the old order stays resting). A same-side price change is still one modify.
- A side change counts as two order actions and is sent only if both fit under the 20/min cap (18 for quotes).
- A refused modify keeps the old quote tracked and is not an order error; the next re-quote of that order is a cancel and a place, never the same modify again.
- Quote error, rejection, refusal, gone and rate-limit events record what was sent (action, oid, side, size, price).
- The paper and fake venues refuse a side-changing modify too, so paper runs take the same path.

## Why
The 1-hour live maker run on 2026-10-11 stopped after 14 minutes with `kill=order_errors`: three sell-to-buy modifies were refused in a row. It ended safe (flat, no open orders, reconcile OK).

## Decisions
- Fix before rerunning the live hour. Rationale: none given.
- Fix review findings R1 and R2; leave R3 as a documented limit. Rationale: none given.

## Caught
- (live run) side change by modify is refused by Hyperliquid → f862024
- (reviewer) R1 refused modify resent forever → d0a1eb3
- (reviewer) R2 successful cancel did not reset the error count → 8149333
- (me) nothing at this milestone

## Evidence
- 884 tests pass, ruff clean. Break-it: 18 builder mutants + 9 reviewer mutants + 6 for the fixes, all caught.
- The 17:44 sequence replayed: old code → three refused modifies and `kill=order_errors`; new code → a buy resting, no order errors.
- 60 s paper maker smoke: 8 side changes, each a cancel then a place, no errors.

https://claude.ai/code/session_01Ua15mU8f7EqK7W91vAtfvo
