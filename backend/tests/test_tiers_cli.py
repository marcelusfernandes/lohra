"""Fatia B — a superfície do OPERADOR para o tier map.

O agente já enxerga tiers (list_models anexa o map) e o cross-provider via tier
já funciona; o que não existia era o lado humano: nenhum writer, nenhuma CLI,
doctor sem remedy, `lohra models` cego para o próprio arquivo. A heurística é
SUGESTÃO apresentada — nunca escrita sem confirmação (e nunca prompta fora de
TTY: a lição do c705c67).
"""

from __future__ import annotations

import json

import pytest

from lohra import cli
from lohra.catalog.catalog import Catalog, ProviderModels
from lohra.catalog.suggest import suggest_tiers
from lohra.workflow.tiers import load_tiers, write_tiers


def _catalog(*entries: ProviderModels) -> Catalog:
    return Catalog(entries=tuple(entries))


def _entry(provider: str, *models: str, source: str = "live") -> ProviderModels:
    return ProviderModels(provider, source, tuple(models), total=len(models))


# --- heurística de sugestão (pura, sem rede) --------------------------------


def test_suggest_classifies_small_by_name_and_prefers_aux_hint():
    catalog = _catalog(
        _entry("anthropic", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"),
    )
    plan = suggest_tiers(catalog)
    assert plan["small"]["model"] == "claude-haiku-4-5"  # default_aux_model do profile
    assert plan["big"]["model"] == "claude-opus-4-8"  # fallback_models[0]
    assert plan["medium"]["model"] == "claude-sonnet-4-6"
    assert all(entry["provider"] == "anthropic" for entry in plan.values())


def test_suggest_skips_a_small_pattern_flagship():
    # gemini lista flash primeiro (pequeno-primeiro) — o big não pode ser um flash.
    catalog = _catalog(_entry("gemini", "gemini-2.0-flash", "gemini-1.5-pro"))
    plan = suggest_tiers(catalog)
    assert plan["big"]["model"] == "gemini-1.5-pro"
    assert plan["small"]["model"] == "gemini-2.0-flash"


def test_suggest_empty_catalog_returns_nothing():
    assert suggest_tiers(_catalog()) == {}


def test_suggest_never_uses_error_or_skipped_entries():
    catalog = _catalog(
        ProviderModels("openai", "skipped", (), total=0, detail="no key"),
        ProviderModels("groq", "error", (), total=0, detail="HTTP 500"),
    )
    assert suggest_tiers(catalog) == {}


# --- writer atômico + round-trip com o loader existente ----------------------


def test_write_tiers_roundtrips_through_load_tiers(tmp_path):
    path = tmp_path / "workflow_tiers.json"
    write_tiers(path, {"big": {"model": "m1", "provider": "openrouter"}, "small": {"model": "m2"}})
    tiers = load_tiers(path)
    assert tiers.get("big").model == "m1" and tiers.get("big").provider == "openrouter"
    assert tiers.get("small").model == "m2"


# --- CLI --------------------------------------------------------------------


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    return tmp_path


def _fake_catalog_builder(monkeypatch, catalog):
    import lohra.catalog.catalog as mod

    monkeypatch.setattr(mod, "build_catalog", lambda **kwargs: catalog)


def test_tiers_list_not_configured_names_the_remedy(home, capsys):
    rc = cli.main(["tiers", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not configured" in out and "lohra tiers suggest" in out


def test_tiers_list_shows_the_map(home, capsys):
    write_tiers(home / "workflow_tiers.json", {"big": {"model": "m", "provider": "p"}})
    rc = cli.main(["tiers", "list"])
    out = capsys.readouterr().out
    assert rc == 0 and "big" in out and "m" in out and "p" in out


def test_tiers_suggest_non_tty_without_yes_prints_plan_and_refuses(home, capsys, monkeypatch):
    # Headless: NUNCA prompta (c705c67). Sem TTY e sem --yes: plano + rc 2, nada escrito.
    _fake_catalog_builder(monkeypatch, _catalog(_entry("anthropic", "claude-opus-4-8", "claude-haiku-4-5")))
    rc = cli.main(["tiers", "suggest"])
    out = capsys.readouterr()
    assert rc == 2
    assert "claude-opus-4-8" in out.out  # o plano foi apresentado
    assert "--yes" in out.out + out.err  # o remédio nomeado
    assert not (home / "workflow_tiers.json").exists()


def test_tiers_suggest_yes_writes_and_names_the_dashboard_note(home, capsys, monkeypatch):
    _fake_catalog_builder(monkeypatch, _catalog(_entry("anthropic", "claude-opus-4-8", "claude-haiku-4-5")))
    rc = cli.main(["tiers", "suggest", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    tiers = load_tiers(home / "workflow_tiers.json")
    assert tiers.get("big").model == "claude-opus-4-8"
    assert "dashboard" in out  # aviso de staleness (WorkflowService cacheia na construção)


def test_tiers_suggest_empty_catalog_refuses_didactically(home, capsys, monkeypatch):
    _fake_catalog_builder(monkeypatch, _catalog())
    rc = cli.main(["tiers", "suggest", "--yes"])
    assert rc == 2
    assert "lohra models" in capsys.readouterr().out  # aponta o diagnóstico
    assert not (home / "workflow_tiers.json").exists()


# --- lohra models mostra o tier map ------------------------------------------


def test_models_human_output_includes_tiers(home, capsys, monkeypatch):
    write_tiers(home / "workflow_tiers.json", {"big": {"model": "m", "provider": "p"}})
    _fake_catalog_builder(monkeypatch, _catalog(_entry("anthropic", "x")))
    rc = cli.main(["models"])
    out = capsys.readouterr().out
    assert rc == 0 and "big" in out and "m" in out


def test_models_json_gains_an_additive_tiers_key(home, capsys, monkeypatch):
    write_tiers(home / "workflow_tiers.json", {"small": {"model": "s"}})
    _fake_catalog_builder(monkeypatch, _catalog(_entry("anthropic", "x")))
    rc = cli.main(["models", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["providers"]  # contrato antigo intacto
    assert payload["tiers"]["small"]["model"] == "s"


# --- doctor: a linha ausente ganha o remedy ----------------------------------


def test_doctor_absent_tiers_line_names_the_suggest_remedy(home):
    from lohra.onboarding import detect
    from lohra.onboarding.doctor import run_checks

    snapshot = detect.detect_environment(env={})
    checks = run_checks(snapshot)
    line = next(c for c in checks if c.name == "workflow_tiers.json")
    assert "lohra tiers suggest" in (line.detail + (line.remedy or ""))


def test_suggest_medium_prefers_the_big_providers_family(monkeypatch):
    # anthropic sem classe média sobrando não pode empurrar o medium para um
    # modelo aleatório de outro provider quando o provider do big tem um
    # candidato médio via fallback (dogfood ao vivo 2026-08-28).
    catalog = _catalog(
        _entry("anthropic", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"),
        _entry("openrouter", "tencent/hy4-preview"),
    )
    plan = suggest_tiers(catalog)
    assert plan["big"]["provider"] == "anthropic"
    assert plan["medium"]["provider"] == "anthropic"  # nunca o tencent aleatório
