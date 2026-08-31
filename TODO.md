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

## Minor, not yet fixed: qlog `packet_lost` events omit `path_id`

`emit_packet_lost` (`noq-proto/src/connection/qlog.rs:192`) doesn't receive
or emit `path_id` in the `PacketHeader`, even though the caller
(`handle_lost_packets` in `mod.rs`) knows it. This made it impossible to
correlate loss events to a specific path directly from qlog (had to infer
indirectly via per-path packet_sent counts instead). Low priority — worth
fixing if per-path loss analysis becomes a recurring need.
