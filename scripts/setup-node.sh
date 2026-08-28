#!/usr/bin/env bash
set -euo pipefail

echo "============================================="
echo " Setting up Mini PC (Client/Server) for n0q"
echo "============================================="

# 1. Update and install system dependencies
echo "[1/4] Installing system dependencies..."
sudo apt-get update -y
sudo apt-get install -y \
    curl \
    git \
    build-essential \
    pkg-config \
    libssl-dev \
    iproute2 \
    iputils-ping \
    tcpdump \
    ethtool \
    linux-tools-common \
    linux-tools-generic \
    iperf3

# 2. Install Rust (if not already installed)
echo "[2/4] Checking for Rust..."
if ! command -v cargo &> /dev/null; then
    echo "Rust not found. Installing via rustup..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
else
    echo "Rust is already installed."
fi

# 3. Clone n0q
echo "[3/4] Cloning n0q repository..."
cd "$(dirname "$0")/../.." # Go up to the parent directory of multipath-quic3
N0Q_DIR="$(pwd)/n0q"

# We assume it's in the same github account/organization. 
# You can change this if the URL is different.
N0Q_REPO="https://github.com/ekstrah/n0q.git" 

if [ ! -d "$N0Q_DIR" ]; then
    echo "Cloning n0q from $N0Q_REPO into $N0Q_DIR ..."
    git clone "$N0Q_REPO" "$N0Q_DIR"
else
    echo "Directory $N0Q_DIR already exists. Pulling latest..."
    cd "$N0Q_DIR"
    git pull
fi

# 4. Build n0q
echo "[4/4] Building n0q..."
cd "$N0Q_DIR"
# Force build the binaries that the sweep scripts use
cargo build --release --bin n0q-client
cargo build --release --bin n0q-server

echo "============================================="
echo " Setup complete! n0q is built and ready."
echo "============================================="
echo "Don't forget to configure your network interfaces in env.sh"
