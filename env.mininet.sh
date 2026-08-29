#!/usr/bin/env bash
# Mininet environment configuration for MP-QUIC
# Sources env.sh with virtual interface mappings

export SERVER_IFACE_A=server-eth0
export SERVER_IFACE_B=server-eth1
export SERVER_IFACE_C=server-eth2

export CLIENT_IFACE_A=client-eth0
export CLIENT_IFACE_B=client-eth1
export CLIENT_IFACE_C=client-eth2

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
