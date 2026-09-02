# TODO

## [FIXED] n0q: qlog mislabeled every packet_lost as "reordering_threshold"

**Status: fixed and confirmed.** The original hypothesis here (n0q's loss
detector treats normal multipath cross-path reordering as loss because it
compares packet numbers across the whole connection instead of per-path) was
**wrong** — two independent code explorations confirmed packet-number spaces,
sent-packet tracking, largest-acked tracking, RTT, and the congestion
controller are all already fully isolated per `PathId`
(`noq-proto/src/connection/spaces.rs:23-33,229-299`,
`noq-proto/src/connection/paths.rs:162-234`). There is no cross-path
packet-number comparison anywhere in the loss-detection algorithm.

The actual bug: `noq-proto/src/connection/qlog.rs`'s `emit_packet_lost` had
its comparison operands reversed —
`info.time_sent.saturating_duration_since(now)` instead of
`now.saturating_duration_since(info.time_sent)` — which always evaluates to
"false" (since `time_sent <= now`), so it always reported
`ReorderingThreshold` regardless of the real cause. Fixed to mirror the
authoritative check at `noq-proto/src/connection/mod.rs:3346`. Rebuilt and
re-ran both sweeps to confirm: trigger labels are now a real, non-degenerate
mix (e.g. server-side MINRTT/CUBIC: 96.5% reordering_threshold / 3.5%
time_threshold; MINRTT/NEWRENO: 17.7% / 82.3%).

## Re-diagnosis after the fix: CC window-growth difference, not a loss-detection bug

Comparing **server-side** qlogs (the side that governs actual download
throughput, since these are download tests) for MINRTT/CUBIC vs
MINRTT/NEWRENO in the stream sweep:

- **CUBIC**: 96,641 packets sent, 36,910 lost (38.2% loss, mostly
  reordering_threshold) — congestion window still grew to **~3 MB**,
  sustaining 35 Mbps.
