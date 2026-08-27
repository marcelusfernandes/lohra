# Lohra

Agente de IA self-improving com app desktop — projeto original: backend próprio em Python, casca desktop própria em Tauri + React. Nasceu em 2026 inspirado na arquitetura do Hermes Agent (Nous Research, MIT) como referência inicial; hoje o núcleo (harness de workflows declarativo com 10 node-types, subscription auth, profiles isolados, runtime standalone instalável) diverge sem equivalente na referência.

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
│   │   ├── gateway/           # FastAPI: WS JSON-RPC + REST + OpenAI server
│   │   └── cli.py
│   └── tests/
└── (app desktop Tauri: incubando fora do repo público até amadurecer)
```

## Status: Fases 0–10 completas + campanha CC-Parity mergeada

Backend com **1273 testes** (94% cobertura, ruff limpo): agent core multi-provider, tools com approval gate, gateway WS/REST + desktop, memória + skills self-improving, compactação + subagentes, harness de workflows declarativo (10 node-types, token budget, estado durável cross-process, checkpoint humano), subscription auth opt-in e runtime standalone instalável (`pip install`). Detalhe por fase: `CLAUDE.md` e `docs/history/ROADMAP-CC-PARITY.md`.

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
