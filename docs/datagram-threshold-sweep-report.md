# Datagram-mode `packet_threshold` sweep — full results (2026-09-01)

120 runs: MINRTT scheduler × {BBR, CUBIC, NewReno} × `packet_threshold`
∈ {3, 5, 10, 15, 20, 25, 30, 40} × 5 repeats, 15s per run. This is the
full-grid, 5-repeat follow-up to the 24-run (n=2) validation pass done
right after fixing the datagram benchmark itself (see
`docs/datagram-mtu-fix.md` / `docs/investigation-summary.md` §5). Raw data:
`scripts/threshold_sweep_analysis_datagram.csv`.

## Headline: goodput by threshold (mean [min-max] across 5 repeats)

| CC | pt=3 | pt=5 | pt=10 | pt=15 | pt=20 | pt=25 | pt=30 | pt=40 | best (mean) |
|---|---|---|---|---|---|---|---|---|---|
| **BBR** | 28.6 | 34.4 | 32.3 | 35.0 | 33.2 | 34.1 | 33.0 | **35.2** | pt=40 |
| **CUBIC** | 35.1 | 39.1 | 36.1 | **43.2** | 42.6 | 41.1 | 38.9 | 33.5 | pt=15 |
| **NewReno** | 3.5 | 3.8 | 3.6 | 4.2 | 4.8 | **5.3** | 4.5 | 4.1 | pt=25 |

Full min-max ranges are in `scripts/threshold_sweep_analysis_datagram.csv`;
NewReno's "best" number needs the caveat below before you trust it.

## Finding 1 — BBR: no ceiling in datagram mode (unlike stream mode)

In stream mode, BBR peaked around `pt=25-30` and then **regressed** hard by
`pt=40` (down to 34.4 Mbps in one combo, worse than `pt=20`). **That
regression does not reproduce in datagram mode.** BBR jumps from 28.6 Mbps
at `pt=3` to ~32-35 Mbps by `pt=5` and then stays essentially flat
(32-35 Mbps) all the way through `pt=40`, with no downturn. The min-max
ranges overlap heavily across pt=5 through pt=40, so beyond "raising the
threshold off the RFC 9002 default of 3 clearly helps," there's no
threshold-tuning story here — pick anything from `pt=5` up.

**Why the stream-mode ceiling might not carry over**: datagram frames
aren't retransmitted, so datagram-mode CC only reacts to loss for
congestion-window purposes, never to actually resend data. If the
stream-mode BBR regression was tied to pacing-gain / retransmission
interaction (the working hypothesis in the stream-mode writeup, never
confirmed), removing retransmission from the picture could plausibly
remove the effect too. Not confirmed — just the most likely explanation
given what changed between the two sweeps.

## Finding 2 — CUBIC: the opposite of the stream-mode story — it now has a ceiling

Stream mode found "CUBIC keeps improving through pt=40, no ceiling found."
**Datagram mode shows the reverse**: CUBIC rises from 35.1 Mbps (`pt=3`) to
a clear peak at `pt=15-20` (43.2 / 42.6 Mbps), then **declines** through
`pt=25` (41.1), `pt=30` (38.9), and `pt=40` (33.5 — back down near the
`pt=3` floor). This is a genuinely new and non-obvious result: the two
transport modes disagree on which CC has a `packet_threshold` ceiling.
Given datagram frames skip retransmission entirely, this reinforces that
datagram-mode CC dynamics are **not a simple subset of stream-mode
dynamics** — thresholds tuned against one mode's sweep shouldn't be
assumed to transfer to the other.

**Practical takeaway for datagram mode specifically**: if you have to pick
one `packet_threshold` for CUBIC, `pt=15-20` is the sweet spot, not "as
high as possible" (which was the stream-mode recommendation).

## Finding 3 (new) — NewReno intermittently collapses to near-zero throughput, independent of threshold

This is the most important thing this larger sweep surfaced — invisible at
`n=2`, unmistakable at `n=5`. **6 of the 40 NewReno runs (15%)** show
throughput crashing to 0.14-1.67 Mbps — 3 to 30x lower than every other
NewReno run — with a **distinctly different loss signature**, not just a
lower number:

| run | Mbps | sent | genuine loss % | spurious % |
|---|---|---|---|---|
| pt=3 rep=4 | 0.23 | 3,942 | 6.14% | 35.6% |
| pt=5 rep=2 | 0.16 | 4,220 | 6.09% | 45.6% |
| pt=10 rep=3 | 0.31 | 4,779 | 5.71% | 41.3% |
| pt=15 rep=5 | 0.15 | 4,311 | 6.05% | 46.8% |
| pt=30 rep=4 | 1.67 | 5,836 | 4.20% | 49.6% |
| pt=40 rep=3 | 0.14 | 4,207 | 5.82% | 48.5% |
| *all other 34 NewReno runs* | 3.6-6.5 | 7,500-12,500 | 0.86-2.56% | 67.8-85.2% |