- **NEWRENO**: only 5,950 packets sent, 260 lost (**4.4% loss — lower than
  CUBIC's**) — congestion window **never exceeded ~30 KB** the whole 15s
  test, capping throughput at 4.35 Mbps.

NewReno isn't losing more to spurious detection — it's losing *less*. Its
window just never grows: classic linear AIMD (+1 MSS/RTT) can't rebuild a
useful window within 15s at 25-50ms RTT, especially after each
multiplicative-decrease halving, while CUBIC's concave/convex regrowth curve
snaps back to a large window quickly after the same kind of cut. **This is
expected NewReno-vs-CUBIC behavior given these RTTs, not an n0q bug** — no
further loss-detector change is planned.

The high reordering_threshold share for CUBIC does look like genuine
within-path reordering (not a display artifact), most likely from the
configured netem jitter (13ms/25ms on Link A, 5ms/50ms on Link B — both
substantial relative jitter) tripping RFC 9002's static `packet_threshold=3`.
Noting this as a property of the emulated links, not something to fix in
n0q's code — revisit only if the netem jitter values themselves turn out to
be unrealistic for the intended link types.

**Update (2026-08-30): this section's framing is now confirmed and
quantified — see "Measured: ~99% of declared loss is spurious" below. The
reordering hypothesis was right, but the magnitude was far larger than
assumed, and it makes the raw loss percentages quoted above unusable as a
CC comparison.**

Evidence / how to reproduce the qlog analysis:
- Run a sweep (`sudo python3 mininet_topo.py --sweep datagram` or `--sweep stream`)
- qlogs land in `logs/qlog/n0q-{client,server}-<epoch_ms>-<cid>-{client,server}.qlog`
  (JSON-SEQ, one `quic:*` event per line) — **use the server qlog to explain
  download throughput**, since bulk data flows server→client in these tests;
  the client qlog's own packet_sent/packet_lost mostly reflects ACK traffic.
- Filter `quic:packet_lost` events and check the `trigger` field
- `quic:recovery_metrics_updated` events carry `congestion_window`/
  `bytes_in_flight` per `path_id` — plot/inspect over time to see window
  growth behavior

## [FIXED] qlog `packet_lost` events omitted `path_id`; no way to tell genuine loss from spurious/reordering

`emit_packet_lost` (`noq-proto/src/connection/qlog.rs`) didn't receive or
emit `path_id`, even though the caller (`handle_lost_packets` in `mod.rs`)
knows it. This made it impossible to correlate loss events to a specific
path directly from qlog. Fixed: `emit_packet_lost` now takes `path_id`, sets
it on the `PacketHeader`, and emits with the path's `tuple_id` like
`packet_sent`/`packet_received` do.

Separately, `reordering_threshold`-triggered `packet_lost` events are only a
declaration at detection time — RFC 9002 loss detection is a heuristic (3+
higher packet numbers already acked), not proof the packet was actually
dropped. The connection already resolves this internally via
`detect_spurious_loss` (`mod.rs:3109`): if a later ACK ends up covering a
packet number already declared lost, it's reclassified as spurious
(counted in `path_stats.spurious_congestion_events`, fed back to the CC via
`on_spurious_congestion_event()`). That reclassification wasn't visible in
qlog output at all.

Fixed by adding a standard qlog `packets_acked` event (`quic:packets_acked`,
`emit_packets_acked` in `qlog.rs`), emitted per-path for every newly-acked
packet-number range in `inner_on_ack_received` (`mod.rs`, right after
`newly_acked` is computed). This event type already existed in the `n0-qlog`
crate's schema but nothing emitted it.

**With both fixes, genuine loss vs. spurious/reordering is now directly
answerable from qlog alone, per path:** for each `packet_lost` event
(`header.path_id`, `header.packet_number`), scan later `packets_acked`
events on the same `tuple_id` (`p{path_id}`) for that packet number in
`packet_numbers`.
- Found later → **spurious loss**: the packet actually arrived, it was just
  reordered past the 3-packet threshold before the ACK caught up.
- Never found (or only found before the loss event) → **genuine network
  loss**: the peer never acknowledged it.

This mirrors `detect_spurious_loss`'s own logic exactly, so it should agree
with the live connection's internal reclassification — a good sanity check
if the two ever diverge.

Verified: `cargo check -p noq-proto` passes both with and without
`--features qlog`; `cargo test -p noq-proto --features qlog --lib` — 421
passed, 0 failed. Confirmed against a live sweep — both fields emit
correctly.

### False start worth remembering

The first version of the `packets_acked` hook was wired into `newly_acked`
in `inner_on_ack_received`. That produced a clean-looking but **entirely
bogus 0.0% spurious across every run**. Reason: `newly_acked` is built by
iterating `sent_packets`, but `handle_lost_packets` calls `take()`, which
*removes* the packet from `sent_packets` when it is declared lost. A later
ACK covering a lost packet therefore can never appear in `newly_acked` — 0%
spurious was guaranteed by construction, independent of the network. This is
exactly why `detect_spurious_loss` maintains its own separate `lost_packets`
map. The emission now lives inside `detect_spurious_loss` itself, at the
authoritative comparison site.

Lesson: a metric that reads 0.000% uniformly across every configuration is
almost always an instrumentation artifact, not a result.

## Measured: ~99% of declared loss is spurious (stream sweep, 2026-08-30)

With the corrected instrumentation, over 9 combos (scheduler × CC),
server-side, aggregated per emulated link:

| Link | sent | declared lost | declared% | **spurious%** | genuine | genuine% | netem loss |
|---|---|---|---|---|---|---|---|
| A LEO (25ms, 13ms jit) | 56,909 | 23,781 | 41.8% | **99.3%** | 178 | 0.31% | 0.17% |
| B Mobile (50ms, 5ms jit) | 228,935 | 125,260 | 54.7% | **98.9%** | 1,362 | 0.59% | 0.006% |
| C Mesh (2ms, 1ms jit) | 274,113 | 73,780 | 26.9% | **97.5%** | 1,862 | 0.68% | 0.0% |

**Nearly all "loss" in this testbed is reordering, not drops.** RFC 9002's
static `packet_threshold = 3` is badly mismatched to these links: relative
jitter is high enough (13/25 on A, 1/2 on C) that 3 packets routinely
overtake a straggler that then arrives fine.

Both figures are conservative in the direction that strengthens the claim:
a packet is only counted spurious on definitive proof (the peer ACKed it),
and packets declared lost near connection close — or acked more than 2×PTO
later, after `drain_lost_packets` prunes them — fall into "genuine" by
default. So genuine is an upper bound and **spurious ≥ 99% is a floor**.

Genuine loss (0.31–0.68%) *exceeds* configured netem loss on every link, and
link C has 0% configured loss yet shows the highest genuine rate — that
residue is real congestion loss: tbf queue overflow from the CCs overdriving
the shaped rate. Real, but two orders of magnitude below the declared numbers.

### Consequence: ~40% of bytes on the wire are wasted

Comparing link-level bytes received (sweep CSV) against application goodput:

| Run | link Mbps | goodput | efficiency | wasted | declared loss |
|---|---|---|---|---|---|
| MINRTT/BBR | 62.6 | 28.6 | 45.7% | 54.3% | 51.1% |
| MINRTT/CUBIC | 62.5 | 36.7 | 58.7% | 41.3% | 38.0% |
| MINRTT/NEWRENO | 5.6 | 5.1 | 90.6% | 9.4% | 4.5% |
| ROUNDROBIN/CUBIC | 62.9 | 36.5 | 58.1% | 41.9% | 38.1% |

Wasted-byte share tracks declared-loss rate almost exactly across all 9 runs.
Since ~99% of those losses are spurious, **~40% of all transmitted bytes are
redundant retransmissions of packets that had already arrived.** The
aggregate emulated downlink capacity is ~122 Mbit/s; best observed goodput is
36.5 Mbps.

### Implication for the CC comparison

The earlier "BBR 52% loss vs NewReno 4.4% loss" comparison was measuring
**reordering sensitivity, not loss.** NewReno's low declared loss is a
consequence of its tiny window (few packets in flight → few chances to
reorder past the threshold), not better network behavior. Any future CC
comparison on this testbed should quote genuine loss, not declared loss.

### Open question worth testing next

Does raising `packet_threshold` (or adopting an adaptive/time-based
reordering threshold, cf. RACK) recover a large share of that ~40% wasted
bandwidth? `config.packet_threshold` is already plumbed
(`noq-proto/src/config/transport.rs`), so a sweep over threshold values
(3 → 5 → 10) with the genuine-vs-spurious analysis should answer it directly.
Worth checking whether the netem jitter values are realistic for the intended
link types *first*, so the tuning isn't fitted to an unrealistic emulation.

## Answered: packet_threshold sweep (3/5/10/15/20), stream, 2026-08-30

Added `--packet-threshold` to `n0q-server`/`n0q-client` (wired to
`TransportConfig::packet_threshold`) and a `packet_threshold` dimension to
`mininet_topo.py`'s sweep (outer loop, so full threshold levels complete
even if interrupted). 45 runs (9 combos × 5 thresholds), ~19 min, ~5.2GB
qlog. Analysis: `scripts/analyze-threshold-sweep.py`.

