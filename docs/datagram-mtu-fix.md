# Datagram-mode benchmark fix — 2026-09-01

## TL;DR

The datagram-mode benchmark reported `Sent 0 dgrams, Received 0 dgrams,
0.00 Mbps` on every run of a 24-run sweep. The cause: the benchmark
hard-codes a 1200-byte payload, but RFC 9000 requires every QUIC path —
including a freshly-opened multipath subflow — to start at a conservative
1200-byte MTU floor, leaving no room for packet framing overhead. Fixed by
sizing each datagram to the connection's *current* usable size instead of
a fixed constant. Verified working: 0/0 → 14,992/14,992 sent/received on a
local test, and real non-zero throughput (23-46 Mbps for BBR/CUBIC) on the
live mininet sweep.

## The symptom

After an earlier fix switched the benchmark from `send_datagram()` to
`send_datagram_wait()` (to stop it from silently dropping unsent data under
backpressure), a 24-run validation sweep came back completely empty:

```
Sent 0 dgrams, Received 0 dgrams (0 bytes) in 15.02 seconds, 0.00 Mbps
```

Every single run — all three congestion controllers, all four
`packet_threshold` values tested. The connection itself worked fine
(handshake succeeded, all 3 multipath subflows came up), but zero
application data ever moved.

## Where the problem was located

**`apps/src/client.rs`, function `run_datagram_test` (n0q repo)** — the
benchmark's datagram-sending loop. It builds one fixed 1200-byte payload
before the loop starts and sends it repeatedly:

```rust
let payload = Bytes::from(vec![0x42; 1200]);
// ... send this same payload in a loop for the whole test duration
```

and treats any send failure as fatal:

```rust
match tokio::time::timeout(remaining, conn_send.send_datagram_wait(payload_clone.clone())).await {
    Ok(Ok(())) => dgrams_sent += 1,
    Ok(Err(_)) => break,   // <-- aborts the whole test on ANY error
    Err(_) => break,
}
```

## What I initially suspected (and ruled out)

My first guess was a retry-loop bug in the QUIC library itself —
`SendDatagram::poll` in `noq/src/connection.rs:1164-1198`, which waits on
an "unblocked" notification when the send buffer is full. Read in
isolation, it looked like it might discard that notification without ever
retrying the actual send.

I didn't trust that read on its own — I built a small standalone test that
forced 2000 datagrams through a deliberately-saturated 1MB send buffer on
a single path. It completed in **30 milliseconds**, no hang. That
disproved the theory: Rust's `Future::poll` always restarts at the top of
the function on the next executor call, so the retry does happen correctly.
**This code was not the bug and was left untouched.**

## The real root cause

Extending the same test to use multipath (3 subflows, like the real
client) reproduced the failure immediately — but as a different error,
`SendDatagramError::TooLarge`, not a hang.

The chain:

1. **RFC 9000 §14.1** mandates every QUIC path start at a conservative
   **1200-byte** payload floor and only grow it over time via Datagram
   Packetization Layer PMTU Discovery (**RFC 8899**, referenced from RFC
   9000 §14.3). This applies per-path — including a multipath subflow that
   was opened a few milliseconds ago.
