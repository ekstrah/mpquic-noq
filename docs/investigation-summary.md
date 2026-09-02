# n0q multipath QUIC investigation — everything done, fixed, and where

Consolidated summary across this whole investigation (multiple sessions),
written to be read top to bottom. For the full chronological log see
`TODO.md`; for the terse "pick this up cold" version see
`SESSION-HANDOFF.md`. Every claim below was re-checked against the current
source on **2026-09-01** before being written down — this investigation was
burned twice before by docs claiming a fix that had quietly disappeared
from the code (see the last section), so nothing here is taken on faith
from an older note.

**What n0q is**: a multipath QUIC implementation (`noq-proto` crate),
tested against an emulated 3-link topology (`mpquic-noq/mininet_topo.py`)
with `mpquic-noq/scripts/*.py` for analysis. Focus has shifted from
stream-mode bulk transfer to **datagram mode (RFC 9221)**, which is
becoming the core transport for the target use case.

---

## 1. The multipath scheduler doesn't exist

**Where**: `apps/src/client.rs` (`--scheduler` CLI flag) and
`noq-proto/src/connection/mod.rs:1075` (`Connection::poll_transmit`).

`--scheduler MINRTT|REDUNDANT|ROUNDROBIN` is parsed and logged once, but
never wired into `TransportConfig` or any path-selection logic. Actual path
selection is a fixed rule: always prefer the lowest `PathId`, only fall
through to another path if it lacks capacity. Not RTT-aware, no packet
duplication, no rotation.

**How this was confirmed**, two independent ways:
- Exhaustive grep across all 7 workspace crates for any Scheduler /
  MinRtt / Redundant / RoundRobin type — nothing exists.
- Statistically: holding CC and `packet_threshold` fixed, goodput spread
  across the three scheduler *labels* was ~10% (stream-mode, 72-run
  dataset) versus ~145% spread across CC (a known, real effect). Every
  scheduler-attributed difference reported earlier in this investigation
  was run-to-run noise, not a real algorithm difference.

**Status**: not fixed — deliberately deferred (see "Open decisions"
below). `mininet_topo.py`'s sweep config now sweeps `MINRTT` only, since
sweeping all three labels was tripling run count for zero information.

---

## 2. Three qlog instrumentation bugs — fixed

**Where**: `noq-proto/src/connection/qlog.rs`, `noq-proto/src/connection/mod.rs`.

Full writeups: `docs/qlog-loss-instrumentation-fixes.md` (developer
reference) and `docs/loss-trigger-postmortem.md` (IETF-compliance framing).

### 2a. `packet_lost`'s `trigger` field was always wrong

`emit_packet_lost`'s comparison had its operands reversed —
`info.time_sent.saturating_duration_since(now)` instead of
`now.saturating_duration_since(info.time_sent)` — which always evaluates
to zero (since `time_sent <= now`), so every loss was reported as
`reordering_threshold`, never `time_threshold`, regardless of the real
cause.

**Fixed** at `noq-proto/src/connection/qlog.rs:218`, now correctly reads
`now.saturating_duration_since(info.time_sent) >= loss_delay`, mirroring
the authoritative check in `mod.rs`. **Re-verified present on 2026-09-01.**

This bug never affected wire behavior — the actual loss-detection code
path (`detect_lost_packets` in `mod.rs`) always used the correct form. It
also never affected any throughput/loss conclusion in this investigation,
since genuine-vs-spurious classification cross-references packet numbers
against `packets_acked` events, never the `trigger` field. Only the label
itself was ever wrong.

**Gotcha**: this exact bug was marked "[FIXED] ... confirmed" in an
earlier `TODO.md` entry, but when a later session went to cite it
precisely, the buggy code was still on disk. Whether it was reverted or
the earlier fix never actually landed is unresolved. **Lesson: never cite
a "fixed" claim from a doc without grepping the current source first** —
this is why every claim in this document was re-checked on 2026-09-01.

### 2b. `packet_lost` events carried no `path_id`

Made per-path loss attribution from qlog impossible. **Fixed**: `emit_packet_lost` (`noq-proto/src/connection/qlog.rs:193`) now takes and emits `path_id`.

### 2c. `packets_acked` (a standard qlog event) was never emitted

Without it, there was no way to distinguish genuine loss from spurious
(reordering-triggered) loss from qlog alone. **Fixed**: added
`emit_packets_acked` (`noq-proto/src/connection/qlog.rs:239`), called from
two sites in `mod.rs` (line 2997, for normal newly-acked packets; line
3159, for retroactively-proven spurious losses inside
`detect_spurious_loss`).

With 2b + 2c together: for any `packet_lost` event, scan later
`packets_acked` events on the same path for that packet number. Found
later → spurious (reordering). Never found → genuine loss.

