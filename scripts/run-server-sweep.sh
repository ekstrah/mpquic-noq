#!/usr/bin/env bash
set -euo pipefail

# Move to the root of the repo and source environment variables
cd "$(dirname "$0")/.."
source env.sh

# Create logging directories
mkdir -p logs/stdout
mkdir -p logs/qlog

echo "Starting N0Q Server Sweep (Stream)..."

for combo in "${SWEEP_COMBOS[@]}"; do
    scheduler=$(echo $combo | awk '{print $1}')
    cc=$(echo $combo | awk '{print $2}')
    
    echo "==========================================="
    echo "Running Server: Scheduler=$scheduler, CC=$cc"
    echo "Logs saving to logs/stdout/server_stream_${scheduler}_${cc}.log"
    echo "==========================================="
    
    export QLOGDIR="$(pwd)/logs/qlog"
    
    # Start the N0Q Server in the background and redirect logs
    cargo run --release --manifest-path ../n0q/Cargo.toml --bin n0q-server -- \
        --listen "$SERVER_CANONICAL_IP:$QUIC_PORT" \
        --multipath \
        --cc "$cc" \
        > "logs/stdout/server_stream_${scheduler}_${cc}.log" 2>&1 &
    SERVER_PID=$!
    
    # Let the server run for the designated window duration
    sleep "$SWEEP_WINDOW_SEC"
    
    # Terminate the server to prepare for the next combination slot
    kill $SERVER_PID || true
    wait $SERVER_PID 2>/dev/null || true
done

echo "Server sweep completed! Check the logs/ folder."