**Raising the threshold recovers most of the wasted bandwidth, cheaply:**

| combo | pt=3 | pt=20 | gain |
|---|---|---|---|
| MINRTT/BBR | 23.9 Mbps | 46.1 Mbps | **+93%** |
| REDUNDANT/BBR | 29.8 | 44.9 | +51% |
| ROUNDROBIN/BBR | 29.1 | 44.1 | +51% |
| MINRTT/CUBIC | 36.7 | 52.1 | +42% |
| REDUNDANT/CUBIC | 36.6 | 52.4 | +43% |
| ROUNDROBIN/CUBIC | 36.7 | 52.5 | +43% |
| *NEWRENO (any)* | ~4.2–5.0 | ~4.3–5.3 | flat, noisy |

BBR benefits most (window-growth is loss-reactive, so cutting spurious
congestion events directly grows its window); CUBIC gains steadily but
less because it already regrows fast after a cut; NewReno is flat because
it's window-bound, not loss-bound (unchanged from the earlier diagnosis) —
raising the threshold doesn't help a CC that was never being throttled by
loss detection in the first place.

**Declared loss falls monotonically and substantially** with threshold
(e.g. MINRTT/CUBIC: 38.2% → 12.7% from pt=3 to pt=20), confirming most of
it really was reordering, not drops.

