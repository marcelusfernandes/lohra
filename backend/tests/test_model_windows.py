"""O cache de janelas de contexto por modelo (issue #38).

O catálogo já fala com a API dos providers e vê ``context_length`` por modelo;
este cache é a ponte entre aquele read (raro, sob `lohra models`) e o preflight
de compactação (quente, a cada iteração do loop). Ele é BEST-EFFORT por
construção: um json corrompido, um home read-only ou um disco cheio degradam
para "não sei" — nunca para uma exceção no meio de um turno.
"""

from __future__ import annotations

import json

import pytest

from lohra.catalog import windows as win


@pytest.fixture(autouse=True)
def _fresh_memo():
    win.clear_cache()
    yield
    win.clear_cache()


# --- localização --------------------------------------------------------------


def test_the_file_lives_in_the_given_home(tmp_path):
    assert win.windows_path(tmp_path) == tmp_path / "model_windows.json"


def test_without_a_home_it_follows_the_active_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("LOHRA_PROFILE", "trabalho")
    assert win.windows_path() == tmp_path / "profiles" / "trabalho" / "model_windows.json"


# --- escrita + merge ----------------------------------------------------------


def test_remembering_windows_writes_them_and_reads_back(tmp_path):
    assert win.remember_windows({"openrouter": {"a/b": 32_000}}, home=tmp_path) is True
    assert win.load_windows(tmp_path) == {"openrouter": {"a/b": 32_000}}
    assert win.lookup("openrouter", "a/b", home=tmp_path) == 32_000


def test_a_second_write_merges_instead_of_replacing_the_file(tmp_path):
    win.remember_windows({"openrouter": {"a/b": 32_000}}, home=tmp_path)
    win.remember_windows({"together": {"x/y": 8_192}}, home=tmp_path)
    # Um `lohra models --provider together` não pode apagar o que já se sabia
    # sobre a openrouter.
    assert win.load_windows(tmp_path) == {
        "openrouter": {"a/b": 32_000},
        "together": {"x/y": 8_192},
    }


def test_within_a_provider_a_newer_read_wins_over_the_old_value(tmp_path):
    win.remember_windows({"openrouter": {"a/b": 32_000, "c/d": 1_000}}, home=tmp_path)
    win.remember_windows({"openrouter": {"a/b": 65_536}}, home=tmp_path)
    assert win.load_windows(tmp_path)["openrouter"] == {"a/b": 65_536, "c/d": 1_000}


def test_nothing_to_remember_touches_no_disk(tmp_path):
    assert win.remember_windows({}, home=tmp_path) is False
    assert win.remember_windows({"openrouter": {}}, home=tmp_path) is False
    assert not win.windows_path(tmp_path).exists()


def test_junk_never_reaches_the_file(tmp_path):
    win.remember_windows(
        {"openrouter": {"ok": 100, "zero": 0, "neg": -1, "texto": "80k", "sim": True}},
        home=tmp_path,
    )
    assert win.load_windows(tmp_path) == {"openrouter": {"ok": 100}}


