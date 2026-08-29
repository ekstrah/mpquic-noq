# Multipath QUIC (`n0q`) Session Memory & State

**Last Updated**: 2026-08-29
**Workspace Root**: `/home/dongho/Desktop/git/mpquic-noq`

---

## 1. Network Testbed Architecture

| Entity | Role / Function | Configuration |
| :--- | :--- | :--- |
| **Server** | QUIC Server Endpoint | `10.99.0.1/32` on `lo`, listening on UDP port `4433` |
| **Client** | Multipath QUIC Client | Source-based policy routing (Tables 101, 102, 103) |
| **Link A (LEO Satellite)** | High-Bandwidth / Jittery | `172.16.1.0/30`, 62M down / 18M up, 25ms delay, ±13ms jitter, 0.17% loss |
| **Link B (Cellular/Mobile)**| Moderate Latency / Stable | `172.16.2.0/30`, 30M symmetric, 50ms delay, ±5ms jitter, 0.006% loss |
| **Link C (Mesh Radio)** | Low Latency / Pristine | `172.16.3.0/30`, 30M symmetric, 2ms delay, ±1ms jitter, 0.0% loss |

---

## 2. Completed Experiments & Datasets

### A. Stream Mode Sweep (12 Combinations)
- **Summary File**: `logs/sweep_stream_summary.csv`
- **Stdout Logs**: `logs/stdout/client_stream_*.log` and `server_stream_*.log`
- **Key Metrics**:
  - Total Data Delivered: **71.9 MB to 109.3 MB** per 15 s run (**21.5 – 32.9 Mbps**).
  - Path Distribution: Link C (Mesh) carried 37–67%, Link B carried 25–53%, Link A carried 6–13%.
  - Top Performers: `REDUNDANT + COPA` (32.87 Mbps), `MINRTT + BBR3` (30.31 Mbps).

### B. Datagram Mode Sweep (12 Combinations)
- **Summary File**: `logs/sweep_datagram_summary.csv`
- **Stdout Logs**: `logs/stdout/client_datagram_*.log` and `server_datagram_*.log`
- **Key Metrics**:
  - Total Datagrams Sent: **175 to 985** (1200 B payload).
  - Total Datagrams Echoed: **18 to 260** (**0.01 – 0.17 Mbps**).
  - Path Distribution: Link A (primary path) carried 38–81% due to polling priority.
  - Top Performers: `MINRTT + CUBIC` (0.17 Mbps, 260 dgrams echoed), `MINRTT + BBR3` (67.5% echo loss, lowest loss).

### C. CID Lifecycle & Protocol Reliability
- **QLOG Directory**: `logs/qlog/`
- **Analyzer Script**: `scripts/analyze-qlog-cids.py`
- **Results**:
  - `0` Disconnections, `0` Connection Close Errors, `0` Path Abandons.
  - 100% Path Validation on all 3 subflows.
  - Clean handshake CID retirement (`RETIRE_CONNECTION_ID`), resolving the bugs previously observed in `picoquic`.

---

## 3. Important File Locations & Environment

- **Topology Script**: `/home/dongho/Desktop/git/mpquic-noq/mininet_topo.py`
- **Setup Script**: `/home/dongho/Desktop/git/mpquic-noq/scripts/setup-node.sh`
- **CID Analyzer**: `/home/dongho/Desktop/git/mpquic-noq/scripts/analyze-qlog-cids.py`
- **Complete Report**: `/home/dongho/Desktop/git/mpquic-noq/EXPERIMENT_REPORT.md`
- **n0q Repository**: `/home/dongho/Desktop/git/n0q`
- **n0q Server Bin**: `/home/dongho/Desktop/git/n0q/target/release/n0q-server`
- **n0q Client Bin**: `/home/dongho/Desktop/git/n0q/target/release/n0q-client`
- **Picoquic Demo**: `/home/dongho/Desktop/git/mpquic-test/picoquic/picoquicdemo`
