#!/usr/bin/env python3
"""
Mininet Topology for MP-QUIC (3-Link Ground Testbed)
Replicates the physical testbed with 3 point-to-point links:
  - Link A ("LEO satellite"): 62M down / 18M up, 25ms delay, 13ms jitter, 0.17% loss
  - Link B ("Mobile/cellular"): 30M symmetric, 50ms delay, 5ms jitter, 0.006% loss
  - Link C ("Mesh"): 30M symmetric, 2ms delay, 1ms jitter, 0.0% loss

Server canonical IP: 10.99.0.1/32 on loopback
Client source-based policy routing (tables 101, 102, 103)
"""

import sys
import os
import time
import argparse
import subprocess
from mininet.net import Mininet
from mininet.cli import CLI
from mininet.log import setLogLevel, info, error, warn

# Default Configuration Matching env.sh
CONFIG = {
    "server_canonical_ip": "10.99.0.1",
    "quic_port": 4433,
    "links": {
        "A": {
            "name": "LEO Satellite",
            "server_iface": "server-eth0",
            "client_iface": "client-eth0",
            "server_ip": "172.16.1.1/30",
            "client_ip": "172.16.1.2/30",
            "server_ip_raw": "172.16.1.1",
            "client_ip_raw": "172.16.1.2",
            "table": 101,
            # Shaping
            "server_rate_mbit": 62,
            "client_rate_mbit": 18,
            "delay_ms": 25,
            "jitter_ms": 13,
            "loss_pct": 0.17,
        },
        "B": {
            "name": "Mobile / Cellular",
            "server_iface": "server-eth1",
            "client_iface": "client-eth1",
            "server_ip": "172.16.2.1/30",
            "client_ip": "172.16.2.2/30",
            "server_ip_raw": "172.16.2.1",
            "client_ip_raw": "172.16.2.2",
            "table": 102,
            # Shaping
            "server_rate_mbit": 30,
            "client_rate_mbit": 30,
            "delay_ms": 50,
            "jitter_ms": 5,
            "loss_pct": 0.006,
        },
        "C": {
            "name": "Mesh Radio",
            "server_iface": "server-eth2",
            "client_iface": "client-eth2",
            "server_ip": "172.16.3.1/30",
            "client_ip": "172.16.3.2/30",
            "server_ip_raw": "172.16.3.1",
            "client_ip_raw": "172.16.3.2",
            "table": 103,
            # Shaping
            "server_rate_mbit": 30,
            "client_rate_mbit": 30,
            "delay_ms": 2,
            "jitter_ms": 1,
            "loss_pct": 0.0,
        },
    },
    # Scheduler collapsed to a single label: MINRTT/REDUNDANT/ROUNDROBIN were
    # confirmed to run the exact same path-selection code (no scheduler is
    # actually implemented in noq-proto - see TODO.md / docs/), so sweeping
    # all three was tripling run count for zero additional information.
    # Reinvesting that 3x budget into repeats/thresholds instead.
    "sweep_combos": [
        ("MINRTT", "BBR"),
        ("MINRTT", "CUBIC"),
        ("MINRTT", "NEWRENO"),
    ],
    # RFC 9002 kPacketThreshold values to sweep. 3 is the spec default.
    # Small first pass: 4 representative points (default, mid, high, and the
    # BBR pt=25-40 ceiling/reversal region from the earlier full 3-40 sweep)
    # rather than the full 8-point grid, to validate the repeat mechanism and
    # the datagram harness fix before committing to a much longer run.
    "sweep_packet_thresholds": [3, 10, 20, 40],
    # Number of times to repeat each (scheduler, cc, packet_threshold) combo.
    # Repeats are the outer-outer loop (before packet_threshold), so an
    # interrupted run still yields whole completed repeat blocks.
    "sweep_repeats": 2,
    "picoquic_cc_list": ["bbr", "cubic", "fast", "newreno"],
    "sweep_window_sec": 25,
    "sweep_client_delay_sec": 5,
    "sweep_client_duration_sec": 15,
}


