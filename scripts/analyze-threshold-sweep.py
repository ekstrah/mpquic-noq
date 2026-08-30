#!/usr/bin/env python3
"""
Analyzes a packet_threshold sweep: for each (scheduler, cc, packet_threshold)
run, splits declared packet loss into genuine network loss vs. spurious loss
(reordering that tripped the threshold), and pairs it with goodput.

A `packet_lost` event is only the loss detector's guess at declaration time. A
packet declared lost that is covered by a *later* `packets_acked` event on the
same path did arrive - it was reordered, not dropped. This mirrors what n0q's
own `detect_spurious_loss` computes internally.

Requires qlogs captured with path_id on packet_lost and the packets_acked
event emitted (see TODO.md).

Usage: analyze-threshold-sweep.py [logs_dir]
"""

import sys
import os
import re
import glob
import json
from collections import defaultdict
from datetime import datetime, timezone

TS_RE = re.compile(rb"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z")
GOODPUT_RE = re.compile(r"([\d.]+)\s*Mbps")
# client_stream_MINRTT_CUBIC_pt10_r2.log  (the _r<N> suffix is optional, for
# older captures taken before repeats existed - those default to rep=1)
TAG_RE = re.compile(
    r"client_\w+?_(?P<sched>[A-Z]+)_(?P<cc>[A-Z0-9]+)_pt(?P<pt>\d+)"
    r"(?:_r(?P<rep>\d+))?\.log$"
)
QLOG_EPOCH_RE = re.compile(r"n0q-server-(\d+)-")


def mean_spread(values):
    """Returns (mean, min, max, n) for a list of numbers."""
    n = len(values)
    return sum(values) / n, min(values), max(values), n


def client_start_epoch_ms(path):
    """Epoch ms of the client's first log line."""
    with open(path, "rb") as f:
        for line in f:
            m = TS_RE.search(line)
            if m:
                ts = m.group(1).decode()
                # log timestamps carry a literal 'Z' (UTC) - treat as such explicitly,
                # or a non-UTC system timezone silently shifts every start estimate by
                # a constant offset, which can collapse "nearest qlog" onto one file.
                dt = datetime.strptime(ts[:26], "%Y-%m-%dT%H:%M:%S.%f").replace(
                    tzinfo=timezone.utc
                )
                return dt.timestamp() * 1000.0
    return None


def scan_qlog(path):
    """Single pass over a server qlog.

    Returns (sent_per_path, lost_events, acked) where lost_events maps
    path_id -> [(time, pn)] and acked maps path_id -> pn -> [times].
    """
    sent = defaultdict(int)
    lost = defaultdict(list)
    acked = defaultdict(lambda: defaultdict(list))

    with open(path, "rb") as f:
        for raw in f:
            # cheap prefilter before paying for json parsing
            if b'"quic:packet_sent"' in raw:
                m = re.search(rb'"tuple":"p(\d+)"', raw)
                if m:
                    sent[int(m.group(1))] += 1
                continue
            if b'"quic:packet_lost"' in raw:
                ev = _load(raw)
                if ev is None:
                    continue
                hdr = (ev.get("data") or {}).get("header") or {}
                pn = hdr.get("packet_number")
                pid = hdr.get("path_id")
                if pid is None:
                    pid = _tuple_path(ev)
                if pn is None or pid is None:
                    continue
                lost[pid].append((ev.get("time", 0.0), pn))
                continue
            if b'"quic:packets_acked"' in raw:
                ev = _load(raw)
                if ev is None:
                    continue
                pid = _tuple_path(ev)
                if pid is None:
                    continue
                t = ev.get("time", 0.0)
                for pn in (ev.get("data") or {}).get("packet_numbers") or []:
                    acked[pid][pn].append(t)
    return sent, lost, acked


