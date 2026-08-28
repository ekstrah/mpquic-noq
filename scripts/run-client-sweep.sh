#!/usr/bin/env bash
set -euo pipefail

# Move to the root of the repo and source environment variables
cd "$(dirname "$0")/.."
source env.sh

# Create logging directories
mkdir -p logs/stdout
mkdir -p logs/qlog

# Give the server a moment to start up the very first time
echo "Waiting ${SWEEP_CLIENT_DELAY_SEC}s for the first server to start..."
sleep "$SWEEP_CLIENT_DELAY_SEC"

echo "Starting N0Q Client Sweep (Stream)..."

for combo in "${SWEEP_COMBOS[@]}"; do
    scheduler=$(echo $combo | awk '{print $1}')
    cc=$(echo $combo | awk '{print $2}')
    
    echo "==========================================="
    echo "Running Client: Scheduler=$scheduler, CC=$cc"
    echo "Logs saving to logs/stdout/client_${scheduler}_${cc}.log"
    echo "==========================================="
    
    # Export QLOGDIR so compatible Rust QUIC crates automatically dump qlog JSONs here
    export QLOGDIR="$(pwd)/logs/qlog"
    
    # Run the N0Q Client and save all terminal output to a log file
    cargo run --release --manifest-path ../n0q/Cargo.toml --bin n0q-client -- \
        --bind "$LINK_A_CLIENT_IP" \
        --bind "$LINK_B_CLIENT_IP" \
        --bind "$LINK_C_CLIENT_IP" \
        --multipath \
        --scheduler "$scheduler" \
        --cc "$cc" \
        --duration "$SWEEP_CLIENT_DURATION_SEC" \
        "https://$SERVER_CANONICAL_IP:$QUIC_PORT/video" \
        > "logs/stdout/client_stream_${scheduler}_${cc}.log" 2>&1 || true
        
    # Calculate how much time remains in this slot so we stay synced with the server
    sleep_time=$(( SWEEP_WINDOW_SEC - SWEEP_CLIENT_DURATION_SEC ))
    if [ $sleep_time -gt 0 ]; then
        echo "Client run finished. Waiting ${sleep_time}s for the next server slot..."
        sleep "$sleep_time"
    fi
done

echo "Client sweep completed! Check the logs/ folder."