def build_mpquic_network():
    """Builds and initializes the Mininet network."""
    info("*** Creating Mininet Network for MP-QUIC...\n")
    net = Mininet(topo=None, build=False)

    info("*** Adding Client and Server hosts...\n")
    client = net.addHost("client", ip=None)
    server = net.addHost("server", ip=None)

    info("*** Creating 3 Point-to-Point Links (LEO, Mobile, Mesh)...\n")
    for key, lconf in CONFIG["links"].items():
        net.addLink(
            client,
            server,
            intfName1=lconf["client_iface"],
            intfName2=lconf["server_iface"],
        )

    info("*** Starting Network...\n")
    net.build()

    info("*** Configuring Server Addressing and Routing...\n")
    # Enable forwarding and relax reverse path filtering for multi-path inbound traffic
    server.cmd("sysctl -w net.ipv4.ip_forward=1")
    server.cmd("sysctl -w net.ipv4.conf.all.rp_filter=2")
    server.cmd("sysctl -w net.ipv4.conf.default.rp_filter=2")
    server.cmd("sysctl -w net.ipv4.conf.lo.rp_filter=2")

    # Add canonical loopback address
    server.cmd(f"ip addr replace {CONFIG['server_canonical_ip']}/32 dev lo")

    for key, lconf in CONFIG["links"].items():
        iface = lconf["server_iface"]
        ip = lconf["server_ip"]
        server.cmd(f"ip addr replace {ip} dev {iface}")
        server.cmd(f"ip link set {iface} up")
        server.cmd(f"sysctl -w net.ipv4.conf.{iface}.rp_filter=2")

    info("*** Configuring Client Addressing and Policy Routing...\n")
    client.cmd("sysctl -w net.ipv4.ip_forward=1")
    client.cmd("sysctl -w net.ipv4.conf.all.rp_filter=2")
    client.cmd("sysctl -w net.ipv4.conf.default.rp_filter=2")

    for key, lconf in CONFIG["links"].items():
        iface = lconf["client_iface"]
        ip = lconf["client_ip"]
        raw_client_ip = lconf["client_ip_raw"]
        raw_server_ip = lconf["server_ip_raw"]
        table = lconf["table"]

        client.cmd(f"ip addr replace {ip} dev {iface}")
        client.cmd(f"ip link set {iface} up")
        client.cmd(f"sysctl -w net.ipv4.conf.{iface}.rp_filter=2")

        # Source-based policy routing per interface
        client.cmd(f"ip rule del from {raw_client_ip} table {table} 2>/dev/null || true")
        client.cmd(f"ip rule add from {raw_client_ip} table {table}")
        client.cmd(
            f"ip route replace {CONFIG['server_canonical_ip']}/32 via {raw_server_ip} dev {iface} table {table}"
        )

    # Default route for un-bound client traffic to Link A
    link_a = CONFIG["links"]["A"]
    client.cmd(
        f"ip route replace {CONFIG['server_canonical_ip']}/32 via {link_a['server_ip_raw']} dev {link_a['client_iface']}"
    )

    info("*** Applying Traffic Shaping (TBF + Netem)...\n")
    apply_traffic_shaping(client, server)

    return net, client, server