2. With multipath enabled, n0q computes the connection's usable size as the
   **minimum MTU across all active paths**
   (`Connection::current_mtu()`, `noq-proto/src/connection/mod.rs:7022`).
   This is intentional, and correct per **RFC 9221 §4** ("An Unreliable
   Datagram Extension to QUIC"), which requires a sent datagram not exceed
   the current path's usable size — sizing to the smallest active path
   guarantees the datagram is safe to send on any of them.
3. The benchmark opens 3 fresh subflows *immediately before* starting the
   datagram test. At least one of them is still sitting at the 1200-byte
   floor at that moment, which drags the connection-wide usable size down
   with it.
4. After subtracting QUIC's own packet/AEAD overhead (~21-29 bytes) and the
   DATAGRAM frame's framing bytes (9 bytes), the actual usable payload size
   comes out to roughly **1162-1170 bytes** — always less than the
   benchmark's hard-coded 1200.
5. So the very first `send_datagram_wait()` call returns `TooLarge`, and
   the benchmark's `Ok(Err(_)) => break` aborts the whole 15-second test
   right there, having sent nothing. This is deterministic and independent
   of CC or `packet_threshold`, which is exactly the pattern observed
   (0/0 on all 24 runs).

## RFC / IETF references

| Reference | Relevance |
|---|---|
| **RFC 9000 §14.1** — Initial Datagram Size | Mandates the 1200-byte floor every path starts at; the value the benchmark's fixed payload collided with |
| **RFC 9000 §14.3** / **RFC 8899** — DPLPMTUD | Defines how a path's usable MTU is expected to grow over time via probing; n0q already implements this correctly (`noq-proto/src/connection/mtud.rs`) |
| **RFC 9221 §4** — An Unreliable Datagram Extension to QUIC | Requires a sent datagram not exceed `max_datagram_frame_size` or the current path's usable size; justifies why n0q's "minimum MTU across all active paths" multipath behavior is correct and was left unchanged |

## The fix

`apps/src/client.rs::run_datagram_test` now queries the connection's
*current* usable size on every iteration and sizes the payload to fit,
instead of assuming a fixed 1200 bytes:

```rust
let Some(max_size) = conn_send.max_datagram_size() else { break };
let payload_len = TARGET_PAYLOAD_LEN.min(max_size);
let payload = Bytes::from(vec![0x42; payload_len]);

match tokio::time::timeout(remaining, conn_send.send_datagram_wait(payload)).await {
    Ok(Ok(())) => dgrams_sent += 1,
    Ok(Err(SendDatagramError::TooLarge)) => continue, // transient, retry
    Ok(Err(_)) => break,
    Err(_) => break,
}
```

This is the spec-correct fix, not a workaround — RFC 9221 always intended
the effective size limit to be dynamic (it can shrink again too, e.g. on an
MTU black-hole event, which n0q already handles elsewhere via
`drop_oversized`). Hard-coding 1200 bytes was the actual bug; adapting to
the real, current limit is what the benchmark should have done from the
start.

## Verification

- **Local loopback test** (multipath + datagram + BBR): **14,992 sent,
  14,992 received**, 46.42 Mbps — versus 0/0 before the fix.
- **Live mininet sweep** (after rebuilding the release binaries — see
  gotcha below): real, non-zero throughput recovered across all three CCs,
  consistent with the ordering already established in stream-mode testing:

  | CC | goodput range | mean |
  |---|---|---|
  | CUBIC | 31.8-45.7 Mbps | ~39 Mbps |
  | BBR | 23.5-39.0 Mbps | ~31 Mbps |
  | NewReno | 1.4-5.5 Mbps | ~3.4 Mbps |

## Two things worth remembering

1. **Rebuild before re-testing.** `mininet_topo.py` runs the prebuilt
   `target/release/n0q-{server,client}` binaries directly if they already
   exist, and does *not* rebuild them automatically. A source fix in the
   n0q repo doesn't take effect in the next sweep until you run:
   ```
   cargo build --release --bin n0q-server --bin n0q-client
   ```
   This cost one full wasted 24-run sweep cycle before it was caught.

2. **The benchmark's "Sent N dgrams" counter overstates real transmission.**
   `send_datagram_wait()` completing only means the payload was accepted
   into a local outgoing queue, not that it was actually put on the wire —
   and whatever's still queued gets discarded when the test ends and the
   connection closes. This shows up as a sent-vs-received gap that gets
   worse the slower the CC is (CUBIC 2-11%, BBR 10-40%, NewReno 18-73%).
   The reported `Mbps` figure is unaffected (it's computed from bytes
   actually received), but don't use "Sent N dgrams" as a throughput or
   loss-rate metric on its own.
