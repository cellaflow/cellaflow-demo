#!/bin/bash
# Daily: drop the engine's data and the API's in-memory runs. Run from cron at a quiet hour.
set -euo pipefail
cd /opt/demo
docker compose -p demo -f compose.prod.yml rm --stop --force engine
docker volume rm demo_engine-data
docker compose -p demo -f compose.prod.yml up -d --wait engine
docker compose -p demo -f compose.prod.yml restart api
