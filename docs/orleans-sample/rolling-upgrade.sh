#!/bin/bash

set -e

echo "Starting rolling upgrade..."

NUM_SILOS=${NUM_SILOS:-9}
SLEEP_AFTER_STOP_SEC=${SLEEP_AFTER_STOP_SEC:-0}
SLEEP_AFTER_START_SEC=${SLEEP_AFTER_START_SEC:-0}

for i in $(seq 1 $((NUM_SILOS))); do
  echo "Upgrading node $i..."
  docker compose stop node$i
  sleep $SLEEP_AFTER_STOP_SEC
  docker compose start node$i
  sleep $SLEEP_AFTER_START_SEC
done
