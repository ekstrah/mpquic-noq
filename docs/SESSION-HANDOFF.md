# Session handoff — n0q multipath QUIC / datagram investigation

**If you're a fresh Claude session picking this up: read this file first.** It's
the TL;DR and "what's next." `TODO.md` (repo root) has the full chronological
log with all the detail; `docs/*.md` has focused writeups on individual
findings. Code changes described below are already committed (see "Auto-commit"
note at the bottom) — verify against current source before trusting any claim
here, including this document's own claims. That verify-first habit is itself
a lesson from this investigation (see "Trust nothing without checking" below).

## What this investigation is

Working with n0q (`../n0q` relative to this repo — `/home/chococake/Desktop/n0q`
on this machine; an earlier version of this doc said `/home/dongho/Desktop/git/n0q`,
which no longer exists here — always resolve via `mininet_topo.py`'s
`n0q_dir = os.path.join(repo_dir, "../n0q")` rather than trusting a hard-coded
path, since usernames/layouts have drifted across sessions), crate `noq-proto`, a
multipath QUIC implementation, tested against an emulated 3-link topology
(`mpquic-noq/mininet_topo.py` — LEO/mobile/mesh links with netem jitter/loss)
using `mpquic-noq/scripts/*.py` for analysis. **Datagram mode (RFC 9221) is
becoming the core transport** for the user's use case, so recent focus has
shifted from stream-mode bulk-transfer testing to datagram-mode reliability.

## Findings, most important first

1. **The multipath scheduler is a no-op.** `--scheduler MINRTT|REDUNDANT|ROUNDROBIN`
   is parsed in `apps/src/client.rs`, logged once, and never wired into
   `TransportConfig` or any path-selection code. Actual path selection
   (`Connection::poll_transmit`, `noq-proto/src/connection/mod.rs:1075`) is a
   fixed "always prefer lowest `PathId`, fall through only if it lacks
   capacity" order — not RTT-aware, no duplication, no rotation. Verified two
   ways: exhaustive grep across all 7 workspace crates for any
   Scheduler/MinRtt/Redundant/RoundRobin type (nothing), and statistically —
   holding CC and `packet_threshold` fixed, goodput spread across the three
   scheduler *labels* was ~10% (stream-mode, 72-point dataset) vs. ~145% spread
   across CC (a known real effect). **Every scheduler-attributed difference in
   this entire investigation's earlier reporting was run-to-run noise**, not a
   real scheduling algorithm difference. Not yet implemented for real — a
   deliberate decision was made to defer that (see "Open decisions" below).

2. **Three qlog diagnostics bugs found and fixed** in `noq-proto/src/connection/qlog.rs`
   — full writeup in `docs/qlog-loss-instrumentation-fixes.md`, IETF-compliance
   framing in `docs/loss-trigger-postmortem.md`:
   - `packet_lost`'s `trigger` field had reversed comparison operands, so it
     ALWAYS reported `reordering_threshold`, never `time_threshold`, regardless
     of actual cause. A prior TODO.md entry claimed this was already "fixed and
     confirmed" — it wasn't; the buggy code was still on disk when checked.
     Re-fixed. **None of this investigation's throughput/loss conclusions were
     affected** — they use packet-number cross-referencing (below), never the
     `trigger` field.
   - `packet_lost` events carried no `path_id` — added, so per-path loss
     attribution now works.
   - `packets_acked` (a standard qlog event) was never emitted at all — added,
     from two call sites (normal newly-acked packets, and retroactively-proven
     spurious losses inside `detect_spurious_loss`). This is what makes
     genuine-vs-spurious loss classification possible from qlog alone: a
     packet declared lost that's later covered by `packets_acked` on the same
     path was spurious (reordering); one that never is, was genuine.
   - **Gotcha if you touch this again:** the first attempt at the
     `packets_acked` wiring hooked into `newly_acked` (built from
     `sent_packets`), which gets emptied by `take()` the instant a packet is
     declared lost — guaranteeing a fake 0.0% spurious rate on every run,
     independent of the network. If a metric reads suspiciously uniform
     (especially exactly 0.000%), suspect the instrumentation first.

3. **`packet_threshold` sweep (stream mode, values 3/5/10/15/20/25/30/40,
   72-run dataset)**: ~99% of "declared loss" at the RFC 9002 default
   (`packet_threshold=3`) is spurious (reordering, not real drops) on this
   testbed's jitter profile. Raising the threshold recovers real throughput —
   but **BBR and CUBIC behave completely differently past pt=20**: BBR peaks
   around pt=25-30 then *regresses* (a real reversal, reproduced across all
   three scheduler labels — see finding #1 for why that's meaningful cross-
   validation despite scheduler being a no-op); CUBIC keeps improving through
   pt=40 with no ceiling found yet. NewReno is flat throughout — it's
   window-bound, not loss-bound, so the threshold doesn't matter for it.