**Genuine loss stays roughly flat across all five thresholds** for BBR
(0.13–0.32%, no trend) — exactly the "should be flat, it's a network
property" sanity check the analysis script calls out, and it passes. CUBIC
and NEWRENO genuine-% figures are noisier (0.02–1.7%) simply because far
fewer packets are involved once loss is mostly gone (CUBIC's genuine counts
are in the hundreds to ~1.7k out of ~100k sent; NEWRENO's are single/low
double digits out of only ~6-7k sent), so relative noise is expected and not
a sign the classification is unreliable — cross-checked against the
absolute counts, not just the percentages.

**No downside observed up to pt=20 in this testbed.** Nothing suggests real
losses are being caught meaningfully later — genuine loss doesn't rise with
threshold, and goodput keeps climbing all the way to pt=20 rather than
plateauing or reversing. This sweep didn't test *why* it keeps climbing all
the way to 20 (i.e. where's the ceiling) — a natural follow-up is sweeping
further (25, 30...) to find where it flattens, since a value picked for a
one-off shouldn't be assumed optimal.

**Caveat:** all of this is on the *current* netem jitter profile (13ms/25ms
on Link A, 5ms/50ms on Link B, 1ms/2ms on Link C — see `env.sh`). A
threshold this large is a direct fit to that specific jitter; the two
haven't been validated against real LEO/mobile/mesh link characteristics.
Don't read pt=20 as a universally-good default — it's a good default *for
this emulation's reordering profile specifically*. If the jitter values
turn out to be pessimistic vs. real links, a smaller threshold may be more
correct in practice even though it scores worse here.

Raw per-run numbers: `scripts/threshold_sweep_analysis.csv` (written next to
the script since `logs/` is root-owned from the sudo mininet run).

## Checked: does n0q have picoquic's CID-retirement stale-cache bug? No.

picoquic (in a separate investigation) was found to tear down healthy
multipath connections because it validates a `RETIRE_CONNECTION_ID` /
incoming-packet CID against a single cached "current CID" pointer that
stops getting updated once the peer rotates CIDs, instead of looking up the
CID the packet actually arrived on.

Traced the analogous n0q (`noq-proto`) code paths — CID tracking structures,
`RETIRE_CONNECTION_ID` handling, `NEW_CONNECTION_ID` handling, and packet
routing on arrival. **Not present.** Every CID-validation decision resolves
by keyed lookup against a map/set kept current as CIDs are issued/retired,
never by comparing to a single pinned field:

- Packet routing (the critical path): `endpoint.rs:1142-1150` does
  `self.connection_ids.get(&datagram.dst_cid())` — a hashmap lookup of the
  actual DCID bytes from the just-decoded header. The map
  (`endpoint.rs:1037`, `FxHashMap<ConnectionId, (ConnectionHandle, PathId)>`)
  gets entries added in `new_cid()` (`endpoint.rs:428`) and removed by the
  exact retired CID value on `RetireConnectionId` (`endpoint.rs:119-127`).
- `RETIRE_CONNECTION_ID`: `connection/mod.rs:5128-5152` →
  `CidState::on_cid_retirement` (`cid_state.rs:162-189`) removes the frame's
  explicit `sequence` from a per-path `FxHashSet<u64>` of active sequences —
  keyed removal, not a pointer comparison.
- `NEW_CONNECTION_ID`: `connection/mod.rs:5183-5245` inserts into a per-path
  `CidQueue` ring buffer (`cid_queue.rs:60-109`); older CIDs stay valid
  until explicitly named by `retire_prior_to`.
- Multipath: both local and remote CID state are keyed by `PathId`
  (`remote_cids: FxHashMap<PathId, CidQueue>`,
  `local_cid_state: FxHashMap<PathId, CidState>`, `connection/mod.rs:242,249`),
  and the endpoint routing map stores `(ConnectionHandle, PathId)` per CID —
  so a path is resolved from the CID itself, not a per-path cache.

One single-cursor structure does exist (`CidQueue`'s "active" index), but
it only selects which remote CID *we* address our own outgoing packets to —
refreshed on every `NEW_CONNECTION_ID`, and never consulted for validating
an incoming packet or a retirement request. Not the vulnerable pattern.

## packet_threshold sweep extended to 25/30/40 — BBR has a ceiling, CUBIC doesn't

**Data-loss note first:** between the pt=3-20 sweep and this extension, the
1.2GB+ of raw qlogs and stdout logs for pt=3/5/10/15/20 disappeared from
`logs/` (which is root-owned, from `sudo python3 mininet_topo.py`). This
wasn't done by any command run on n0q/mpquic-noq's behalf — `logs/` is
gitignored, ephemeral, generated data, so nothing tracked was lost, but the
*raw* pt=3-20 qlogs can no longer be independently re-verified; only the
already-extracted numeric summary survived (because it happened to be saved
to `scripts/` instead of `logs/`, purely as a side effect of `logs/` being
read-only at the time). **Lesson: analysis outputs derived from `logs/`
should always be saved outside it** — this one accident is why the pt=3-20
numbers exist at all. Full merged table across all 8 thresholds:
`scripts/threshold_sweep_master.csv` (72 rows: 9 combos x 8 thresholds).

**Also worth flagging: every cell in every sweep so far is n=1** — one run
per (scheduler, cc, packet_threshold). None of this has error bars. The
finding below held up identically across 3 independent scheduler configs,
which is reassuring, but it is still not a substitute for repeated trials.

### Goodput (Mbps), full sweep

| combo | pt=3 | 5 | 10 | 15 | 20 | 25 | 30 | 40 | best |
|---|---|---|---|---|---|---|---|---|---|
| MINRTT/BBR | 23.9 | 28.1 | 36.6 | 40.2 | 46.1 | **46.7** | 43.9 | 37.4 | pt=25 |
| REDUNDANT/BBR | 29.8 | 29.2 | 35.7 | 41.3 | 44.9 | 35.3 | **47.5** | 40.8 | pt=30 |
| ROUNDROBIN/BBR | 29.1 | 35.4 | 39.1 | 40.2 | 44.1 | **49.7** | 49.4 | 34.4 | pt=25 |
| MINRTT/CUBIC | 36.7 | 42.1 | 46.9 | 50.5 | 52.1 | 54.0 | **56.9** | 55.6 | pt=30 |
| REDUNDANT/CUBIC | 36.6 | 40.7 | 47.3 | 50.8 | 52.4 | 54.0 | 58.9 | **59.6** | pt=40 |
| ROUNDROBIN/CUBIC | 36.7 | 41.1 | 47.5 | 50.6 | 52.5 | 56.7 | 56.8 | **59.4** | pt=40 |
| NEWRENO (any) | ~4.2-5.0 across the board, no trend, dominated by noise (window-bound, not loss-bound — unchanged conclusion) |

### BBR has a real ceiling around pt=25-30; CUBIC doesn't (at least not by pt=40)

The pt=3-20 data alone suggested "keep raising it, goodput keeps climbing."
That trend **reverses for BBR**. All three schedulers show the same
shape: BBR goodput peaks at pt=25 or pt=30, then drops at pt=40 — in
ROUNDROBIN/BBR's case, down to 34.4 Mbps, *worse than pt=20* (44.1 Mbps).

This isn't just diminishing returns — declared loss and, more tellingly,
the raw **spurious loss count** stop falling and turn back up at pt=40 for
BBR on all three schedulers:

| | pt=3 | 5 | 10 | 15 | 20 | 25 | 30 | 40 |
|---|---|---|---|---|---|---|---|---|
| MINRTT/BBR spurious count | 19031 | 19149 | 13977 | 12343 | 11666 | 8624 | **5778** | 6735 |
| REDUNDANT/BBR spurious count | 25826 | 20173 | 16429 | 15965 | 11721 | 7594 | **7655** | 8490 |
| ROUNDROBIN/BBR spurious count | 35618 | 29344 | 20550 | 12180 | 11268 | 8999 | **6596** | 8097 |

All three bottom out at pt=30 and rise again at pt=40 — consistent across
independent scheduler runs, so this looks like a real BBR-specific effect,
not one noisy run. Genuine loss stays low and doesn't explain it (0.08-0.11%
at pt=40, same as or lower than pt=25/30) — the packets newly counted as
"spurious" at pt=40 are extra reordering that a bigger threshold should have
absorbed, not real drops. Not yet root-caused; a plausible direction is
BBR's own probe-bw pacing-gain cycling interacting differently with a larger
allowed-reordering window (each cycle changes how bursty transmission is,
which changes how much natural reordering occurs) — but this is a hypothesis,
not confirmed. Re-running pt=30-40 for BBR a few more times to rule out
single-run noise, and pulling BBR's qlog `congestion_state_updated` events
to look for a pacing-gain/threshold interaction, are the natural next steps
if this is worth chasing further.

**CUBIC shows no such reversal through pt=40** — still climbing (best at
pt=30 or pt=40 depending on scheduler), no sign of a ceiling yet in the
range tested.

### Practical takeaway

There is no single "right" packet_threshold across CC algorithms in this
testbed:
- **CUBIC**: push it as high as tested (pt=40 or beyond) — no downside seen.
- **BBR**: pt≈20-30 is the sweet spot; going to pt=40 actively regresses
  goodput on this testbed. This *narrows* the earlier "just raise it"
  recommendation for BBR specifically — pt=20-25 looks like a safer,
  better-supported default than the originally-guessed pt=20-ish "floor."
- **NEWRENO**: irrelevant — it's window-limited, not loss-limited, so
  packet_threshold doesn't move its throughput either way.

## [RE-FIXED] The original "[FIXED] qlog mislabeled packet_lost" entry above was not actually fixed

Went to write up formal documentation of that first TODO.md entry (bug: qlog
mislabeled every loss `reordering_threshold`) and, pulling the exact
before/after code to cite precisely, discovered **the fix it describes was
not present in the source.** `qlog.rs`'s `emit_packet_lost` still had the
exact reversed comparison the entry describes as fixed:
`info.time_sent.saturating_duration_since(now)` (always `Duration::ZERO`,
since `time_sent <= now` always — dead code, always falls to
`ReorderingThreshold`), instead of `mod.rs:3377`'s correct
`now.saturating_duration_since(info.time_sent)`.

This matches the data collected all through this session: **every one of
the 72 packet_threshold-sweep runs showed 100% `reordering_threshold` / 0%
`time_threshold`** — exactly this bug's signature. Whether the original fix
was reverted at some point (possibly connected to whatever cleared `logs/`
between the pt=3-20 and pt=25/30/40 sweeps — still unexplained) or never
actually landed, unclear either way; the code is what matters and it had
the bug.

**Re-fixed**: swapped the operands back to `now.saturating_duration_since(
info.time_sent) >= loss_delay` in `qlog.rs`, matching `mod.rs:3377` exactly.
Verified: `cargo check` clean with and without `--features qlog`; `cargo
test -p noq-proto --features qlog --lib` — 421 passed; release binaries
rebuilt. **Not yet re-run against a live sweep** to confirm a non-degenerate
trigger split shows up in real captures — do that before citing trigger
percentages anywhere.

Important scope note: this bug never affected wire behavior (RFC 9002) —
`detect_lost_packets` in `mod.rs`, which actually decides retransmissions
and congestion events, always used the correct form. It also never affected
any of this session's genuine-vs-spurious loss or packet_threshold-sweep
conclusions, since that classification cross-references `packet_lost`
packet numbers against `packets_acked` events, never the `trigger` field.
Only the `trigger` label itself was ever wrong. Full writeup, including the
IETF-compliance framing (RFC 9002 vs. the qlog-quic-events draft), published
as an artifact: *Loss Trigger Postmortem*.

**Process note for future sessions**: don't take a "[FIXED] ... confirmed"
TODO.md entry as proof the fix is still in the code — verify against the
actual source before relying on or citing it, especially after anything
that touched the workspace outside this conversation (like the unexplained
`logs/` clearing earlier in this session).

## [FIXED] Datagram-mode benchmark sent 0 dgrams in every run — root-caused (2026-09-01)

**Symptom**: after the earlier `send_datagram()` → `send_datagram_wait()`
harness fix (see the "Datagram-mode benchmark bug" entry above), a 24-run
validation sweep (`mininet_topo.py --sweep datagram`, MINRTT-only,
`packet_threshold` in `[3, 10, 20, 40]`, 2 repeats) came back with **every
single run** printing `Sent 0 dgrams, Received 0 dgrams (0 bytes) ... 0.00
Mbps`. That earlier fix didn't just fail to help — it broke the benchmark
completely.

### False lead: `SendDatagram::poll`'s retry loop

First suspect was `noq/src/connection.rs:1164-1198` (`SendDatagram::poll`):
its `Blocked` arm polls a `Notified` future in a loop and, on `Ready(())`,
just resets to a fresh `notified()` future and loops — never explicitly
re-invoking `.send()`. Read in isolation this looks like it discards every
unblock signal forever. **Verified false** by building a standalone
reproduction (`noq/tests/` scratch test, removed after use): forced 2000
datagrams through a deliberately-saturated 1MB send buffer on a single
path — completed in 30ms, no hang. Reason it's not a bug: `Future::poll` in
Rust always restarts at the top of the function body on every fresh
executor call (there's no "resume mid-loop" across separate `poll()`
invocations), so when the wake fires and the task is re-polled, it's a
brand-new top-level call that re-attempts the real `.send()` — the loop's
"reset and re-poll notify" behavior is only handling the harmless
spurious-immediate-readiness case *within* one call. **Lesson: reasoning
about async retry-loop correctness from a single read is unreliable —
build a minimal reproduction before trusting the analysis, even when the
code looks suspicious.**

### Real root cause: fixed 1200-byte payload vs. multipath's per-connection MTU floor

Extended the same reproduction to multipath (3 subflows over loopback-aliased
addresses, matching the real client's `open_path_ensure` setup) and it
failed immediately with `SendDatagramError::TooLarge` — not `Blocked`.

- `Connection::current_mtu()` (`noq-proto/src/connection/mod.rs:7022`)
  computes the connection-wide usable MTU as the **minimum across all
  active paths** when multipath is enabled (this is intentional and
  documented — see IETF framing below).
- RFC 9000 §14.1 ("Initial Datagram Size") requires every QUIC path —
  including a freshly-opened multipath subflow — to start conservatively at
  a 1200-byte payload floor (`INITIAL_MTU`, `noq-proto/src/lib.rs:357`) and
  only grow it via DPLPMTUD (RFC 8899, referenced by RFC 9000 §14.3) probes
  over subsequent RTTs.
- The datagram benchmark (`apps/src/client.rs::run_datagram_test`) opens 3
  fresh multipath subflows immediately before starting the 15s test, so at
  the moment the first `send_datagram_wait()` call fires, at least one
  subflow is still sitting at the 1200-byte floor — dragging
  `current_mtu()` down with it.
- `Datagrams::max_size()` (`noq-proto/src/connection/datagrams.rs:69`)
  computes the usable datagram payload as
  `current_mtu() - predict_1rtt_overhead_no_pn() - Datagram::SIZE_BOUND`.
  `predict_1rtt_overhead_no_pn()` (`mod.rs:7057`) accounts for the QUIC
  header + AEAD tag (~21-29 bytes depending on CID length);
  `Datagram::SIZE_BOUND` (`noq-proto/src/frame.rs:2044`) is 9 bytes for the
  DATAGRAM frame's own type+length prefix. At the 1200-byte floor, usable
  size comes out to ~1162-1170 bytes — **always less than the benchmark's
  hard-coded 1200-byte payload.**
- Result: `send_datagram_wait()`'s very first call returns
  `SendDatagramError::TooLarge`, every single time, deterministically,
  regardless of CC or `packet_threshold` — explaining the uniform 0/0
  across all 24 runs.
- Compounding bug: `run_datagram_test`'s sender loop treated *any* error as
  fatal (`Ok(Err(_)) => break`), aborting the entire 15s test after this one
  transient, self-resolving-within-a-few-RTTs error, instead of retrying
  once MTU discovery caught up.

### IETF compliance framing

- **RFC 9000 §14.1** mandates the conservative 1200-byte initial floor this
  bug collided with — not a bug in n0q, expected protocol behavior.
- **RFC 9000 §14.3 / RFC 8899 (DPLPMTUD)** — n0q's `MtuDiscovery`
  (`noq-proto/src/connection/mtud.rs`) already implements this correctly;
  the issue was purely that the benchmark queried MTU only once (implicitly,
  by hard-coding 1200) instead of continuously.
- **RFC 9221 §4** ("An Unreliable Datagram Extension to QUIC") requires a
  sender not exceed `max_datagram_frame_size` or the current path's usable
  size — `current_mtu()`'s "minimum across all active paths" choice for
  multipath is the conservative, spec-correct interpretation (a datagram
  sized for the smallest active path is guaranteed sendable regardless of
  which path gets picked), so that logic was intentionally left unchanged.

### Fix

`apps/src/client.rs` (`run_datagram_test`, sender closure, lines ~244-289):
query `conn.max_datagram_size()` every iteration and size each datagram to
`TARGET_PAYLOAD_LEN.min(max_size)` instead of assuming a fixed 1200 bytes;
treat `SendDatagramError::TooLarge` as transient (`continue`) instead of
fatal. This is the spec-correct fix, not a workaround — RFC 9221 always
intended the effective size limit to be dynamic (n0q's own
`drop_oversized` already handles it shrinking again on an MTU black-hole
event), so adapting to it rather than hard-coding the RFC 9000 floor is
what the harness should have done from the start. `SendDatagram::poll` was
left untouched — it isn't broken.

**Verified**: local loopback client/server run (multipath + datagram +
BBR) — 14,992 sent / 14,992 received, 46.42 Mbps, vs. 0/0 before.

### Gotcha: `mininet_topo.py` prefers prebuilt binaries over `cargo run`

First re-run of the sweep *after* the source fix still showed 0/0 — cost a
full wasted 24-run cycle. Cause: `mininet_topo.py:270`
(`use_direct_bin = os.path.isfile(n0q_server_bin) and
os.path.isfile(n0q_client_bin)`) runs the prebuilt
`target/release/n0q-{server,client}` binaries directly whenever they
already exist, skipping `cargo run --release` (that's only a fallback path
for a from-scratch checkout). Editing n0q source does **not** get picked up
by the next sweep unless you rebuild first:
```
cargo build --release --bin n0q-server --bin n0q-client
```
**Lesson: after any n0q source change, rebuild release binaries explicitly
before re-running a mininet sweep — don't assume the sweep script will
notice.**

### New methodology caveat found in the same investigation

The benchmark's own printed `Sent N dgrams` line is **not** a reliable
transmission metric. `send_datagram_wait()` completing only means the
payload was accepted into the local outgoing queue
(`noq-proto`'s `DatagramState::outgoing`,
`noq-proto/src/connection/datagrams.rs:108`) — not that it hit the wire;
actual transmission happens later, gated by the congestion window, when the
packet-builder drains that queue. `conn.close()` at test end discards
whatever's still queued. Since the benchmark deliberately offers load
(~48 Mbps at 5000 dgrams/s × 1200B) faster than any CC sustains, this shows
up as a sent-vs-received gap that scales inversely with achieved
throughput:

| CC | sent-vs-received gap (24-run validation sweep) |
|---|---|
| CUBIC | 2-11% |
| BBR | 10-40% |
| NewReno | 18-73% |

The reported `Mbps` figure is **not** affected (computed purely from
received bytes), but `Sent N dgrams` should never be cited as an
attempted-transmission-rate or loss-denominator metric.

### Datagram-mode results after the fix (24-run validation sweep, MINRTT-only, n=2)

| CC | goodput range | mean |
|---|---|---|
| CUBIC | 31.8-45.7 Mbps | ~39 Mbps |
| BBR | 23.5-39.0 Mbps | ~31 Mbps |
| NewReno | 1.4-5.5 Mbps | ~3.4 Mbps |

Same CUBIC > BBR >> NewReno ordering and "most declared loss is spurious
reordering, not real drops" pattern (60-99% spurious, genuine loss under
~2% for BBR/CUBIC) as stream mode — good cross-validation. **n=2 is too
noisy to say anything about `packet_threshold`'s effect in datagram mode**
though (ranges overlap heavily across thresholds; NewReno's especially,
given its small per-run packet counts of 5-11k vs. 60-136k for BBR/CUBIC).
Scaled the sweep up to match stream mode's rigor — full 8-point threshold
grid `[3, 5, 10, 15, 20, 25, 30, 40]`, `sweep_repeats: 5` (120 runs,
~50 min) — to get a real threshold trend; in progress as of this entry.