**Gotcha if this is ever touched again**: the first attempt at 2c hooked
into `newly_acked` (built from `sent_packets`), which gets emptied by
`take()` the instant a packet is declared lost — guaranteeing a fake 0.0%
spurious rate on every run, independent of the network. **A metric that
reads exactly 0.000% uniformly across every configuration is almost always
an instrumentation bug, not a result.**

---

## 3. `packet_threshold` sweep — most "loss" is reordering, not drops

**Where**: `apps/src/client.rs` / `apps/src/server.rs` (`--packet-threshold`
CLI flag, added), `noq-proto/src/config/transport.rs:173`
(`TransportConfig::packet_threshold`, pre-existing, now exposed via CLI).

Stream-mode sweep, 8 threshold values × 9 (scheduler × CC) combos = 72
runs. Findings:

- At the RFC 9002 default (`packet_threshold = 3`), **~99% of "declared
  loss" is spurious** (reordering caused by netem jitter overtaking a
  straggler packet before the 3-packet threshold), not real drops. This
  wastes ~40% of transmitted bytes on redundant retransmissions.
- **Raising the threshold recovers most of that, cheaply**, but CC
  algorithms respond very differently:
  - **BBR**: peaks around `pt=25-30`, then *regresses* — a real reversal,
    reproduced identically across all three scheduler labels (useful
    cross-validation despite the scheduler being a no-op — see §1). Not
    yet root-caused; hypothesized as BBR's probe-bw pacing-gain cycling
    interacting with a larger allowed-reordering window.
  - **CUBIC**: keeps improving through `pt=40`, no ceiling found in the
    tested range.
  - **NewReno**: flat throughout — it's window-bound (AIMD can't rebuild
    a useful window within 15s at these RTTs), not loss-bound, so the
    threshold doesn't matter for it.
- **Genuine loss stays flat across all thresholds for BBR** (0.08-0.32%,
  no trend) — the expected "it's a network property" sanity check, and it
  passes.

**Caveat**: every cell in this sweep was `n=1` at the time — no error
bars. This is part of why the sweep is now being scaled up with repeats
(see §6).

---

## 4. Checked for picoquic's CID-retirement bug — not present

**Where**: `noq-proto/src/endpoint.rs`, `noq-proto/src/connection/mod.rs`,
`noq-proto/src/connection/cid_state.rs`, `noq-proto/src/connection/cid_queue.rs`.

picoquic (in a separate investigation) was found to validate incoming CIDs
against a single cached pointer that stops updating once the peer rotates
CIDs, causing spurious multipath connection teardown. Traced the
equivalent n0q code paths — packet routing
(`endpoint.rs:1142-1150`, hashmap lookup of the actual arriving DCID),
`RETIRE_CONNECTION_ID` handling (`cid_state.rs:162-189`, keyed removal
from a set), `NEW_CONNECTION_ID` handling (`cid_queue.rs:60-109`, ring
buffer) — every decision resolves by keyed lookup against a live
map/set, never a single cached pointer. **Not present in n0q.**

---

## 5. Datagram-mode benchmark — two separate bugs, both fixed

**Where**: `apps/src/client.rs::run_datagram_test`.

### 5a. `send_datagram()` silently evicted unsent data under backpressure

The original benchmark used `send_datagram()`, which drops old unsent
datagrams from the local send buffer to make room for new ones when full
(`noq-proto/src/connection/datagrams.rs::make_space_for`, only
`trace!`-logged, no counter). This meant the "Sent N dgrams" the benchmark
printed was dominated by host-scheduling artifacts, not real network
capacity — confirmed by an outlier run reporting 40,140 sent when typical
runs were in the hundreds.

**Fixed** by switching to `send_datagram_wait()`, which blocks for real
buffer space instead of evicting.

### 5b. That fix broke the benchmark completely — root-caused 2026-09-01

