#!/usr/bin/env bash
set -euo pipefail
RUN_DIR="/home/user/rig-ft/train_run"
LOG_DIR="/home/user/rig-ft"
RESULTS_FILE="$LOG_DIR/training_results.json"

echo '{"rounds": [{"round": 1, "loss": "0.809", "adapter": "/home/user/rig-ft/output/round1/adapter_model.safetensors"}]}' > "$RESULTS_FILE"

for i in $(seq 2 10); do
  echo ""
  echo "============================================"
  echo "ROUND $i TRAINING"
  echo "============================================"
  rm -rf "$RUN_DIR/last_run_prepared" 2>/dev/null || true
  cd "$RUN_DIR"
  CUDA_VISIBLE_DEVICES=1 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    accelerate launch -m axolotl.cli.train "axolotl-round${i}.yaml" 2>&1 | tee "$LOG_DIR/training-r${i}.log"
  
  ADAPTER="/home/user/rig-ft/output/round${i}/adapter_model.safetensors"
  if [ -f "$ADAPTER" ]; then
    echo "✓ Round $i completed"
    FINAL_LOSS=$(grep "'loss'" "$LOG_DIR/training-r${i}.log" | tail -1 | sed "s/.*'loss': '\([0-9.]*\)'.*/\1/" || echo "unknown")
    echo "  Final loss: $FINAL_LOSS"
    python3 -c "
import json
with open('$RESULTS_FILE') as f:
    r = json.load(f)
r['rounds'].append({'round': $i, 'loss': '$FINAL_LOSS'})
with open('$RESULTS_FILE', 'w') as f:
    json.dump(r, f, indent=2)
"
    python3 ~/.rig/scripts/qnap-bridge.py put "$ADAPTER" "models/rig-kat-round${i}.safetensors" 2>/dev/null || true
  else
    echo "✗ Round $i FAILED"
  fi
done

echo ""
echo "============================================"
echo "ALL 10 ROUNDS COMPLETE"
echo "============================================"
cat "$RESULTS_FILE"
