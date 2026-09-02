# Lohra

Agente de IA self-improving — projeto original: um runtime Python headless (CLI + envelope de orquestração + servidor OpenAI-compat), publicado no PyPI. (Um app desktop Tauri existiu nas fases iniciais; está fora do repo, com possível reescrita futura do zero.) Começou (2026) inspirado na arquitetura do Hermes Agent (Nous Research, MIT, `github.com/nousresearch/hermes-agent`) como referência — e divergiu: hoje o núcleo (harness de workflows declarativo, token budget, estado durável cross-process, subscription auth, profiles isolados) é projeto próprio sem equivalente na referência. Nenhum código foi copiado verbatim.

## Decisões travadas (não re-perguntar)
- **Projeto original** — backend + frontend próprios, zero dependência do código do Hermes (referência histórica de arquitetura, não base de código).
- **Paridade completa** — tools, memória persistente, skills auto-geradas, multi-provider, gateway de mensageria.
- **Backend: Python** (`backend/`, pacote `lohra`). **Desktop: Tauri + React** (`desktop/`).


## Estado (resumo — detalhe em `docs/STATUS.md`)
Runtime Python publicado no PyPI (`pip install lohra`, 0.0.19). Fases 0–10, CC-Parity, Standalone e Waves 4/6/7/7.5 completas; Wave 8 aberta (investigação feita, épicos propostos em `docs/history/reviews/2026-09-02-wave8-investigation.md`). Suíte ~2767 testes, 95% cobertura, ruff limpo. O histórico por fase/wave, as pendências nomeadas e o **Próximo** vivem em `docs/STATUS.md` — leia antes de planejar; atualize lá (não aqui) ao fechar uma rodada.

## Regras operacionais (aprendidas em rodadas reais — não re-descobrir)
- **Branches**: todo trabalho novo numa branch `<tipo>/<desc>`; nunca merge na `main` sem o usuário testar e aprovar. Rodadas com várias fatias usam uma branch `integration/<rodada>` e uma worktree por fatia (`../lohra-wt/<nome>`).
- **Integração LINEAR**: rebasear cada branch sobre a integração e fast-forward; **nenhum merge commit** na `main` — o `release.sh` espelha a `main` no repo público por cherry-pick de commits não-merge, e uma resolução de conflito que só existe num merge commit quebra o espelho.
- **`origin/main` é história pública reescrita** (sem `desktop/`; hashes ≠ `main` local): NUNCA push da `main` local. Release = `./release.sh -y` (gitignored, raiz): espelha main→`public/main` a partir da tag `public-sync`, push, build em `dist/`, publica via `./publish.sh` (token em `~/.pypi-token`). Se o espelho já foi feito por outro caminho, avance a tag (`git tag -f public-sync main`) antes; a `main` remota exige história linear e proíbe force-push (hook `.claude/hooks/block-force-push.sh`).
- **Agentes implementadores** (worktrees) não tocam `CHANGELOG.md`, `CLAUDE.md`, `docs/STATUS.md`, versão no `pyproject.toml`; isso é feito num commit de fechamento na integração. Skills builtin têm orçamento de 800 linhas testado (`workflow-authoring` está em ~798). A skill `use-lohra` tem 3 cópias (export, `docs/skills`, `.codex/skills` local gitignored) com teste anti-drift — sincronizar a `.codex` à mão após mudar.
- **Verificar a issue no código antes de implementar** (várias issues tinham o mecanismo errado); **review adversarial independente** nas fatias de segurança/concorrência/doutrina, gates aplicados antes do merge; **dogfood ao vivo via Codex headless** (receita de sandbox em `docs/history/reviews/2026-09-02-dogfood-codex-wave7.5.md`; profile `lohra-dogfood-w75` já tem subscription) antes da release.
- **Hooks de Stop** (`.claude/hooks/pytest-check.sh` e `.codex/hooks/pytest-check.sh`, ambos locais) rodam a suíte; o regex precisa ignorar `xfailed` (já corrigido nesta máquina).

**Subagentes delegados** (relembrar): isolados (sem memória/skills/context do pai — o pai DESTILA e passa na task); usam tools de trabalho (read/write/terminal[dangerous auto-deny]/web/mcp), excluídas as stateful (memory/skill/session_search/cron), orquestração e vision/image_gen.

## Leia primeiro
- `docs/STATUS.md` — estado atual por fase/wave, pendências nomeadas e o próximo passo.
- `docs/ARCHITECTURE.md` — visão em 3 camadas + invariantes load-bearing.
- `docs/ROADMAP.md` — plano faseado (0→10) com checklist; o trabalho pós-Fase-10 é rastreado como Waves via GitHub milestones/issues.
- `docs/specs/01-04, 06-08` — specs detalhados por subsistema (05/desktop foi para `docs/history/`); 06-08 cobrem orquestração e o workflow harness.

## Invariante #1 (não quebrar)
System prompt em 3 tiers (stable→context→volatile), construído **uma vez por sessão e congelado**. Memória e skills atualizam o disco, **nunca** o prompt vivo — é o que mantém o prefix-cache do provider quente. Detalhe em `docs/ARCHITECTURE.md`.

## Referência
Hermes serviu de prior art só na fase inicial (specs 01–05, Fases 0–3). Não é mais consultado ativamente — desde a Fase 6 e a campanha CC-Parity a arquitetura diverge sem correspondente na referência. Reclonar `github.com/nousresearch/hermes-agent` em `/tmp` só para revisitar decisões da fase inicial.

## Convenções
TDD (teste primeiro, 80%+ cobertura) · arquivos pequenos (200–400 linhas, 800 max) · imutabilidade (nunca mutar, retornar cópias) · sem segredos hardcoded · conventional commits.

## Rodar
```bash
cd backend && python3 -m pip install -e ".[dev]" && lohra --version && pytest
# app desktop: fora do repo público (incubando; possível reescrita do zero)
```
