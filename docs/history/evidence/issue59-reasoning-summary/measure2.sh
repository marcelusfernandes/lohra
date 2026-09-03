#!/bin/zsh
# Medição ao vivo da issue #59 — RODADA 2 (corrigida: PYTHONPATH força o código
# da worktree nas sondas; na rodada 1 as sondas importaram o checkout instalado).
set -u
SP=/private/tmp/claude-501/-Users-marcelusfernandes-Desktop-playground-ai-lohra/5656cdd2-c639-4295-ab2c-d035aceec985/scratchpad
WT=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/reasoning-summary/backend
cd "$WT" || exit 1
export PYTHONPATH="$WT"

echo "=== INÍCIO R2 $(date -u +%FT%TZ) ==="

echo "--- TIPOS summary=off $(date -u +%FT%TZ) ---"
python3 "$SP/probe_types.py" gpt-5.6-sol off "TIPOS-OFF" 2>&1
echo "--- TIPOS summary=auto $(date -u +%FT%TZ) ---"
python3 "$SP/probe_types.py" gpt-5.6-sol auto "TIPOS-AUTO" 2>&1

for round in 1 2; do
  echo "--- PROBE DEPOIS-$round (summary=auto) $(date -u +%FT%TZ) ---"
  python3 "$SP/probe_summary.py" gpt-5.6-sol auto "DEPOIS-$round" 2>&1
  echo "--- PROBE ANTES-R2-$round (summary=off, código da worktree) $(date -u +%FT%TZ) ---"
  python3 "$SP/probe_summary.py" gpt-5.6-sol off "ANTES-R2-$round" 2>&1
done

echo "=== CLI 2x2 (receita do briefing) ==="
unset PYTHONPATH  # os braços do CLI usam `python3 -c` com cwd => resolvem sozinhos
for round in 1 2; do
  for arm in before after; do
    label=$([ "$arm" = "before" ] && echo "CLI-ANTES-$round" || echo "CLI-DEPOIS-$round")
    echo "--- $label $(date -u +%FT%TZ) ---"
    python3 "$SP/run_arm.py" "$arm" "$label" 2>&1
  done
done

echo "=== FIM R2 $(date -u +%FT%TZ) ==="
