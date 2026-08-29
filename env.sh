#!/usr/bin/env bash
# Sourced by every script in this repo.
#
# No auto-detection: assign these by hand after physically wiring the
# three point-to-point links, so there's no ambiguity about which NIC is
# which. On each box, run `ip -brief link show`, pick the three NICs that
# are NOT your management/SSH interface, and set the matching variables
# below.

# --- assign these yourself, one file per box (see note at bottom) ---
SERVER_IFACE_A="${SERVER_IFACE_A:-enxf8e43b965cdc}"   # SERVER NIC wired to CLIENT_IFACE_A (LEO link)
SERVER_IFACE_B="${SERVER_IFACE_B:-enxf8e43b9eaa06}"   # SERVER NIC wired to CLIENT_IFACE_B (Mobile link)
SERVER_IFACE_C="${SERVER_IFACE_C:-enx00e04c041621}"   # SERVER NIC wired to CLIENT_IFACE_C (Mesh link)

CLIENT_IFACE_A="${CLIENT_IFACE_A:-enx00e04c680170}"   # CLIENT NIC wired to SERVER_IFACE_A (LEO link)
CLIENT_IFACE_B="${CLIENT_IFACE_B:-enxf8e43b9e8eee}"   # CLIENT NIC wired to SERVER_IFACE_B (Mobile link)
CLIENT_IFACE_C="${CLIENT_IFACE_C:-enxf8e43b9e8bfa}"   # CLIENT NIC wired to SERVER_IFACE_C (Mesh link)

# --- fixed addressing, do not change without updating both boxes ---
LINK_A_SUBNET=172.16.1.0/30
LINK_A_SERVER_IP=172.16.1.1
LINK_A_CLIENT_IP=172.16.1.2

LINK_B_SUBNET=172.16.2.0/30
LINK_B_SERVER_IP=172.16.2.1
LINK_B_CLIENT_IP=172.16.2.2

LINK_C_SUBNET=172.16.3.0/30
LINK_C_SERVER_IP=172.16.3.1
LINK_C_CLIENT_IP=172.16.3.2

SERVER_CANONICAL_IP=10.99.0.1
QUIC_PORT=4433

# --- Link A ("LEO") netem/tbf targets ---
LINK_A_RATE_DOWN_MBIT=62   # server->client (applied as egress shaping on SERVER)
LINK_A_RATE_UP_MBIT=18     # client->server (applied as egress shaping on CLIENT)
LINK_A_DELAY_MS=25
LINK_A_JITTER_MS=13
LINK_A_LOSS_PCT=0.17

# --- Link B ("Mobile") netem/tbf targets ---
LINK_B_RATE_MBIT=30        # symmetric static approximation
LINK_B_DELAY_MS=50
LINK_B_JITTER_MS=5
LINK_B_LOSS_PCT=0.006

# --- Link C ("Mesh") netem/tbf targets ---
# Based on the UAV flight measurement paper, Table I
LINK_C_RATE_MBIT=30        # >30 Mbit/s
LINK_C_DELAY_MS=2          # ~5ms RTT (2.5ms one-way, using 2 for netem)
LINK_C_JITTER_MS=1
LINK_C_LOSS_PCT=0.0

# --- sweep: every (scheduler, cc) combo, and per-combo timing ---
# Single source of truth for run-server-sweep.sh and run-client-sweep.sh -
# both loop this same list in this same order so they stay aligned with
# no channel between the two boxes (see those scripts for how).

SWEEP_COMBOS=(
  "MINRTT BBR"
  "MINRTT BBR3"
  "MINRTT CUBIC"
  "MINRTT COPA"
  "REDUNDANT BBR"
  "REDUNDANT BBR3"
  "REDUNDANT CUBIC"
  "REDUNDANT COPA"
  "ROUNDROBIN BBR"
  "ROUNDROBIN BBR3"
  "ROUNDROBIN CUBIC"
  "ROUNDROBIN COPA"
)
SWEEP_WINDOW_SEC=25          # per-combo slot length on the SERVER side
SWEEP_CLIENT_DELAY_SEC=5     # CLIENT startup delay, lets the fresh server come up
SWEEP_CLIENT_DURATION_SEC=15 # n0q client --duration per combo

# NOTE: this file is the same on both boxes at clone time. Only the three
# CHANGE_ME lines for your box differ in practice (net-server.sh only
# reads SERVER_IFACE_*, net-client.sh only reads CLIENT_IFACE_*), so
# leaving the *other* box's CHANGE_ME values unset on your local edit is
# harmless — just don't commit a CHANGE_ME on the three variables the
# script you're about to run actually needs.
