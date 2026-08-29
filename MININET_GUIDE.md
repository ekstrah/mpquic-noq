# MP-QUIC 3-Link Mininet Ground Testbed Guide

This setup emulates the 3-link Multi-Path QUIC testbed inside **Mininet**, matching the physical testbed configuration from `env.sh` and the source UAV paper (*Baltaci et al., IEEE Access 2023*).

---

## 1. Network Topology & Emulation Profiles

| Link | Name | Subnet | Server IP (`iface`) | Client IP (`iface`) | Egress Shaping | Delay & Jitter | Loss |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Link A** | LEO Satellite | `172.16.1.0/30` | `172.16.1.1` (`server-eth0`) | `172.16.1.2` (`client-eth0`) | Down: 62 Mbps<br>Up: 18 Mbps | 25 ms ± 13 ms | 0.17% |
| **Link B** | Mobile / LTE | `172.16.2.0/30` | `172.16.2.1` (`server-eth1`) | `172.16.2.2` (`client-eth1`) | 30 Mbps (symmetric) | 50 ms ± 5 ms | 0.006% |
| **Link C** | Wireless Mesh | `172.16.3.0/30` | `172.16.3.1` (`server-eth2`) | `172.16.3.2` (`client-eth2`) | 30 Mbps (symmetric) | 2 ms ± 1 ms | 0.0% |

### Key Configuration
- **Server Canonical IP**: `10.99.0.1/32` configured on `lo`.
- **Server rp_filter**: Relaxed (`net.ipv4.conf.*.rp_filter = 2`) to accept multipath inbound traffic from all three physical interfaces.
- **Client Policy Routing**: Source routing with `ip rule` and tables `101`, `102`, `103` directing packets sourced from `172.16.1.2`, `172.16.2.2`, and `172.16.3.2` to the respective next-hop gateway on `server`.

---

## 2. Usage & Running Options

All Mininet commands require `sudo`.

### Option A: Interactive Mininet CLI (Recommended for Development)
```bash
sudo python3 mininet_topo.py
```
This builds the topology, applies traffic shaping, runs connectivity verification, and drops into the interactive Mininet CLI (`mininet>`):

```bash
# Open interactive XTerm terminals for both client and server:
mininet> xterm client server

# Ping server's canonical IP via specific links (policy routing):
mininet> client ping -c 3 -I 172.16.1.2 10.99.0.1   # Via Link A (LEO)
mininet> client ping -c 3 -I 172.16.2.2 10.99.0.1   # Via Link B (Mobile)
mininet> client ping -c 3 -I 172.16.3.2 10.99.0.1   # Via Link C (Mesh)

# Check real-time per-link interface statistics:
mininet> client ip -s link show client-eth0
mininet> client ip -s link show client-eth1
mininet> client ip -s link show client-eth2

# Exit and clean up:
mininet> exit
```

---

### Option B: Quick Connectivity & Policy Routing Test
To verify the virtual links and policy routing without opening the CLI:
```bash
sudo python3 mininet_topo.py --test
```

---

### Option C: Automated N0Q Scheduler × Congestion Control Sweep
Runs the full sweep across all combinations of schedulers (`MINRTT`, `REDUNDANT`, `ROUNDROBIN`) and congestion controllers (`BBR`, `BBR3`, `CUBIC`, `COPA`):
```bash
# Stream mode
sudo python3 mininet_topo.py --sweep stream

# Datagram mode
sudo python3 mininet_topo.py --sweep datagram
```
- Logs are automatically saved to `logs/stdout/` and `logs/qlog/`.

---

### Option D: Automated Picoquic Sweep
Runs the multipath sweep for Picoquic (`bbr`, `cubic`, `fast`, `newreno`):
```bash
sudo python3 mininet_topo.py --picoquic-sweep
```
- Outputs per-link RX byte distribution and goodput to `results_picoquic/sweep.csv`.

---

## 3. Running Existing Shell Scripts in Mininet Nodes

If you prefer to run the bash scripts (`run-server-sweep.sh`, `run-client-sweep.sh`, etc.) inside Mininet nodes manually:

1. Launch Mininet:
   ```bash
   sudo python3 mininet_topo.py
   ```
2. In the `mininet>` prompt, start xterms:
   ```bash
   mininet> xterm server client
   ```
3. In the **Server** terminal:
   ```bash
   source env.mininet.sh
   ./scripts/run-server-sweep.sh
   ```
4. In the **Client** terminal:
   ```bash
   source env.mininet.sh
   ./scripts/run-client-sweep.sh
   ```
