#!/usr/bin/env python3
"""
Splits qlog packet_lost events into genuine network loss vs. spurious loss
(reordering that tripped RFC 9002's packet_threshold).

A `packet_lost` event only records the detector's guess at declaration time.
A packet declared lost that is covered by a *later* `packets_acked` event on
the same path did in fact arrive - it was reordered, not dropped. This
mirrors the logic n0q's own `detect_spurious_loss` applies internally.

Requires qlogs captured with path_id on packet_lost and the packets_acked
event emitted (n0q noq-proto, see TODO.md).
"""

import sys
import os
import glob
import json
from collections import defaultdict


def analyze_file(fpath):
    """Returns (per_path_stats, unknown_path_losses).

    per_path_stats maps path_id -> dict with lost/spurious/genuine counts and
    a trigger breakdown.
    """
    # path_id -> {pn: [times]} for packets declared lost, and acked pns seen after
    lost_events = defaultdict(list)   # path_id -> [(time, pn, trigger)]
    acked = defaultdict(lambda: defaultdict(list))  # path_id -> pn -> [times]
    unknown_path = 0

    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            name = ev.get("name")
            data = ev.get("data") or {}
            time = ev.get("time", 0.0)

            if name == "quic:packet_lost":
                header = data.get("header") or {}
                pn = header.get("packet_number")
                if pn is None:
                    continue
                path_id = header.get("path_id")
                if path_id is None:
                    path_id = _path_from_tuple(ev)
                if path_id is None:
                    unknown_path += 1
                    continue
                lost_events[path_id].append((time, pn, data.get("trigger", "unknown")))

            elif name == "quic:packets_acked":
                path_id = _path_from_tuple(ev)
                if path_id is None:
                    continue
                for pn in data.get("packet_numbers") or []:
                    acked[path_id][pn].append(time)

    stats = {}
    for path_id, events in sorted(lost_events.items()):
        spurious = 0
        genuine = 0
        triggers = defaultdict(int)
        spurious_by_trigger = defaultdict(int)
        for time, pn, trigger in events:
            triggers[trigger] += 1
            # spurious only if acked strictly AFTER the loss declaration
            if any(t > time for t in acked[path_id].get(pn, ())):
                spurious += 1
                spurious_by_trigger[trigger] += 1
            else:
                genuine += 1
        stats[path_id] = {
            "lost": len(events),
            "spurious": spurious,
            "genuine": genuine,
            "triggers": dict(triggers),
            "spurious_by_trigger": dict(spurious_by_trigger),
        }
    return stats, unknown_path


def _path_from_tuple(ev):
    """qlog tuple ids are formatted 'p<path_id>' by n0q."""
    tuple_id = ev.get("tuple")
    if isinstance(tuple_id, str) and tuple_id.startswith("p"):
        try:
            return int(tuple_id[1:])
        except ValueError:
            return None
    return None


def main(qlog_dir="logs/qlog", side="server"):
    if not os.path.isdir(qlog_dir):
        print(f"Error: Directory {qlog_dir} not found.")
        return 1

    pattern = os.path.join(qlog_dir, f"*-{side}.qlog")
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not files:
        print(f"No {side}-side .qlog files found in {qlog_dir}.")
        return 1

    print("=" * 100)
    print(f"Genuine vs. spurious loss ({side}-side qlogs)")
    print("=" * 100)

    any_data = False
    for fpath in files:
        stats, unknown = analyze_file(fpath)
        print(f"\n{os.path.basename(fpath)}")
        if unknown:
            print(f"  !! {unknown} packet_lost events had no path_id/tuple "
                  f"(qlog predates the path_id fix) - skipped")
        if not stats:
            print("  (no attributable packet_lost events)")
            continue
        any_data = True
        print(f"  {'path':<6} {'lost':>8} {'genuine':>9} {'spurious':>9} {'spur%':>7}   triggers")
        for path_id, s in stats.items():
            pct = (s["spurious"] / s["lost"] * 100) if s["lost"] else 0.0
            trig = ", ".join(f"{k}={v}" for k, v in sorted(s["triggers"].items()))
            print(f"  {path_id:<6} {s['lost']:>8} {s['genuine']:>9} "
                  f"{s['spurious']:>9} {pct:>6.1f}%   {trig}")

        total_lost = sum(s["lost"] for s in stats.values())
        total_spur = sum(s["spurious"] for s in stats.values())
        pct = (total_spur / total_lost * 100) if total_lost else 0.0
        print(f"  {'ALL':<6} {total_lost:>8} {total_lost - total_spur:>9} "
              f"{total_spur:>9} {pct:>6.1f}%")

    print()
    if not any_data:
        print("No usable data. If every file reported missing path_id, the qlogs")
        print("were captured before the instrumentation fix - re-run the sweep.")
        return 1
    return 0


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "logs/qlog"
    s = sys.argv[2] if len(sys.argv) > 2 else "server"
    sys.exit(main(d, s))
