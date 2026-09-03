#!/bin/zsh
# Rodada 3 — métrica refinada: maior silêncio ANTES do primeiro token de saída
# (a fase de raciocínio, que é onde o abort não alcançava). Braços intercalados.
set -u
SP=/private/tmp/claude-501/-Users-marcelusfernandes-Desktop-playground-ai-lohra/5656cdd2-c639-4295-ab2c-d035aceec985/scratchpad
WT=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/reasoning-summary/backend
cd "$WT" || exit 1
export PYTHONPATH="$WT"

echo "=== INÍCIO R3 $(date -u +%FT%TZ) ==="
for round in 1 2 3; do
  for mode in off auto; do
    echo "--- R3-$mode-$round $(date -u +%FT%TZ) ---"
    python3 "$SP/probe_summary.py" gpt-5.6-sol "$mode" "R3-$mode-$round" 2>&1
  done
done
echo "=== FIM R3 $(date -u +%FT%TZ) ==="