def apply_traffic_shaping(client, server):
    """Applies traffic control (tc) rate limiting, delay, jitter, and packet loss."""
    for key, lconf in CONFIG["links"].items():
        s_iface = lconf["server_iface"]
        c_iface = lconf["client_iface"]

        s_rate = lconf["server_rate_mbit"]
        c_rate = lconf["client_rate_mbit"]
        delay = lconf["delay_ms"]
        jitter = lconf["jitter_ms"]
        loss = lconf["loss_pct"]

        # Server-side shaping (downlink egress)
        server.cmd(f"tc qdisc del dev {s_iface} root 2>/dev/null || true")
        server.cmd(
            f"tc qdisc add dev {s_iface} root handle 1: tbf rate {s_rate}mbit burst 32kbit latency 400ms"
        )
        server.cmd(
            f"tc qdisc add dev {s_iface} parent 1: handle 10: netem delay {delay}ms {jitter}ms loss {loss}%"
        )

        # Client-side shaping (uplink egress)
        client.cmd(f"tc qdisc del dev {c_iface} root 2>/dev/null || true")
        client.cmd(
            f"tc qdisc add dev {c_iface} root handle 1: tbf rate {c_rate}mbit burst 32kbit latency 400ms"
        )
        client.cmd(
            f"tc qdisc add dev {c_iface} parent 1: handle 10: netem delay {delay}ms {jitter}ms loss {loss}%"
        )

    info("Traffic shaping applied to all interfaces.\n")


def test_connectivity(client, server):
    """Verifies direct link connectivity and policy routing to server canonical IP."""
    info("\n===========================================\n")
    info("       Verifying Link Connectivity         \n")
    info("===========================================\n")

    all_passed = True
    for key, lconf in CONFIG["links"].items():
        name = lconf["name"]
        c_ip = lconf["client_ip_raw"]
        s_ip = lconf["server_ip_raw"]

        # Test direct P2P ping
        res = client.cmd(f"ping -c 3 -W 2 -I {c_ip} {s_ip}")
        if "0% packet loss" in res or ("packets transmitted, " in res and "received" in res and "0 received" not in res):
            info(f"[PASS] Link {key} ({name}): Direct P2P ping {c_ip} -> {s_ip} OK\n")
        else:
            error(f"[FAIL] Link {key} ({name}): Direct P2P ping failed!\n{res}\n")
            all_passed = False

        # Test policy routed ping to server canonical IP (10.99.0.1)
        res_canon = client.cmd(f"ping -c 3 -W 2 -I {c_ip} {CONFIG['server_canonical_ip']}")
        if "0% packet loss" in res_canon or ("packets transmitted, " in res_canon and "received" in res_canon and "0 received" not in res_canon):
            info(
                f"[PASS] Link {key} ({name}): Routed ping {c_ip} -> {CONFIG['server_canonical_ip']} OK\n"
            )
        else:
            error(
                f"[FAIL] Link {key} ({name}): Routed ping to canonical IP failed!\n{res_canon}\n"
            )
            all_passed = False

    info("===========================================\n\n")
    return all_passed


