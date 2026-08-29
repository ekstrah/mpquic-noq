# Project Context & Persistent Memory: MP-QUIC (`n0q`) Ground Testbed

## Overview
This repository contains the Mininet emulation and evaluation framework for **Multipath QUIC (`n0q`)** and **Picoquic** over a 3-link ground testbed.

## Network Topology & Shaping Parameters
- **Link A (LEO Satellite)**: `server-eth0` (172.16.1.1/30) <-> `client-eth0` (172.16.1.2/30), Table 101.
  - Shaping: 62 Mbps down / 18 Mbps up, 25 ms delay, ±13 ms jitter, 0.17% loss.
- **Link B (Mobile/Cellular)**: `server-eth1` (172.16.2.1/30) <-> `client-eth1` (172.16.2.2/30), Table 102.
  - Shaping: 30 Mbps symmetric, 50 ms delay, ±5 ms jitter, 0.006% loss.
- **Link C (Mesh Radio)**: `server-eth2` (172.16.3.1/30) <-> `client-eth2` (172.16.3.2/30), Table 103.
  - Shaping: 30 Mbps symmetric, 2 ms delay, ±1 ms jitter, 0.0% loss.
- **Server Canonical IP**: `10.99.0.1/32` on `lo` interface.

## Key Binaries & Repositories
- **n0q source**: `../n0q`
- **n0q binaries**: `../n0q/target/release/n0q-server`, `../n0q/target/release/n0q-client`
- **Picoquic source**: `../mpquic-test/picoquic/picoquicdemo`
- **Topology script**: `mininet_topo.py`
- **CID Analyzer**: `scripts/analyze-qlog-cids.py`

## Experimental Results Summary
- **Stream Mode**: 12 combinations tested. Throughput ~21.5–32.9 Mbps. Link C (2ms) & Link B carried ~85–93% of bulk traffic. Top configs: `REDUNDANT + COPA` and `MINRTT + BBR3`.
- **Datagram Mode**: 12 combinations tested. Link A (primary path) carried 38–81% of traffic due to initial queue evaluation. Top configs: `MINRTT + CUBIC` (highest rate: 0.17 Mbps, 260 dgrams echoed) and `MINRTT + BBR3` (lowest loss: 67.5%).
- **Protocol Stability**: 0 `CONNECTION_CLOSE` errors, 0 `PATH_ABANDON`, 100% path validation in `n0q` (resolved `picoquic` CID retirement issues).
- Full detailed report: `EXPERIMENT_REPORT.md`

## Common Commands
- Run Stream Sweep: `sudo python3 mininet_topo.py --sweep stream`
- Run Datagram Sweep: `sudo python3 mininet_topo.py --sweep datagram`
- Analyze CID lifecycle: `python3 scripts/analyze-qlog-cids.py logs/qlog`
- Interactive Mininet: `sudo python3 mininet_topo.py --cli`
