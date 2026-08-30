# The `packet_lost` trigger was always wrong

**Component:** `noq-proto/src/connection/qlog.rs`, function `emit_packet_lost`
**Governing specs:** RFC 9002 · draft-ietf-quic-qlog-quic-events-12
**Status:** Fixed and verified (2026-08-30)

A one-line operand swap in n0q's qlog emitter meant every packet ever
declared lost was labeled `reordering_threshold`, regardless of why it was
actually declared lost — and a note in the project's working log claiming
this was already fixed turned out not to match the code that was actually
running.

---

## 1. The problem

### 1.1 Symptom

Every stream-sweep run collected in this investigation — **72 runs**,
spanning three schedulers, three congestion controllers, and eight
`packet_threshold` values — reported packet loss triggers that were
**100% `reordering_threshold`** and **0% `time_threshold`**, with no
exceptions.

A separate note in the project's working log (`TODO.md`) claimed this exact
issue had already been found and fixed, citing a "confirmed" non-degenerate
split (e.g. 96.5% / 3.5% on one configuration). The two claims cannot both
be true of the same code, and the code on disk is what actually ran.

### 1.2 Root cause

QUIC's loss detector (RFC 9002 §6.1) can declare a packet lost for either of
two reasons: too many higher packet numbers have already been acknowledged
(**packet threshold**), or too much time has elapsed since it was sent
(**time threshold**). n0q's qlog emitter is supposed to report which one
actually fired. The comparison it used to decide was inverted:

```rust
// qlog.rs — before
info.time_sent.saturating_duration_since(now) >= loss_delay
```

`saturating_duration_since` computes `self − earlier`, flooring at zero if
the result would be negative. Since a packet's `time_sent` can never be
later than `now` — the packet is already sent — `info.time_sent
.saturating_duration_since(now)` is **always `Duration::ZERO`**.
`Duration::ZERO >= loss_delay` is false for every real (non-zero)
`loss_delay`, so the branch that reports `TimeThreshold` was dead code: the
`match` always fell to `ReorderingThreshold`, independent of the actual
reason.

> The operands were simply backwards. The correct question is "how long ago
> was this packet sent" — `now − time_sent` — not "how far in the future is
> its send time," which for an already-sent packet is nonsensical and
> always zero.

## 2. Where it lived in the source

n0q already implements the correct comparison — just not in the function
that logs it. The authoritative loss-detection algorithm, in the same
crate, gets it right:

```rust
// mod.rs:3377 — the real detector (always correct)
let packet_too_old = now.saturating_duration_since(info.time_sent) >= loss_delay;
```

This distinction matters: `detect_lost_packets` in `mod.rs` is what
actually decides which packets get retransmitted and which
congestion-control events fire — and it always used the correct
`now − time_sent` form. A prior independent review of this codebase
(packet-number spaces, sent-packet tracking, largest-acked tracking, RTT,
and the congestion controller) confirmed all of that machinery is correctly
isolated per network path with no cross-path bugs. The bug lived *only* in
`emit_packet_lost` in `qlog.rs` — the function whose sole job is to
describe, after the fact, which of the two conditions the real detector had
already correctly used.

## 3. Does this violate the IETF drafts?

Two different specifications are relevant here, and the bug only touches
one of them.

### 3.1 RFC 9002 — not violated

RFC 9002 (QUIC Loss Detection and Congestion Control) governs what an
endpoint actually *does*: which packets it retransmits, how its congestion
window reacts. That behavior comes entirely from `mod.rs`'s
`detect_lost_packets`, which never had this bug. No packet was
retransmitted incorrectly, no congestion event fired incorrectly, and no
other implementation talking to n0q would ever observe a difference on the
wire. **Wire-protocol conformance was intact throughout.**

### 3.2 draft-ietf-quic-qlog-quic-events — wrongly implemented, not "violated"

n0q's qlog output targets [draft-ietf-quic-qlog-quic-events-12][events],
layered on [draft-ietf-quic-qlog-main-schema-13][schema] (per the crate's
own doc comment). That draft defines `packet_lost`'s `trigger` field as an
enum:

[events]: https://www.ietf.org/archive/id/draft-ietf-quic-qlog-quic-events-12.html
[schema]: https://www.ietf.org/archive/id/draft-ietf-quic-qlog-main-schema-13.html

> **PacketLostTrigger** — `reordering_threshold` · `time_threshold` · `pto_expired`

This is a *diagnostics* schema, not a wire protocol. Nothing in it is
phrased as a conformance-testable MUST on emitted-value accuracy — a qlog
consumer has no way to know a value is "illegal," because any of the four
enum members is syntactically valid output. So this bug is not a spec
*violation* in the RFC 2119 sense. It is, precisely, **wrongly
implemented**: the code emitted a schema-legal value that did not reflect
the schema's own defined semantics — every event claimed to be
reordering-triggered whether or not it was. The practical damage lands
entirely on whoever reads the qlog trace afterward for diagnosis, not on
interoperability.

> **Concretely, in this investigation:** every loss-detector analysis
> performed on n0q's qlogs this session used the `trigger` field only for
> descriptive commentary — the actual genuine-vs-spurious loss
> classification cross-referenced `packet_lost` packet numbers against
> later `packets_acked` events, a mechanism that does not read `trigger` at
> all. **None of the goodput, spurious-loss, or `packet_threshold` sweep
> conclusions in this investigation are affected by this bug.** Only the
> trigger label itself was wrong.

## 4. The fix

One line, swapping the operands to match the already-correct form used in
`mod.rs`:

```diff
- match info.time_sent.saturating_duration_since(now) >= loss_delay {
+ match now.saturating_duration_since(info.time_sent) >= loss_delay {
      true => PacketLostTrigger::TimeThreshold,
      false => PacketLostTrigger::ReorderingThreshold,
  },
```

Verified after the change:

- [x] `cargo check -p noq-proto --features qlog` — clean
- [x] `cargo check -p noq-proto` (qlog feature off) — clean
- [x] `cargo test -p noq-proto --features qlog --lib` — 421 passed, 0 failed
- [x] Release binaries (`n0q-server`, `n0q-client`) rebuilt
- [ ] Not yet re-run against a live sweep to confirm a non-degenerate
      trigger split in captured qlogs

## 5. Compliance after the fix

| Layer | Before | After |
|---|---|---|
| RFC 9002 (wire behavior) | Compliant | Compliant *(unchanged)* |
| qlog-quic-events trigger field | Wrongly implemented | Correct |

With the operand order corrected, `emit_packet_lost` now mirrors
`detect_lost_packets`'s own decision exactly — the qlog trace reports the
same reason the algorithm actually used. There is no remaining known
deviation from either specification in this code path.

## 6. Related work from the same investigation

Two smaller, adjacent gaps were closed earlier in this investigation while
building the tooling to analyze n0q's loss behavior. Neither is a spec
violation — both are completeness gaps in n0q's own diagnostics, addressed
because the analysis needed them.

### 6.1 `packet_lost` events carried no `path_id`

`PacketHeader.path_id` is an optional field in the qlog schema, present but
unset on every `packet_lost` event n0q emitted, even though the caller had
the path ID in hand. This made per-path loss impossible to attribute
directly. Fixed by threading `path_id` through to the header and the
event's `tuple_id`, matching how `packet_sent`/`packet_received` already
behaved.

### 6.2 `packets_acked` was never emitted

The schema defines a `packets_acked` event; n0q's qlog module never called
it. Without it, there was no way to tell — from qlog alone — whether a
packet declared lost had actually arrived late (spurious) versus never
arriving at all (genuine). Added `emit_packets_acked`, called per
newly-acknowledged packet-number range.

> **A false start worth recording:** the first wiring of this emission read
> from `sent_packets`, which `handle_lost_packets` empties via `take()` the
> moment a packet is declared lost — guaranteeing a spurious-loss rate of
> exactly 0.0% on every single run, regardless of what the network did. The
> fix moved the emission into `detect_spurious_loss`'s own independent
> `lost_packets` tracking, the same state the connection already uses
> internally to decide whether a congestion event was spurious.

---

*Investigation conducted against `n0q` (noq-proto crate) and the
`mpquic-noq` experiment harness. Full working notes: `TODO.md`, mpquic-noq
repository.*
