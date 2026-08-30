# n0q qlog loss instrumentation: three problems, and how they were fixed

**Component:** `noq-proto/src/connection/qlog.rs` and `noq-proto/src/connection/mod.rs`
**Context:** found while building tooling to classify n0q's packet loss as
genuine (network drop) vs. spurious (reordering mistaken for loss) —
see `TODO.md` and `docs/loss-trigger-postmortem.md` for the full
investigation this came out of.

n0q's actual loss-detection *algorithm* (RFC 9002, in
`detect_lost_packets`/`mod.rs`) was correct throughout this investigation —
independently verified: packet-number spaces, sent-packet tracking,
largest-acked tracking, RTT, and the congestion controller are all properly
isolated per network path, with no cross-path bugs. Every problem below is
in the **qlog diagnostics layer** around that algorithm, not the algorithm
itself. None of them changed what n0q actually did on the wire; all three
affected what n0q's qlog output *said* it did, which matters if you're
trying to debug or analyze n0q from its logs — which is exactly what this
investigation was doing.

---

## Problem 1 — `packet_lost` events always said "reordering", never "timeout"

### What

QUIC's loss detector (RFC 9002 §6.1) declares a packet lost for one of two
reasons: too many higher packet numbers have already been acknowledged
(**packet threshold**), or too much time has passed since it was sent
(**time threshold**). n0q's qlog output is supposed to report which one
fired, via the `packet_lost` event's `trigger` field. It didn't — every
single loss, for either reason, was logged as `reordering_threshold`.

This was caught empirically: across 72 sweep runs in this investigation
(varying scheduler, congestion control, and `packet_threshold` values), the
trigger split was **100% `reordering_threshold` / 0% `time_threshold`**,
with zero exceptions — exactly what you'd see if the code computing the
trigger were structurally incapable of ever selecting the other branch.

### Where

`noq-proto/src/connection/qlog.rs`, `emit_packet_lost`:

```rust
// before
match info.time_sent.saturating_duration_since(now) >= loss_delay {
    true => PacketLostTrigger::TimeThreshold,
    false => PacketLostTrigger::ReorderingThreshold,
}
```

