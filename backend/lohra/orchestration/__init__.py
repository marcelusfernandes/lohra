"""Orchestration — the Lohra agent spawns, steers, and collects sub-sessions.

One core (`OrchestrationCore`), two consumers: the agent tool triad
(`spawn_session`/`steer_session`/`collect_session`) and the gateway WS methods.
Sub-sessions are INDEPENDENT (own isolated Agent, frozen prompt), run on a
capped thread pool, and are steerable mid-turn via a per-session inbox.
See docs/specs/06-orchestration.md.
"""
