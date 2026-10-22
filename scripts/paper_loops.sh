#!/bin/bash
# Hosted PAPER-ONLY loops (no keys, no signers, public data only). Every flag that could act is forced off.
set -u
export NOEMA_EXECUTION_GATEWAY_ENABLED=0 NOEMA_ALLOW_LIVE_ORDERS=0 NOEMA_TREASURY_ACTIONS_ENABLED=0
export NOEMA_SOLANA_SIGNER_ENABLED=0 NOEMA_EVM_SIGNER_ENABLED=0 NOEMA_MASTER_HALT=1
D=${NOEMA_DATA_DIR:-/opt/render/project/src/data}
mkdir -p "$D"
run() { while true; do "$@"; echo "$(date -u +%FT%TZ) exited $?, restarting in 30s"; sleep 30; done & }
run python -m noema.launch_stream --db "$D/noema.db"
run python -m noema.pmus_history --loop --db "$D/pmus_history.db"
run python -m noema.xv_match --loop --db "$D/noema.db"
run python -m noema.edge_avenues --loop --db "$D/edge_avenues.db" --main-db "$D/noema.db" --life-db "$D/agent_lifecycle.db"
run python -m noema.pm_weak --loop --db "$D/pm_train.db" --avenues-db "$D/edge_avenues.db"
run python -m noema.agent_lifecycle --loop --db "$D/noema.db" --life-db "$D/agent_lifecycle.db" --avenues-db "$D/edge_avenues.db"
wait