`saturating_duration_since` computes `self − earlier`, flooring at zero if
that would go negative. A packet's `time_sent` can never be later than
`now` — it's already been sent — so `info.time_sent
.saturating_duration_since(now)` is **always `Duration::ZERO`**.
`Duration::ZERO >= loss_delay` is false for any real `loss_delay`, so the
`true` arm was dead code. The operands were simply backwards: the question
that needs answering is "how long ago was this sent" (`now − time_sent`),
not "how far in the future is its send time" (nonsensical for an
already-sent packet).

The correct form already existed a few hundred lines away, in the real
detector:

```rust
// mod.rs:3377 — always correct
let packet_too_old = now.saturating_duration_since(info.time_sent) >= loss_delay;
```

`emit_packet_lost` just wasn't mirroring it.

### How it was fixed

Swapped the operands to match `mod.rs`:

```rust
// after
match now.saturating_duration_since(info.time_sent) >= loss_delay {
    true => PacketLostTrigger::TimeThreshold,
    false => PacketLostTrigger::ReorderingThreshold,
}
```

Verified with `cargo check -p noq-proto` (with and without `--features
qlog`) and `cargo test -p noq-proto --features qlog --lib` (421 passed).

**Spec note:** this never violated RFC 9002 — the real detector was always
correct, so no packet was ever mis-retransmitted and no interop would ever
be affected. It's a violation of the *intent* of
[draft-ietf-quic-qlog-quic-events-12][events]'s `trigger` field (which
exists to describe why a loss was declared), but not an enforceable MUST —
any of the enum's values (`reordering_threshold` / `time_threshold` /
`pto_expired`) is schema-legal output. Best described as **wrongly
implemented**, not **non-compliant**. Full compliance discussion in
`docs/loss-trigger-postmortem.md`.

[events]: https://www.ietf.org/archive/id/draft-ietf-quic-qlog-quic-events-12.html

---

## Problem 2 — `packet_lost` events didn't say which path the loss was on

### What

n0q is a multipath QUIC implementation — every connection can have multiple
active network paths, each with its own packet-number space and its own
loss behavior. Every other per-path qlog event (`packet_sent`,
`packet_received`) is tagged with which path it belongs to. `packet_lost`
wasn't. That made it impossible to tell, from qlog alone, which of a
connection's paths any given loss happened on — you had to infer it
indirectly by cross-referencing packet-number ranges against per-path
`packet_sent` counts, which is fragile and doesn't scale past a couple of
paths.

### Where

`noq-proto/src/connection/qlog.rs`, `emit_packet_lost`. The qlog schema's
`PacketHeader` struct has an optional `path_id` field — it was simply never
populated:

```rust
// before
let event = PacketLost {
    header: Some(PacketHeader {
        packet_number: Some(pn),
        packet_type: packet_type(space, false),
        length: Some(info.size),
        ..Default::default()   // path_id left as None
    }),
    ...
};
stream.emit_event(EventData::QuicPacketLost(event), now);  // no tuple_id either
```

The caller, `handle_lost_packets` in `mod.rs`, already had `path_id` in
scope — it just wasn't being passed through.

### How it was fixed

Added a `path_id: PathId` parameter to `emit_packet_lost`, set it on the
header, and attached the event's `tuple_id` the same way `packet_sent` and
`packet_received` already do:

```rust
// after
pub(super) fn emit_packet_lost(
    &self,
    pn: u64,
    path_id: PathId,   // new
    info: &SentPacket,
    loss_delay: Duration,
    space: SpaceKind,
    now: Instant,
) {
    ...
    let event = PacketLost {
        header: Some(PacketHeader {
            packet_number: Some(pn),
            packet_type: packet_type(space, false),
            length: Some(info.size),
            path_id: Some(path_id.as_u32() as u64),   // new
            ..Default::default()
        }),
        ...
    };
    let tuple_id = fmt_tuple_id(path_id.as_u32() as u64);
    stream.emit_event_with_tuple_id(EventData::QuicPacketLost(event), now, Some(tuple_id)); // new
}
```

Updated the one call site in `handle_lost_packets` to pass `path_id`
through. Loss events now carry both the header field and the `tuple`
grouping, matching `packet_sent`/`packet_received` exactly.

---

## Problem 3 — `packets_acked` was never emitted at all

### What

The qlog schema defines a `packets_acked` event — the peer telling you
which packet numbers it received. The underlying crate (`n0-qlog`) already
had the type for it; n0q's connection code just never called it. Without
it, there was no way to answer, from qlog alone, the question this whole
investigation was built around: **was a declared-lost packet actually
dropped by the network, or did it just arrive late (reordering) and get
acknowledged afterward?** RFC 9002's loss detector is a heuristic guess at
declaration time — it can't know the difference — and the connection
already resolves that guess internally (see `detect_spurious_loss`), but
none of that resolution was visible in the qlog output.

### Where

`noq-proto/src/connection/qlog.rs` had no `emit_packets_acked` function.
`noq-proto/src/connection/mod.rs`'s `inner_on_ack_received` processes every
incoming ACK frame and knows exactly which packet numbers it covers, but
never told the qlog stream about it.

### How it was fixed

Added `emit_packets_acked(path_id, space, packet_numbers, now)` to
`qlog.rs`, following the same per-path `tuple_id` pattern as the other
per-path events. It's called from **two places**, which together cover the
complete set of acknowledged packet numbers:

1. **`inner_on_ack_received`**, right after computing `newly_acked` — the
   normal case, packet numbers that are freshly acknowledged and were
   still tracked in `sent_packets` (i.e. not yet declared lost).

2. **`detect_spurious_loss`** — the case that actually matters for
   genuine-vs-spurious classification: packet numbers that had *already*
   been declared lost (and therefore already removed from `sent_packets`)
   but are now covered by this ACK after all, proving the loss declaration
   was wrong.

```rust
// mod.rs, inside detect_spurious_loss
if !spurious_acked.is_empty() {
    spurious_acked.sort_unstable();
    self.qlog.emit_packets_acked(path, space.kind(), spurious_acked, now);
}
```

These two call sites are disjoint by construction — a packet number is
either still in `sent_packets` (case 1) or was already moved into the
`lost_packets` map when declared lost (case 2), never both — so together
they give a complete, non-overlapping `packets_acked` stream.

With this in place, genuine-vs-spurious classification becomes a direct
qlog query: for each `packet_lost` (now tagged with `path_id`, per Problem
2), check whether that packet number ever shows up in a later
`packets_acked` on the same path. If it does, the loss was spurious; if it
never does, it was genuine.

### A gotcha worth knowing if you touch this code again

The first attempt at wiring this hooked `emit_packets_acked` **only** into
`inner_on_ack_received`'s `newly_acked` (case 1 above), reasoning that it
was "the" place ACKs get processed. That produced a clean-looking but
**entirely bogus 0.0% spurious rate on every single run** — because
`newly_acked` is built by iterating `sent_packets`, and a packet already
declared lost has already been removed from `sent_packets` via `take()` in
`handle_lost_packets`. A later ACK covering a lost packet can therefore
*never* appear in `newly_acked` — 0% spurious was guaranteed by
construction, completely independent of what the network actually did.

This is exactly why `detect_spurious_loss` keeps its own separate
`lost_packets` map rather than reusing `sent_packets` — and exactly why
case 2 above has to live inside `detect_spurious_loss` itself, not
alongside case 1. If you see a metric that reads suspiciously uniform
(especially 0.000%) across every configuration, suspect the instrumentation
before the network.

---

## Summary

| # | Problem | File | Category |
|---|---|---|---|
| 1 | `packet_lost` trigger always `reordering_threshold` | `qlog.rs::emit_packet_lost` | Wrongly implemented (vs. qlog-quic-events draft) |
| 2 | `packet_lost` had no `path_id` | `qlog.rs::emit_packet_lost` | Completeness gap |
| 3 | `packets_acked` never emitted | `qlog.rs` (new fn) + `mod.rs` (2 call sites) | Completeness gap |

All three are diagnostics-layer issues in n0q's qlog output, not defects in
the RFC 9002 loss-detection algorithm itself, which was correct throughout.
Fixed and verified together: `cargo check -p noq-proto` clean with and
without `--features qlog`; `cargo test -p noq-proto --features qlog --lib`
— 421 passed, 0 failed; release binaries (`n0q-server`, `n0q-client`)
rebuilt.
