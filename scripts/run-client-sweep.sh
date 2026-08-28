#!/usr/bin/env bash
set -euo pipefail

# Move to the root of the repo and source environment variables
cd "$(dirname "$0")/.."
source env.sh

# Give the server a moment to start up the very first time
echo "Waiting ${SWEEP_CLIENT_DELAY_SEC}s for the first server to start..."
sleep "$SWEEP_CLIENT_DELAY_SEC"

echo "Starting N0Q Client Sweep..."

for combo in "${SWEEP_COMBOS[@]}"; do
    scheduler=$(echo $combo | awk '{print $1}')
    cc=$(echo $combo | awk '{print $2}')
    
    echo "==========================================="
    echo "Running Client: Scheduler=$scheduler, CC=$cc"
    echo "==========================================="
    
    # Run the N0Q Client. We bind to all three client IPs so the multipath 
    # QUIC implementation knows it can use all of them as separate paths.
    # Adjust the manifest path/binary name based on where you clone n0q.
    cargo run --release --manifest-path ../n0q/Cargo.toml --bin n0q-client -- \
        --bind "$LINK_A_CLIENT_IP" \
        --bind "$LINK_B_CLIENT_IP" \
        --bind "$LINK_C_CLIENT_IP" \
        --multipath \
        --scheduler "$scheduler" \
        --cc "$cc" \
        --duration "$SWEEP_CLIENT_DURATION_SEC" \
        "https://$SERVER_CANONICAL_IP:$QUIC_PORT/video" || true
        
    # Calculate how much time remains in this slot so we stay synced with the server
    sleep_time=$(( SWEEP_WINDOW_SEC - SWEEP_CLIENT_DURATION_SEC ))
    if [ $sleep_time -gt 0 ]; then
        echo "Client run finished. Waiting ${sleep_time}s for the next server slot..."
        sleep "$sleep_time"
    fi
done

echo "Client sweep completed!"