def run_automated_sweep(client, server, mode="stream"):
    """
    Executes the automated scheduler x CC sweep inside the Mininet topology for n0q.
    Runs n0q-server on server host and n0q-client on client host.
    Logs per-run stdout, QLOGs, and writes a summary CSV with per-link RX/TX byte distributions.
    """
    repo_dir = os.path.abspath(os.path.dirname(__file__))
    n0q_dir = os.path.abspath(os.path.join(repo_dir, "../n0q"))
    n0q_manifest = os.path.join(n0q_dir, "Cargo.toml")
    n0q_server_bin = os.path.join(n0q_dir, "target/release/n0q-server")
    n0q_client_bin = os.path.join(n0q_dir, "target/release/n0q-client")

    # Discover cargo path across possible user and root directories
    candidate_cargo_dirs = [
        "/home/dongho/.cargo/bin",
        "/root/.cargo/bin",
        os.path.expanduser("~/.cargo/bin"),
    ]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        candidate_cargo_dirs.insert(0, f"/home/{sudo_user}/.cargo/bin")

    cargo_path_str = ":".join(d for d in candidate_cargo_dirs if os.path.isdir(d))
    cargo_env_path = f'export PATH="{cargo_path_str}:$PATH"; ' if cargo_path_str else ""

    use_direct_bin = os.path.isfile(n0q_server_bin) and os.path.isfile(n0q_client_bin)

    if not use_direct_bin:
        if not os.path.isfile(n0q_manifest):
            error(f"\n[ERROR] n0q Cargo.toml not found at: {n0q_manifest}\n")
            error("Please run ./scripts/setup-node.sh to install Rust and clone/build n0q, or make sure the n0q repo is located at ../n0q\n\n")
            return

        cargo_check = server.cmd(f"bash -c \"{cargo_env_path} command -v cargo\"").strip()
        if not cargo_check:
            error("\n[ERROR] Rust 'cargo' command not found in PATH or ~/.cargo/bin!\n")
            error("Please install Rust via 'curl --proto =https --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y' or run ./scripts/setup-node.sh\n\n")
            return

    logs_dir = os.path.join(repo_dir, "logs")
    logs_stdout = os.path.join(logs_dir, "stdout")
    logs_qlog = os.path.join(logs_dir, "qlog")
    os.makedirs(logs_stdout, exist_ok=True)
    os.makedirs(logs_qlog, exist_ok=True)

    csv_file = os.path.join(logs_dir, f"sweep_{mode}_summary.csv")
    with open(csv_file, "w") as f:
        f.write("scheduler,cc,packet_threshold,repeat,a_rx_bytes,b_rx_bytes,c_rx_bytes,a_share_pct,b_share_pct,c_share_pct,total_rx_bytes,exit_code\n")

    info(f"\n*** Starting Automated N0Q Sweep (Mode: {mode.upper()}) ***\n")
    info(f"Summary CSV: {csv_file}\n")
    info(f"Stdout logs: {logs_stdout}\n")
    info(f"Qlogs:       {logs_qlog}\n\n")

    def get_rx_bytes(node, iface):
        out = node.cmd(f"cat /sys/class/net/{iface}/statistics/rx_bytes").strip()
        try:
            return int(out)
        except ValueError:
            return 0

    datagram_flag = "--datagram" if mode == "datagram" else ""
    canonical_ip = CONFIG["server_canonical_ip"]
    port = CONFIG["quic_port"]
    bind_a = CONFIG["links"]["A"]["client_ip_raw"]
    bind_b = CONFIG["links"]["B"]["client_ip_raw"]
    bind_c = CONFIG["links"]["C"]["client_ip_raw"]

    def port_is_free():
        out = server.cmd(f"ss -uln sport = :{port} 2>/dev/null").strip()
        return "UNCONN" not in out

    def wait_for_port_free(max_wait_sec=5.0, interval_sec=0.5):
        waited = 0.0
        while waited < max_wait_sec:
            if port_is_free():
                return True
            time.sleep(interval_sec)
            waited += interval_sec
        return port_is_free()

    # Guard against stale n0q-server processes left over from a previous
    # (e.g. interrupted) run occupying the port before the sweep even starts.
    server.cmd("pkill -9 -f n0q-server 2>/dev/null || true")
    if not wait_for_port_free():
        warn(f"Port {port} still in use before sweep start; a stray process may not have been killed.\n")

    repeats = CONFIG["sweep_repeats"]
    sweep_grid = [
        (rep, scheduler, cc, pt)
        for rep in range(1, repeats + 1)
        for pt in CONFIG["sweep_packet_thresholds"]
        for scheduler, cc in CONFIG["sweep_combos"]
    ]
    total_runs = len(sweep_grid)
    est_min = total_runs * CONFIG["sweep_window_sec"] / 60.0
    info(f"Sweep grid: {total_runs} runs "
         f"({len(CONFIG['sweep_combos'])} combos x "
         f"{len(CONFIG['sweep_packet_thresholds'])} packet thresholds x "
         f"{repeats} repeats), ~{est_min:.0f} min\n\n")

    for run_idx, (rep, scheduler, cc, packet_threshold) in enumerate(sweep_grid, start=1):
        info("============================================================\n")
        info(f"[{run_idx}/{total_runs}] Scheduler={scheduler}, CC={cc}, "
             f"packet_threshold={packet_threshold}, repeat={rep}/{repeats}\n")
        info("============================================================\n")

        tag = f"{scheduler}_{cc}_pt{packet_threshold}_r{rep}"
        s_log = os.path.join(logs_stdout, f"server_{mode}_{tag}.log")
        c_log = os.path.join(logs_stdout, f"client_{mode}_{tag}.log")

        # Start Server in background
        if use_direct_bin:
            server_cmd = (
                f"export QLOGDIR='{logs_qlog}'; "
                f"{n0q_server_bin} --listen {canonical_ip}:{port} --multipath {datagram_flag} --cc {cc} "
                f"--packet-threshold {packet_threshold} "
                f"> '{s_log}' 2>&1 & echo $!"
            )
        else:
            server_cmd = (
                f"{cargo_env_path}"
                f"export QLOGDIR='{logs_qlog}'; "
                f"cargo run --release --manifest-path {n0q_manifest} --bin n0q-server -- "
                f"--listen {canonical_ip}:{port} --multipath {datagram_flag} --cc {cc} "
                f"--packet-threshold {packet_threshold} "
                f"> '{s_log}' 2>&1 & echo $!"
            )
        server_pid_out = server.cmd(f"bash -c \"{server_cmd}\"")
        parts = server_pid_out.strip().split()
        server_pid = parts[-1] if parts and parts[-1].isdigit() else None
        info(f"Server started (PID: {server_pid}), waiting {CONFIG['sweep_client_delay_sec']}s...\n")
        time.sleep(CONFIG["sweep_client_delay_sec"])

        # Capture interface byte counters before client run
        a0 = get_rx_bytes(client, "client-eth0")
        b0 = get_rx_bytes(client, "client-eth1")
        c0 = get_rx_bytes(client, "client-eth2")

        # Start Client
        if use_direct_bin:
            client_cmd = (
                f"export QLOGDIR='{logs_qlog}'; "
                f"{n0q_client_bin} --bind {bind_a} --bind {bind_b} --bind {bind_c} "
                f"--multipath {datagram_flag} --scheduler {scheduler} --cc {cc} "
                f"--packet-threshold {packet_threshold} "
                f"--duration {CONFIG['sweep_client_duration_sec']} "
                f"https://{canonical_ip}:{port}/video > '{c_log}' 2>&1"
            )
        else:
            client_cmd = (
                f"{cargo_env_path}"
                f"export QLOGDIR='{logs_qlog}'; "
                f"cargo run --release --manifest-path {n0q_manifest} --bin n0q-client -- "
                f"--bind {bind_a} --bind {bind_b} --bind {bind_c} "
                f"--multipath {datagram_flag} --scheduler {scheduler} --cc {cc} "
                f"--packet-threshold {packet_threshold} "
                f"--duration {CONFIG['sweep_client_duration_sec']} "
                f"https://{canonical_ip}:{port}/video > '{c_log}' 2>&1"
            )
        info(f"Client executing for {CONFIG['sweep_client_duration_sec']}s...\n")
        raw_exit = client.cmd(f"bash -c \"{client_cmd}; echo $?\"").strip().split()
        exit_code_str = raw_exit[-1] if raw_exit else "1"
        exit_code = int(exit_code_str) if exit_code_str.isdigit() else 1

        # Capture interface byte counters after client run
        a1 = get_rx_bytes(client, "client-eth0")
        b1 = get_rx_bytes(client, "client-eth1")
        c1 = get_rx_bytes(client, "client-eth2")

        da = max(0, a1 - a0)
        db = max(0, b1 - b0)
        dc = max(0, c1 - c0)
        total = da + db + dc

        pa = f"{(100.0 * da / total):.1f}" if total > 0 else "0.0"
        pb = f"{(100.0 * db / total):.1f}" if total > 0 else "0.0"
        pc = f"{(100.0 * dc / total):.1f}" if total > 0 else "0.0"

        row = f"{scheduler},{cc},{packet_threshold},{rep},{da},{db},{dc},{pa},{pb},{pc},{total},{exit_code}"
        with open(csv_file, "a") as f:
            f.write(row + "\n")

        info(f"Combo Result: Link A={pa}% ({da} B), Link B={pb}% ({db} B), Link C={pc}% ({dc} B) [Exit: {exit_code}]\n")

        # Clean up Server for this combo. Kill by PID and by name, then
        # actively verify the port was released -- a plain "kill" (SIGTERM)
        # or a single pkill attempt is not guaranteed to have taken effect
        # by the time the next combo tries to bind the same port.
        if server_pid:
            server.cmd(f"kill -9 {server_pid} 2>/dev/null || true")
        server.cmd("pkill -9 -f n0q-server 2>/dev/null || true")
        if not wait_for_port_free():
            warn(
                f"Port {port} still in use after killing server (PID {server_pid}) "
                f"for Scheduler={scheduler}, CC={cc}, pt={packet_threshold}, rep={rep}. "
                f"Next combo's server may fail to bind.\n"
            )

        rem_time = (
            CONFIG["sweep_window_sec"]
            - CONFIG["sweep_client_delay_sec"]
            - CONFIG["sweep_client_duration_sec"]
        )
        if rem_time > 0:
            time.sleep(rem_time)

    info(f"\n*** Automated Sweep Complete! Summary saved to {csv_file} ***\n")