def test_the_write_is_atomic_and_leaves_no_temp_behind(tmp_path):
    win.remember_windows({"openrouter": {"a": 1}}, home=tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["model_windows.json"]


def test_a_provider_map_is_capped_so_the_file_cannot_grow_without_bound(tmp_path):
    huge = {f"m{i}": 1_000 + i for i in range(win.MAX_MODELS_PER_PROVIDER + 50)}
    win.remember_windows({"openrouter": huge}, home=tmp_path)
    assert len(win.load_windows(tmp_path)["openrouter"]) == win.MAX_MODELS_PER_PROVIDER


def test_an_unwritable_home_degrades_to_false_instead_of_raising(tmp_path):
    blocked = tmp_path / "arquivo-no-lugar-do-diretorio"
    blocked.write_text("sou um arquivo")
    assert win.remember_windows({"openrouter": {"a": 1}}, home=blocked) is False


# --- leitura defensiva --------------------------------------------------------


def test_a_missing_file_is_an_empty_answer_not_an_error(tmp_path):
    assert win.load_windows(tmp_path) == {}
    assert win.lookup("openrouter", "a/b", home=tmp_path) is None


def test_a_corrupt_json_is_ignored_without_breaking_the_turn(tmp_path):
    win.windows_path(tmp_path).write_text("{isto não é json")
    assert win.load_windows(tmp_path) == {}
    assert win.lookup("openrouter", "a/b", home=tmp_path) is None


def test_a_wrong_shape_is_ignored_entry_by_entry(tmp_path):
    win.windows_path(tmp_path).write_text(
        json.dumps(
            {
                "openrouter": {"bom": 4_096, "ruim": "muito", "zero": 0},
                "quebrado": "isto devia ser um dict",
                "vazio": {},
            }
        )
    )
    assert win.load_windows(tmp_path) == {"openrouter": {"bom": 4_096}}


def test_a_top_level_list_is_ignored(tmp_path):
    win.windows_path(tmp_path).write_text(json.dumps([1, 2, 3]))
    assert win.load_windows(tmp_path) == {}


def test_an_oversized_file_is_refused_instead_of_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(win, "MAX_FILE_BYTES", 32)
    win.windows_path(tmp_path).write_text(json.dumps({"openrouter": {f"m{i}": 9 for i in range(50)}}))
    assert win.load_windows(tmp_path) == {}


# --- memoização ---------------------------------------------------------------


def test_the_parse_is_memoized_per_path(tmp_path, monkeypatch):
    win.remember_windows({"openrouter": {"a": 1}}, home=tmp_path)
    reads = []
    real = win.safeio.read_text_bounded
    monkeypatch.setattr(
        win.safeio, "read_text_bounded", lambda p, n: (reads.append(p), real(p, n))[1]
    )
    for _ in range(5):
        assert win.load_windows(tmp_path)["openrouter"] == {"a": 1}
    assert len(reads) == 1, "o loop chama isto a cada iteração; não pode reparsear sempre"


def test_a_write_in_this_process_invalidates_the_memo(tmp_path):
    assert win.load_windows(tmp_path) == {}  # memoiza a ausência
    win.remember_windows({"openrouter": {"a": 1}}, home=tmp_path)
    assert win.load_windows(tmp_path) == {"openrouter": {"a": 1}}


def test_an_external_rewrite_is_picked_up(tmp_path):
    win.remember_windows({"openrouter": {"a": 1}}, home=tmp_path)
    assert win.load_windows(tmp_path)["openrouter"] == {"a": 1}
    win.windows_path(tmp_path).write_text(json.dumps({"openrouter": {"a": 2, "b": 3}}))
    assert win.load_windows(tmp_path)["openrouter"] == {"a": 2, "b": 3}


def test_two_homes_do_not_share_an_answer(tmp_path):
    um, dois = tmp_path / "um", tmp_path / "dois"
    um.mkdir(), dois.mkdir()
    win.remember_windows({"openrouter": {"a": 1}}, home=um)
    win.remember_windows({"openrouter": {"a": 2}}, home=dois)
    assert win.lookup("openrouter", "a", home=um) == 1
    assert win.lookup("openrouter", "a", home=dois) == 2


def test_a_write_that_blows_up_midway_cleans_its_temp_and_reports_false(tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise OSError("disco cheio")

    monkeypatch.setattr(win.json, "dump", explode)
    assert win.remember_windows({"openrouter": {"a": 1}}, home=tmp_path) is False
    assert list(tmp_path.iterdir()) == [], "um tmp órfão no home do usuário é lixo permanente"


def test_a_lookup_without_a_provider_or_a_model_answers_none(tmp_path):
    win.remember_windows({"openrouter": {"a": 1}}, home=tmp_path)
    assert win.lookup("", "a", home=tmp_path) is None
    assert win.lookup("openrouter", "", home=tmp_path) is None


def test_an_empty_provider_name_on_disk_is_dropped(tmp_path):
    win.windows_path(tmp_path).write_text(json.dumps({"": {"a": 1}, "openrouter": {"b": 2}}))
    assert win.load_windows(tmp_path) == {"openrouter": {"b": 2}}


def test_a_provider_with_nothing_usable_never_creates_an_empty_entry(tmp_path):
    win.remember_windows({"openrouter": {"a": 1}}, home=tmp_path)
    win.remember_windows({"openrouter": {"a": 1}, "groq": {"x": "nada"}}, home=tmp_path)
    assert "groq" not in win.load_windows(tmp_path)
