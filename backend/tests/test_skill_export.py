"""O kit de delegação (skill use-lohra) viaja no pacote e sai por `lohra skill export`."""

from pathlib import Path

import pytest

from lohra.skills.exportkit import export_root, list_exportable, read_exportable, write_exportable


def test_the_use_lohra_kit_ships_inside_the_package():
    assert "use-lohra" in list_exportable()
    body = read_exportable("use-lohra")
    assert "lohra chat --profile" in body  # a invocação que a skill ensina
    assert "--json" in body


def test_the_packaged_copy_never_drifts_from_the_repo_docs_copy():
    # Anti-drift (mesma filosofia dos contratos da skill builtin): a cópia
    # empacotada É a de docs/skills — divergiu, este teste quebra.
    repo_copy = Path(__file__).resolve().parents[2] / "docs" / "skills" / "use-lohra" / "SKILL.md"
    if not repo_copy.exists():
        pytest.skip("fora do checkout do repo (instalação standalone)")
    assert read_exportable("use-lohra") == repo_copy.read_text(encoding="utf-8")


def test_export_writes_the_skill_under_the_destination(tmp_path):
    out = write_exportable("use-lohra", tmp_path)
    assert out == tmp_path / "use-lohra" / "SKILL.md"
    assert out.read_text(encoding="utf-8") == read_exportable("use-lohra")


def test_an_unknown_kit_is_a_clean_error(tmp_path):
    with pytest.raises(KeyError) as err:
        write_exportable("nope", tmp_path)
    assert "use-lohra" in str(err.value)  # o erro lista o que existe


def test_the_export_root_is_package_data_not_the_repo():
    assert export_root().name == "export"
    assert (export_root() / "use-lohra" / "SKILL.md").exists()