def run_picoquic_sweep(client, server):
    """
    Executes the automated sweep for Picoquic inside the Mininet topology.
    """
    repo_dir = os.path.abspath(os.path.dirname(__file__))
    picoquic_dir = os.path.abspath(os.path.join(repo_dir, "../mpquic-test/picoquic"))
    www_dir = os.path.abspath(os.path.join(repo_dir, "../mpquic-test/www"))
    results_dir = os.path.join(repo_dir, "results_picoquic")
    qlogs_dir = os.path.join(results_dir, "qlogs")
    os.makedirs(qlogs_dir, exist_ok=True)

    if not os.path.isfile(os.path.join(picoquic_dir, "picoquicdemo")):
        error(f"picoquicdemo not found in {picoquic_dir}! Please build picoquic first.\n")
        return

    csv_file = os.path.join(results_dir, "sweep.csv")
    with open(csv_file, "w") as f:
        f.write("cc,a_rx_bytes,b_rx_bytes,c_rx_bytes,a_share_pct,b_share_pct,c_share_pct,elapsed_s,mbps,exit_code\n")

    info("\n*** Starting Automated Picoquic Sweep ***\n")

    def get_rx_bytes(node, iface):
        out = node.cmd(f"cat /sys/class/net/{iface}/statistics/rx_bytes").strip()
        try:
            return int(out)
        except ValueError:
            return 0

    idx_b = client.cmd("ip -o link show client-eth1 | cut -d: -f1 | tr -d ' '").strip()
    idx_c = client.cmd("ip -o link show client-eth2 | cut -d: -f1 | tr -d ' '").strip()

    for cc in CONFIG["picoquic_cc_list"]:
        info(f"=== CC={cc} ===\n")
        log_file = os.path.join(results_dir, f"run_{cc}.log")
        qlog_cc_dir = os.path.join(qlogs_dir, cc)
        os.makedirs(qlog_cc_dir, exist_ok=True)

        # Start Picoquic Server (exits after 1 connection via -1)
        s_cmd = (
            f"cd {picoquic_dir} && exec ./picoquicdemo -w {www_dir} -p {CONFIG['quic_port']} "
            f"-M -q '{qlog_cc_dir}' -G '{cc}' -1 > '{results_dir}/server_{cc}.log' 2>&1 &"
        )
        server.cmd(f"bash -c \"{s_cmd}\"")
        time.sleep(1)

        # Record initial bytes
        a0 = get_rx_bytes(client, "client-eth0")
        b0 = get_rx_bytes(client, "client-eth1")
        c0 = get_rx_bytes(client, "client-eth2")

        # Run Client
        c_cmd = (
            f"cd {picoquic_dir} && ./picoquicdemo -M -n test.example.com -q '{qlog_cc_dir}' "
            f"-A '172.16.2.2/{idx_b},172.16.3.2/{idx_c}' {CONFIG['server_canonical_ip']} {CONFIG['quic_port']} "
            f"/testfile.bin > '{log_file}' 2>&1"
        )
        raw_exit = client.cmd(f"bash -c \"{c_cmd}; echo $?\"").strip().split()
        exit_code_str = raw_exit[-1] if raw_exit else "1"
        exit_code = int(exit_code_str) if exit_code_str.isdigit() else 1

        # Record delta bytes
        a1 = get_rx_bytes(client, "client-eth0")
        b1 = get_rx_bytes(client, "client-eth1")
        c1 = get_rx_bytes(client, "client-eth2")
        da = max(0, a1 - a0)
        db = max(0, b1 - b0)
        dc = max(0, c1 - c0)
        total = da + db + dc

        pa = f"{(100.0 * da / total):.1f}" if total > 0 else "0.0"
        pb = f"{(100.0 * db / total):.1f}" if total > 0 else "0.0"
        pc = f"{(100.0 * dc / total):.1f}" if total > 0 else "0.0"

        # Parse log for mbps and elapsed
        elapsed = ""
        mbps = ""
        if os.path.exists(log_file):
            with open(log_file) as lf:
                content = lf.read()
                import re
                m = re.search(r"Received \d+ bytes in ([0-9.]+) seconds, ([0-9.]+) Mbps", content)
                if m:
                    elapsed, mbps = m.group(1), m.group(2)

        row = f"{cc},{da},{db},{dc},{pa},{pb},{pc},{elapsed},{mbps},{exit_code}"
        with open(csv_file, "a") as f:
            f.write(row + "\n")
        info(f"Result: {row}\n")

    info(f"\nPicoquic sweep completed! Results in {csv_file}\n")


