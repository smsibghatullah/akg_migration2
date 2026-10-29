#!/usr/bin/env bash
# Odoo 16 -> Odoo 20 TRANSACTIONS, DIRECT DB.   Pehle env vars set karo (config.py dekho):
#   export SRC_PGHOST=.. SRC_PGDATABASE=AKG_Hardware SRC_PGUSER=.. SRC_PGPASSWORD=..
#   export DST_PGHOST=.. DST_PGDATABASE=<PROD_DB>    DST_PGUSER=.. DST_PGPASSWORD=..
#   export MASTER_IDMAP=idmap.json      # PROD ke masters wala (UAT wala NAHI)
#
#   ./run_tx_db.sh gaps        # 1) sirf padhta hai: kaun se columns nahi jayenge / dst mein kya required hai
#   ./run_tx_db.sh check       # 2) masters ka gap report
#   ./run_tx_db.sh fix         # 3) natural keys se masters map (commit hota hai)
#   ./run_tx_db.sh test        # 4) DRY-RUN: har step ke 5 docs, aakhir mein ROLLBACK
#   ./run_tx_db.sh all         # 5) POORA migration (commit)   [PROD: Odoo band + backup ke baad]
#   ./run_tx_db.sh step moves --date-from 2025-01-01
#   ./run_tx_db.sh verify | recompute | hr | export
cd "$(dirname "$0")" || exit 1
PY="python3 migrate_tx_db.py"
DB="${DST_PGDATABASE:?DST_PGDATABASE set karo}"
case "$1" in
  gaps)      $PY gaps ;;
  check)     $PY check ;;
  fix)       $PY check --fix --apply --confirm-db "$DB" ;;
  test)      $PY run --steps masters,core --limit 5 ;;
  masters)   shift; $PY run --steps masters --apply --confirm-db "$DB" "$@" ;;
  all)       shift; $PY run --steps masters,core --apply --confirm-db "$DB" "$@" ;;
  hr)        shift; $PY run --steps hr --apply --confirm-db "$DB" "$@" ;;
  step)      s="$2"; shift 2; $PY run --steps "$s" --apply --confirm-db "$DB" "$@" ;;
  verify)    $PY verify --steps all ;;
  recompute) $PY recompute-script ;;   # phir: odoo-bin shell -d $DB --no-http < post_recompute_shell.py
  export)    $PY export-idmap ;;
  *) echo "Use: gaps | check | fix | test | masters | all | hr | step <name> [opts] | verify | recompute | export" ;;
esac