def _load(raw):
    try:
        return json.loads(raw.strip().lstrip(b"\x1e"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _tuple_path(ev):
    t = ev.get("tuple")
    if isinstance(t, str) and t.startswith("p"):
        try:
            return int(t[1:])
        except ValueError:
            return None
    return None


def classify(lost, acked):
    """Returns (genuine, spurious) totals across all paths.

    Spurious = declared lost, then acknowledged strictly afterwards.
    """
    genuine = spurious = 0
    for pid, events in lost.items():
        per_pn = acked.get(pid, {})
        for t_lost, pn in events:
            if any(t > t_lost for t in per_pn.get(pn, ())):
                spurious += 1
            else:
                genuine += 1
    return genuine, spurious


def main(logs_dir="logs", mode="stream"):
    qdir = os.path.join(logs_dir, "qlog")
    sdir = os.path.join(logs_dir, "stdout")
    if not os.path.isdir(qdir) or not os.path.isdir(sdir):
        print(f"Error: expected {qdir} and {sdir}")
        return 1

    # index server qlogs by their embedded connection-start epoch (ms)
    qlogs = []
    for p in glob.glob(os.path.join(qdir, "*-server.qlog")):
        m = QLOG_EPOCH_RE.search(os.path.basename(p))
        if m:
            qlogs.append((int(m.group(1)), p))
    if not qlogs:
        print(f"No server qlogs in {qdir}")
        return 1

    runs = []
    for c in sorted(glob.glob(os.path.join(sdir, f"client_{mode}_*_pt*.log"))):
        m = TAG_RE.search(os.path.basename(c))
        if not m:
            continue
        start = client_start_epoch_ms(c)
        if start is None:
            continue
        # pair with the server qlog whose connection start is nearest
        epoch, qpath = min(qlogs, key=lambda kv: abs(kv[0] - start))
        text = open(c, errors="ignore").read()
        g = GOODPUT_RE.search(text)
        runs.append({
            "sched": m.group("sched"), "cc": m.group("cc"),
            "pt": int(m.group("pt")),
            "rep": int(m.group("rep")) if m.group("rep") else 1,
            "qlog": qpath,
            "goodput": float(g.group(1)) if g else 0.0,
            "skew_ms": abs(epoch - start),
        })

    if not runs:
        print(f"No client_*_pt*.log runs found in {sdir} - was the sweep run "
              f"with the packet_threshold dimension?")
        return 1

    bad = [r for r in runs if r["skew_ms"] > 20000]
    if bad:
        print(f"!! {len(bad)} run(s) paired to a qlog >20s away - pairing may be wrong\n")

    # Each combo/threshold ran its own connection, so each run must map to a
    # distinct qlog. A collision here means every colliding run will silently
    # report identical, meaningless stats - refuse to proceed rather than
    # print numbers that look plausible but aren't.
    qlog_to_runs = defaultdict(list)
    for r in runs:
        qlog_to_runs[r["qlog"]].append(r)
    collisions = {q: rs for q, rs in qlog_to_runs.items() if len(rs) > 1}
    if collisions:
        print(f"!! {len(runs)} runs mapped to only {len(qlog_to_runs)} distinct qlogs "
              f"- pairing is broken, aborting.\n")
        for q, rs in list(collisions.items())[:3]:
            tags = ", ".join(f"{r['sched']}/{r['cc']}/pt{r['pt']}/r{r['rep']}" for r in rs)
            print(f"  {os.path.basename(q)} <- {tags}")
        return 1

    print(f"Analyzing {len(runs)} runs...\n", flush=True)
    results = []
    for i, r in enumerate(sorted(runs, key=lambda r: (r["cc"], r["sched"], r["pt"], r["rep"])), 1):
        sent, lost, acked = scan_qlog(r["qlog"])
        gen, spur = classify(lost, acked)
        total_sent = sum(sent.values())
        total_lost = gen + spur
        r.update(sent=total_sent, lost=total_lost, genuine=gen, spurious=spur)
        results.append(r)
        print(f"  [{i}/{len(runs)}] {r['sched']}/{r['cc']} pt={r['pt']} rep={r['rep']}", flush=True)

    print()
    print("=" * 104)
    print("Per-run: genuine vs spurious loss by packet_threshold")
    print("=" * 104)
    hdr = (f"{'SCHED':<11} {'CC':<8} {'PT':>3} {'REP':>3} {'SENT':>8} {'LOST':>8} "
           f"{'DECL%':>7} {'SPUR%':>7} {'GENUINE':>8} {'GEN%':>6} {'Mbps':>7}")
    last = None
    for r in results:
        key = (r["cc"], r["sched"])
        if key != last:
            print("\n" + hdr)
            last = key
        decl = r["lost"] / r["sent"] * 100 if r["sent"] else 0
        sp = r["spurious"] / r["lost"] * 100 if r["lost"] else 0
        gp = r["genuine"] / r["sent"] * 100 if r["sent"] else 0
        print(f"{r['sched']:<11} {r['cc']:<8} {r['pt']:>3} {r['rep']:>3} {r['sent']:>8} "
              f"{r['lost']:>8} {decl:>6.1f}% {sp:>6.1f}% {r['genuine']:>8} "
              f"{gp:>5.2f}% {r['goodput']:>7.2f}")

    # aggregate across repeats: (sched, cc, pt) -> list of per-repeat result dicts
    pts = sorted({r["pt"] for r in results})
    by_combo = defaultdict(lambda: defaultdict(list))
    for r in results:
        by_combo[(r["sched"], r["cc"])][r["pt"]].append(r)
    n_repeats = max((len(rs) for d in by_combo.values() for rs in d.values()), default=1)

    print("\n" + "=" * 104)
    print(f"Goodput (Mbps) by packet_threshold - mean [min-max] across up to {n_repeats} repeat(s)")
    print("=" * 104)
    print(f"{'SCHED/CC':<22}" + "".join(f"{('pt=' + str(p)):>18}" for p in pts) + f"{'best (mean)':>14}")
    for (s, cc), d in sorted(by_combo.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        cells = ""
        means = {}
        for p in pts:
            if p in d:
                m, lo, hi, n = mean_spread([r["goodput"] for r in d[p]])
                means[p] = m
                cells += f"{f'{m:.2f} [{lo:.2f}-{hi:.2f}]':>18}"
            else:
                cells += f"{'-':>18}"
        best_pt = max(means, key=means.get) if means else None
        best_str = f"pt={best_pt}" if best_pt is not None else "-"
        print(f"{s + '/' + cc:<22}{cells}{best_str:>14}")

    print("\n" + "=" * 104)
    print("Declared loss % (how often the detector cried wolf) - mean across repeats")
    print("=" * 104)
    print(f"{'SCHED/CC':<22}" + "".join(f"{('pt=' + str(p)):>10}" for p in pts))
    for (s, cc), d in sorted(by_combo.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        cells = ""
        for p in pts:
            if p in d:
                vals = [r["lost"] / r["sent"] * 100 for r in d[p] if r["sent"]]
                cells += f"{(sum(vals) / len(vals) if vals else 0):>9.1f}%"
            else:
                cells += f"{'-':>10}"
        print(f"{s + '/' + cc:<22}{cells}")

    print("\n" + "=" * 104)
    print("Genuine loss % of packets sent (should be ~flat - it is a network property) - mean across repeats")
    print("=" * 104)
    print(f"{'SCHED/CC':<22}" + "".join(f"{('pt=' + str(p)):>10}" for p in pts))
    for (s, cc), d in sorted(by_combo.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        cells = ""
        for p in pts:
            if p in d:
                vals = [r["genuine"] / r["sent"] * 100 for r in d[p] if r["sent"]]
                cells += f"{(sum(vals) / len(vals) if vals else 0):>9.2f}%"
            else:
                cells += f"{'-':>10}"
        print(f"{s + '/' + cc:<22}{cells}")

    csv_body = "scheduler,cc,packet_threshold,repeat,sent,declared_lost,declared_pct,spurious," \
               "spurious_pct,genuine,genuine_pct,goodput_mbps\n"
    for r in results:
        decl = r["lost"] / r["sent"] * 100 if r["sent"] else 0
        sp = r["spurious"] / r["lost"] * 100 if r["lost"] else 0
        gp = r["genuine"] / r["sent"] * 100 if r["sent"] else 0
        csv_body += (f"{r['sched']},{r['cc']},{r['pt']},{r['rep']},{r['sent']},{r['lost']},"
                     f"{decl:.2f},{r['spurious']},{sp:.2f},{r['genuine']},"
                     f"{gp:.3f},{r['goodput']:.2f}\n")

    out_name = f"threshold_sweep_analysis_{mode}.csv"
    out = os.path.join(logs_dir, out_name)
    try:
        with open(out, "w") as f:
            f.write(csv_body)
    except PermissionError:
        # logs_dir is commonly root-owned (files written by a sudo mininet run)
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), out_name)
        with open(out, "w") as f:
            f.write(csv_body)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(
        sys.argv[1] if len(sys.argv) > 1 else "logs",
        sys.argv[2] if len(sys.argv) > 2 else "stream",
    ))
