#!/usr/bin/env bash
set -euo pipefail

# Move to the root of the repo and source environment variables
cd "$(dirname "$0")/.."
source env.sh

echo "Starting N0Q Server Datagram Sweep..."

for combo in "${SWEEP_COMBOS[@]}"; do
    scheduler=$(echo $combo | awk '{print $1}')
    cc=$(echo $combo | awk '{print $2}')
    
    echo "==========================================="
    echo "Running Server (Datagram): Scheduler=$scheduler, CC=$cc"
    echo "==========================================="
    
    # Start the N0Q Server in Datagram mode in the background.
    cargo run --release --manifest-path ../n0q/Cargo.toml --bin n0q-server -- \
        --listen "$SERVER_CANONICAL_IP:$QUIC_PORT" \
        --multipath \
        --datagram \
        --cc "$cc" &
    SERVER_PID=$!
    
    # Let the server run for the designated window duration
    sleep "$SWEEP_WINDOW_SEC"
    
    # Terminate the server to prepare for the next combination slot
    kill $SERVER_PID || true
    wait $SERVER_PID 2>/dev/null || true
done

echo "Server datagram sweep completed!"