def main():
    parser = argparse.ArgumentParser(description="Mininet MP-QUIC 3-Link Ground Testbed")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Open Mininet interactive CLI after setting up the topology",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run network connectivity and policy routing tests and exit",
    )
    parser.add_argument(
        "--sweep",
        choices=["stream", "datagram"],
        help="Run the automated n0q scheduler x CC sweep (stream or datagram)",
    )
    parser.add_argument(
        "--picoquic-sweep",
        action="store_true",
        help="Run the automated Picoquic CC sweep",
    )
    args = parser.parse_args()

    # Check root privileges
    if os.geteuid() != 0:
        print("ERROR: Mininet requires root privileges. Please run with 'sudo python3 mininet_topo.py'")
        sys.exit(1)

    setLogLevel("info")

    net = None
    try:
        net, client, server = build_mpquic_network()
        passed = test_connectivity(client, server)

        if args.sweep:
            run_automated_sweep(client, server, mode=args.sweep)
        elif args.picoquic_sweep:
            run_picoquic_sweep(client, server)
        elif args.cli or (not args.test and not args.sweep and not args.picoquic_sweep):
            info("\n*** Network ready! Entering Mininet interactive CLI ***\n")
            info("Tips:\n")
            info("  - Open node terminals:    mininet> xterm client server\n")
            info("  - Direct ping Link A:     mininet> client ping -c 2 -I 172.16.1.2 172.16.1.1\n")
            info("  - Policy ping Server IP:  mininet> client ping -c 2 -I 172.16.2.2 10.99.0.1\n")
            info("  - Check interface stats:  mininet> client ip -s link show client-eth0\n")
            info("  - Exit and clean up:      mininet> exit\n\n")
            CLI(net)
    except KeyboardInterrupt:
        info("\nReceived keyboard interrupt, exiting...\n")
    except Exception as e:
        error(f"\nError encountered: {e}\n")
    finally:
        if net:
            info("*** Stopping Mininet network and cleaning up...\n")
            net.stop()
            os.system("mn -c >/dev/null 2>&1")


if __name__ == "__main__":
    main()