4. **Checked whether n0q has picoquic's CID-retirement bug** (picoquic
   validates against a cached pointer instead of the actual arriving CID,
   causing spurious multipath connection teardown once the peer rotates
   CIDs). **n0q does not have this** — every CID-related decision (packet
   routing, `RETIRE_CONNECTION_ID` handling) resolves by keyed lookup against
   maps kept current as CIDs are issued/retired, never a cached pointer.

5. **Datagram-mode benchmark bug found and fixed — twice.** First bug: the
   test (`apps/src/client.rs::run_datagram_test`) used `send_datagram()`,
   which silently evicts old unsent data from the local send buffer under
   backpressure (`noq-proto/src/connection/datagrams.rs::make_space_for`,
   only `trace!`-logged). Fixed by switching to `send_datagram_wait()` —
   but that fix, applied without re-validating against a live sweep, turned
   out to break the benchmark completely: **every run of the next validation
   sweep reported "Sent 0 dgrams, Received 0 dgrams... 0.00 Mbps."**
   Root-caused as a *second*, unrelated bug: `run_datagram_test` hard-codes a
   1200-byte payload, but RFC 9000 §14.1 mandates every path — including a
   freshly-opened multipath subflow — start at a conservative 1200-byte MTU
   floor and grow it only via DPLPMTUD (RFC 8899) over subsequent RTTs; with
   multipath, `Connection::current_mtu()` (`noq-proto/src/connection/mod.rs:7022`)
   is the *minimum* MTU across all active paths (intentional — RFC 9221 §4
   requires a sent datagram not exceed the current path's usable size, and
   a size safe for the smallest active path is safe for any of them). Since
   the benchmark opens 3 fresh subflows moments before the test starts, at
   least one is still at the 1200-byte floor, and after QUIC/AEAD overhead
   (~21-29 bytes) and DATAGRAM framing (9 bytes) the usable size never
   reaches 1200 — so the very first `send_datagram_wait()` call returned
   `TooLarge`, and the benchmark's `Ok(Err(_)) => break` treated that as
   fatal, aborting the whole 15s test after 0 sends, on every single run.
   **Initially misdiagnosed** as a bug in `SendDatagram::poll`'s retry loop
   (`noq/src/connection.rs:1164-1198`) — disproven by building a standalone
   reproduction before touching that code (see `TODO.md`'s 2026-09-01 entry
   for the full story; that code was left untouched, it isn't broken).
   **Fixed** by querying `conn.max_datagram_size()` each send and sizing the
   payload to `min(1200, max_size)`, retrying (not aborting) on `TooLarge` —
   spec-correct per RFC 9221, since the size limit was always meant to be
   dynamic. **Verified**: loopback repro went from 0/0 to 14,992/14,992
   sent/received; live mininet re-run recovered real throughput (CUBIC
   ~39 Mbps mean, BBR ~31 Mbps, NewReno ~3.4 Mbps — same ordering as stream
   mode). Also surfaced a **separate methodology caveat**: the benchmark's
   printed "Sent N dgrams" undercounts nothing but *overstates* real wire
   transmission — `send_datagram_wait()` completing only means "accepted
   into the local outgoing queue," and `conn.close()` at test end discards
   whatever's still queued there. Gap scales inversely with throughput
   (CUBIC 2-11%, BBR 10-40%, NewReno 18-73% sent-but-never-received) — the
   reported `Mbps` (computed from received bytes only) is unaffected, but
   don't cite "Sent N dgrams" as a transmission-rate metric.

## Trust nothing without checking

Two things burned this investigation and are worth internalizing:

- A `TODO.md` entry marked "[FIXED] ... confirmed" was not actually reflected
  in the source when checked months/sessions later. Don't cite a fix without
  grepping the current code for it.
- A metric reading exactly 0.000% uniformly across every configuration was
  an instrumentation bug, not a real result (see finding #2's gotcha).
- The `logs/` directory (gitignored, ephemeral sweep output) disappeared
  entirely mid-session once, cause never determined — cleared by something
  outside this conversation. Nothing tracked was lost, but it means `logs/`
  should never be assumed to persist between sessions; anything worth keeping
  from it should be extracted into a committed file promptly (this is why
  `scripts/threshold_sweep_*.csv` exist as separate saved snapshots).
- `mininet_topo.py` runs prebuilt `target/release/n0q-{server,client}`
  binaries directly whenever they already exist (`use_direct_bin`,
  `mininet_topo.py:270`), only falling back to `cargo run --release` on a
  from-scratch checkout. A source fix in `../n0q` does **not** get picked up
  by the next sweep unless you `cargo build --release --bin n0q-server --bin
  n0q-client` first — cost a full wasted 24-run sweep cycle on 2026-09-01
  when a datagram-benchmark fix's binary was never rebuilt.

## Auto-commit note

Both `/home/dongho/Desktop/git/n0q` and this repo appear to have an automated
process periodically committing with generic `"update"` messages, independent
of this conversation (visible in `git log --oneline` as long runs of `update`
commits). This means `git status` reading clean does NOT mean nothing
changed recently — check `git log -1 --format=%ci -- <file>` for a file's
actual last-modified-and-committed time if timing matters, and don't assume
uncommitted work is safe just because no one explicitly committed it — it may
get swept up (or, per the `logs/` incident above, something may get cleared)
by whatever this process is. Nobody in this conversation has identified what
it is.

## Open decisions (deliberately not resolved yet)

- **Real scheduler implementation.** Given finding #1, MINRTT/REDUNDANT/
  ROUNDROBIN don't exist as real algorithms. Explicitly deferred rather than
  built, pending a decision on how far to take it (full RTT-aware + redundant
  duplication implementation vs. a narrower datagram-specific fix). Revisit
  once datagram-mode data is clean.
- **How far to push `packet_threshold`.** BBR's ceiling was found around
  pt=25-30 but not fully characterized (only n=1 per point at pt=25-40); CUBIC's
  ceiling wasn't found at all within the tested range.

## Current in-flight state (as of this handoff)

The user runs sweeps themselves (mininet requires root; this session's
sandbox can't `sudo` — confirmed still true as of 2026-09-01, though `mn` is
installed locally so this may be the *same* machine, just requiring the
user's own sudo access rather than a genuinely separate remote host). The
small first-pass validation sweep (`packet_threshold` in `[3, 10, 20, 40]`,
`sweep_repeats: 2`) is done and analyzed for both stream and (after the
finding-#5 fix + rebuild) datagram mode — see `TODO.md`'s 2026-09-01 entry
for datagram-mode results. `mininet_topo.py` config has been scaled up to
match stream mode's rigor: `packet_threshold` in
`[3, 5, 10, 15, 20, 25, 30, 40]` (full 8-point grid), `sweep_repeats: 5`
(120 runs, ~50 min) — **a sweep with this config is in progress as of this
handoff**; scheduler stays collapsed to `MINRTT` only (3x redundant sweeping
removed per finding #1).

**To analyze once results land:** only `logs/qlog/*-server.qlog` and
`logs/stdout/client_*.log` (+ optionally `server_*.log`, `sweep_*_summary.csv`)
are needed — client-side qlogs are never read by the analysis scripts. Sync
into this repo's `logs/` preserving the `stdout/`/`qlog/` subdirectory
structure, then run:

```
python3 scripts/analyze-threshold-sweep.py logs stream
python3 scripts/analyze-threshold-sweep.py logs datagram
```

This script is repeat-aware (parses `_r<N>` filename suffixes, reports
mean/min/max across repeats per combo) and backward-compatible with older
single-run filenames (defaults to rep=1). It refuses to run if any two runs
pair to the same qlog file (a broken-pairing safety check, added after an
earlier timezone bug in an older version of this script caused exactly that
kind of silent collision once).

## File map

- `TODO.md` (repo root) — full chronological working log, most detail
- `docs/loss-trigger-postmortem.md` — the trigger-mislabeling bug, IETF
  compliance framing (RFC 9002 vs. qlog-quic-events draft)
- `docs/qlog-loss-instrumentation-fixes.md` — all three qlog bugs, developer
  reference format (what/where/how fixed)
- `scripts/analyze-threshold-sweep.py` — main analysis tool, repeat-aware
- `scripts/analyze-spurious-loss.py` — earlier, simpler version (single-run,
  no repeat support) — superseded by analyze-threshold-sweep.py but still
  works for quick one-off checks
- `scripts/threshold_sweep_analysis_datagram.csv` — the datagram-mode
  packet_threshold sweep dataset (120-run full sweep, done and analyzed as
  of 2026-09-03; see TODO.md for the run history and earlier smaller
  datasets that fed into it, since superseded and removed from the repo)
- `viewer/index.html` — self-contained HTML dashboard for the datagram
  sweep results (local copy of a dashboard also published as a Claude
  artifact)
