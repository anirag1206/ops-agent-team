#!/bin/sh
# Run the middleman on a Linux server (e.g. the box where lucent-kb lives).
# Keys come from the environment: WAMCP_KEY and XAI_API_KEY (or ANTHROPIC_API_KEY).
# Keeps restarting if it crashes or the network drops.
cd "$(dirname "$0")"
while true; do
  python3 run.py middleman
  echo "Middleman stopped; restarting in 10 seconds..."
  sleep 10
done
