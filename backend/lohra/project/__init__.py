"""Project-context awareness (Fase 9).

When Lohra runs inside a project, it discovers the project's agent instructions
(AGENTS.md / CLAUDE.md) and skills, so it understands and follows what's already
there. Instructions feed the frozen system prompt's context tier (Invariante #1:
injected once at Agent construction); skills are discovered by the SkillStore.
"""
