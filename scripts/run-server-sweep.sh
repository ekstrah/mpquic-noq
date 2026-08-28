#!/usr/bin/env bash
set -euo pipefail

# Move to the root of the repo and source environment variables
cd "$(dirname "$0")/.."
source env.sh

echo "Starting N0Q Server Sweep..."

for combo in "${SWEEP_COMBOS[@]}"; do
    scheduler=$(echo $combo | awk '{print $1}')
    cc=$(echo $combo | awk '{print $2}')
    
    echo "==========================================="
    echo "Running Server: Scheduler=$scheduler, CC=$cc"
    echo "==========================================="
    
    # Start the N0Q Server in the background. 
    # We bind to the canonical server IP (10.99.0.1) so the client can 
    # route to it via any of the three physical interfaces.
    # Adjust the manifest path/binary name based on where you clone n0q.
    cargo run --release --manifest-path ../n0q/Cargo.toml --bin n0q-server -- \
        --listen "$SERVER_CANONICAL_IP:$QUIC_PORT" \
        --multipath \
        --cc "$cc" &
    SERVER_PID=$!
    
    # Let the server run for the designated window duration
    sleep "$SWEEP_WINDOW_SEC"
    
    # Terminate the server to prepare for the next combination slot
    kill $SERVER_PID || true
    wait $SERVER_PID 2>/dev/null || true
done

echo "Server sweep completed!"