Confirmed against the raw logs, not just the summary numbers — e.g.
`pt=3 rep=4`: `Sent 7809 dgrams, Received 374 dgrams`. That's a **95%**
delivery failure, far beyond the normal 18-73% sent-vs-received queueing
gap documented in the fix writeup (which is a local-queue artifact, not
real loss). This is real: genuine (non-retransmitted, confirmed-never-acked)
packet loss more than doubles (4-6% vs 1-2.5%), and the *proportion* of
declared loss that's spurious (reordering) drops sharply (35-50% vs
68-85%) — consistent with a congestion window that collapsed early and
stayed pinned near its floor for most of the 15s test: a tiny window has
almost nothing in flight to reorder, so what loss does happen is much more
likely to be real.

**This happens across every threshold value tested, roughly evenly** — it
is not something raising `packet_threshold` fixes. It's very likely the
same underlying mechanism as the already-documented stream-mode finding
("NewReno's window never exceeded ~30KB in 15s — AIMD can't rebuild fast
enough after a multiplicative-decrease cut at these RTTs"), just showing
its worst case here: if NewReno's window gets cut hard enough early in the
test, it sometimes never recovers meaningfully within the 15s window at
all, rather than merely staying small.

**This also explains the apparent "NewReno peaks at pt=25" in the headline
table above — that's mostly an artifact, not a real threshold effect.**
`pt=20` and `pt=25` happened to draw zero collapse runs in this particular
5-repeat sample while every other threshold drew one; with only 15%
per-run collapse odds, getting 0-of-5 at exactly two thresholds and 1-of-5
at the rest is well within what you'd expect from chance. Recomputing the
per-threshold mean **excluding the 6 collapsed runs**:

| threshold | 3 | 5 | 10 | 15 | 20 | 25 | 30 | 40 |
|---|---|---|---|---|---|---|---|---|
| mean, collapses excluded | 4.26 | 4.67 | ~4.5-5.1 | 5.15 | 4.77 | 5.30 | 5.18 | 5.10 |

Once the collapse runs are pulled out, NewReno is basically **flat at
~4.3-5.3 Mbps regardless of threshold** — consistent with the original
stream-mode conclusion that NewReno is window-bound, not loss-bound, so
`packet_threshold` doesn't move it either way. The "best at pt=25" result
in the raw mean is sample luck, not a real effect. **Don't tune
`packet_threshold` for NewReno based on this sweep's raw per-threshold
means — use the collapse-adjusted numbers, or better, run enough repeats
per threshold that the collapse rate itself averages out (would need
roughly n≈15-20 per threshold to keep the collapse-count noise down to
about ±1 run).**

This collapse behavior is worth its own follow-up (not done as part of
this sweep): pull `congestion_window`/`bytes_in_flight` from the qlog of a
collapsed run (e.g. `pt=40 rep=3`) and compare against a normal run at the
same threshold to see exactly when and how hard the window gets cut, and
whether it ever attempts to recover before the 15s cutoff.

## Sanity checks that passed

- **Genuine loss stays roughly flat across all 8 thresholds** for both
  BBR (0.64-1.20%) and CUBIC (0.61-1.03%) — the expected "it's a network
  property, not a detector artifact" signature, and it holds. NewReno's
  genuine loss (1.34-2.76%) is pulled upward by the collapse runs but is
  still in a similar ballpark once you account for them.
- **Declared loss is still dominated by spurious/reordering**, not real
  drops, across the whole grid (89-99% spurious for the large majority of
  BBR/CUBIC runs) — consistent with every earlier finding in this
  investigation, in both transport modes.

## What this changes vs. the earlier (n=2) validation pass

The n=2 pass flagged itself as too noisy to trust for a threshold trend —
that caution was justified. With n=5:
- BBR's apparent "keeps rising through pt=40" from the small pass is
  **confirmed directionally but the magnitude was noise** — it's flat from
  pt=5 onward, not still climbing.
- CUBIC's small-pass numbers (peaking at pt=40) turned out to be **backwards**
  — the larger sample shows a clear peak at pt=15-20 and a decline by pt=40.
- NewReno's apparent "peaks at pt=20" from the small pass is **explained**,
  not confirmed — it was collapse-run sampling luck, and the real
  underlying behavior is flat.

This is the expected value of increasing repeats: two of three CC
conclusions from the small pass didn't survive contact with a larger
sample. Treat single-repeat or double-repeat sweep cells in this project's
history with the same skepticism going forward.
