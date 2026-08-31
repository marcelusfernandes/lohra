# Lohra

Agente de IA self-improving — projeto original: um runtime Python headless (CLI, envelope de orquestração `--json`, servidor OpenAI-compatível), publicado no PyPI. Nasceu em 2026 inspirado na arquitetura do Hermes Agent (Nous Research, MIT) como referência inicial; hoje o núcleo (harness de workflows declarativo com 10 node-types, subscription auth, profiles isolados, runtime standalone instalável) diverge sem equivalente na referência.

## Instalar (PyPI)

```bash
pip install lohra          # Python 3.11–3.13
lohra chat "olá"           # CLI · lohra chat --json (orquestração) · lohra serve (API OpenAI-compat)
lohra workflow list        # espectador de runs, sem gastar tokens
```

Guia standalone (incl. Windows e subscription): `docs/STANDALONE.md`.

## Estrutura

```
lohra/
├── docs/
│   ├── ARCHITECTURE.md        # visão geral em 3 camadas + invariantes
│   ├── ROADMAP.md             # plano faseado (Fases 0–10 + CC-Parity)
│   └── specs/                 # specs dos subsistemas (históricos do bootstrap)
├── backend/                   # agent core + gateway (Python)
│   ├── pyproject.toml
│   ├── lohra/
│   │   ├── agent/             # loop de conversa, transports, prompt builder
│   │   ├── providers/         # ProviderProfile + registry
│   │   ├── tools/             # registry de tools + dispatch
│   │   ├── memory/            # memory, skills, state SQLite+FTS5
│   │   ├── gateway/           # FastAPI: WS JSON-RPC + REST
│   │   ├── server/            # servidor HTTP OpenAI-compat (chat/completions + responses)
│   │   ├── workflow/          # harness de dynamic workflows (DAG, engine, sandbox, supervisão)
│   │   └── cli.py
│   └── tests/
└── (app desktop: fora do repo — possível reescrita futura do zero)
```

## Status: Fases 0–10 + CC-Parity + Waves 4/6 completas (0.0.13)

Backend com **2451 testes** (94% cobertura, ruff limpo): agent core multi-provider, tools com approval gate, gateway WS/REST + desktop, memória + skills self-improving, compactação + subagentes, harness de workflows declarativo (10 node-types, token budget, estado durável cross-process, checkpoint humano), **supervisão ativa de runs em voo** (steering de leaf, notices duráveis entre turnos, doutrina agente×humano com freios), subscription auth opt-in e runtime standalone instalável (`pip install`). Detalhe por fase: `CLAUDE.md` e `docs/history/ROADMAP-CC-PARITY.md`.

## Rodar (dev)

```bash
# backend
cd backend
python3 -m pip install -e ".[dev]"     # ou: uv pip install -e ".[dev]"
lohra --version
pytest

# desktop (precisa de Node + Rust toolchain)
cd desktop
npm install
npm run tauri dev
```