The very next validation sweep after 5a's fix reported **`Sent 0 dgrams,
Received 0 dgrams, 0.00 Mbps` on every single one of 24 runs.**

**False lead ruled out first**: suspected `SendDatagram::poll`
(`noq/src/connection.rs:1164-1198`)'s retry loop, which on first read
looked like it might discard an "unblocked" notification without ever
retrying the actual send. Built a standalone reproduction (2000 datagrams
forced through a saturated 1MB send buffer on a single path) — it
completed in 30ms. **Not a bug**: Rust's `Future::poll` always restarts at
the top of the function on the next executor call, so the real send does
get retried. This code was left untouched.

**Actual root cause**: the benchmark hard-codes a fixed 1200-byte payload.
Extending the reproduction to multipath (3 subflows, matching the real
client) reproduced the failure immediately, but as `TooLarge`, not a hang:

1. **RFC 9000 §14.1** requires every QUIC path — including a
   freshly-opened multipath subflow — to start at a conservative
   **1200-byte** floor, only growing via DPLPMTUD (**RFC 8899**, RFC 9000
   §14.3) over subsequent RTTs.
2. With multipath, n0q computes the connection-wide usable size as the
   **minimum MTU across all active paths**
   (`Connection::current_mtu()`, `noq-proto/src/connection/mod.rs:7022`) —
   intentional, and correct per **RFC 9221 §4**, which requires a sent
   datagram not exceed the current path's usable size (sizing to the
   smallest active path guarantees it's safe on any of them).
3. The benchmark opens 3 fresh subflows *immediately* before the test
   starts, so at least one is still at the 1200-byte floor at that moment.
4. After QUIC/AEAD overhead (~21-29 bytes) and DATAGRAM framing (9 bytes,
   `Datagram::SIZE_BOUND`, `noq-proto/src/frame.rs:2044`), usable size
   comes out to ~1162-1170 bytes — always under the hard-coded 1200.
5. Every `send_datagram_wait()` call returned `TooLarge`, and the
   benchmark's `Ok(Err(_)) => break` treated *any* error as fatal,
   aborting the whole 15s test after 0 sends — deterministically, on
   every run, independent of CC or `packet_threshold`.

**Fixed**: `apps/src/client.rs` now queries `conn.max_datagram_size()`
every iteration and sizes the payload to `min(1200, max_size)`, and
retries (instead of aborting) on a `TooLarge` result. This is the
spec-correct fix — RFC 9221 always intended the size limit to be dynamic
(n0q already handles it shrinking again via `drop_oversized` on an MTU
black-hole event) — not a workaround.

**Verified**: local loopback test went from 0/0 to **14,992 sent / 14,992
received, 46.42 Mbps**. Live mininet re-run recovered real throughput:

| CC | goodput range (24-run validation, n=2) | mean |
|---|---|---|
| CUBIC | 31.8-45.7 Mbps | ~39 Mbps |
| BBR | 23.5-39.0 Mbps | ~31 Mbps |
| NewReno | 1.4-5.5 Mbps | ~3.4 Mbps |

Same CUBIC > BBR >> NewReno ordering and "most declared loss is spurious"
pattern as stream mode — good cross-validation.

**Gotcha that cost one full wasted sweep cycle**: `mininet_topo.py:270`
runs prebuilt `target/release/n0q-{server,client}` binaries directly
whenever they already exist, never auto-rebuilding. A source fix does not
take effect in the next sweep until you explicitly run
`cargo build --release --bin n0q-server --bin n0q-client` first.

**Methodology caveat also found**: the benchmark's printed "Sent N dgrams"
overstates real transmission — `send_datagram_wait()` completing only
means the payload was accepted into the local outgoing queue, not that it
hit the wire, and `conn.close()` at test end discards whatever's still
queued. Gap scales inversely with throughput: CUBIC 2-11%, BBR 10-40%,
NewReno 18-73% sent-but-never-received. The reported `Mbps` figure is
unaffected (computed from received bytes only); don't cite "Sent N
dgrams" as a transmission-rate metric.

Full writeup with more code excerpts: `docs/datagram-mtu-fix.md`.

---

## 6. Current state / in progress

Sweep config (`mininet_topo.py`) scaled up on 2026-09-01 from the 24-run
validation pass to match stream mode's rigor: full 8-point
`packet_threshold` grid `[3, 5, 10, 15, 20, 25, 30, 40]`, `sweep_repeats: 5`
(120 runs, ~50 min) — to get a real threshold trend for datagram mode
instead of the noisy `n=2` validation numbers above (which are too noisy
to draw threshold conclusions from, especially for NewReno's small
per-run packet counts of 5-11k vs. 60-136k for BBR/CUBIC).

## Open decisions (not yet resolved)

- **Real scheduler implementation** (§1) — deferred pending a decision on
  scope: full RTT-aware + redundant duplication vs. a narrower
  datagram-specific fix. Revisit once datagram-mode data is clean.
- **`packet_threshold` ceiling** (§3) — BBR's ceiling found around
  `pt=25-30` but not fully characterized (was `n=1`); CUBIC's ceiling not
  found at all within the tested range. §6's larger sweep should help.

## The one meta-lesson underlying everything above

Two things burned this investigation and shaped how it's now conducted:

- A "[FIXED] ... confirmed" doc entry that was not actually reflected in
  the source when checked later (§2a). **Never cite a fix without
  grepping the current code for it** — every claim in this document was
  re-verified against source on 2026-09-01 for exactly this reason.
- A metric reading exactly 0.000% uniformly across every configuration
  was an instrumentation bug, not a real result (§2c's gotcha). **Treat
  suspiciously uniform numbers as a signal to check the instrumentation
  first, not the network.**
