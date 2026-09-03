"""#45 E4 — o manifesto de artefato, medido pelo harness e cobrado no replay.

A investigação da run real `lohra-notion-v4` provou a violação: 3 de 5 artefatos
declarados por células foram MUTADOS depois da gravação (por um leaf vivo e
legítimo), e duas células replaiaram 2× afirmando o que já não era verdade. O
prejuízo foi zero só porque aquela spec não tinha `${ref}` — controle negativo,
não garantia.

O que estes testes fixam:
- o harness mede (`stat`+`sha256`) o que a célula DECLARA, dentro do escopo;
- a alegação do leaf (`sha256`/`bytes`) é hint — divergir vira fault de AVISO;
- fora do escopo o harness NÃO ABRE nada (spy em `open`/`os.stat`);
- no replay, arquivo mudado/sumido = MISS `artifact_changed` + re-spawn;
- a medida do harness NUNCA entra no `output_json` (que flui pro `${ref}`).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import artifact as artifacts
from lohra.workflow.artifact import ArtifactScope
from lohra.workflow.budget import Budget
from lohra.workflow.cache import MISS_ARTIFACT_CHANGED, NodeCache
from lohra.workflow.cache_preview import preview_resume
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.sandbox import WorkflowPolicy
from lohra.workflow.schema import validate_spec
from tests.test_loop import _text_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


class _Client:
    """Answers with whatever the test scripted, counting every leaf spawned."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        answer = self._answers[min(self.calls - 1, len(self._answers) - 1)]
        return _text_response(answer)

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


def _core(db, client: _Client) -> OrchestrationCore:
    return OrchestrationCore(
        db,
        lambda: Agent(
            model="claude-opus-4-8", provider=get_provider_profile("anthropic"), client=client
        ),
    )


_SPEC: dict[str, Any] = {
    "meta": {"name": "artifacts", "version": 1},
    "nodes": [
        {
            "id": "writer",
            "type": "agent",
            "prompt": "write the report",
            "schema_ref": "artifact_manifest",
        }
    ],
}


def _run(
    db,
    run_id: str,
    client: _Client,
    scope: ArtifactScope | None,
    events: list[dict[str, Any]] | None = None,
    spec: dict[str, Any] | None = None,
):
    core = _core(db, client)
    try:
        return WorkflowEngine(
            core,
            budget=Budget(),
            cache=NodeCache(db, run_id),
            run_id=run_id,
            artifact_scope=scope,
            on_audit=events.append if events is not None else None,
        ).run(validate_spec(spec or _SPEC), {})
    finally:
        core.shutdown()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(path: Path, **claims: Any) -> str:
    return json.dumps({"path": str(path), **claims})


def _cell(db, run_id: str, node_id: str = "writer") -> dict[str, Any]:
    chash = db.cache_hashes_for_node(run_id, node_id)[0]
    return db.cache_get(run_id, chash)


def _artifact_of(db, run_id: str, node_id: str = "writer") -> list[dict[str, Any]]:
    return json.loads(_cell(db, run_id, node_id)["artifact_json"])


def _events(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event["event_type"] == event_type]


@pytest.fixture
def project(tmp_path):
    """An operator-allowed root (`fs_allow`), with one artifact already in it."""
    root = tmp_path / "project"
    root.mkdir()
    target = root / "report.md"
    target.write_text("the first draft\n", encoding="utf-8")
    policy = WorkflowPolicy(fs_allow=(str(root),))
    return target, ArtifactScope.of(None, policy)


# --- (i) dentro do fs_allow: verified, com o sha REAL -----------------------


def test_a_manifest_inside_fs_allow_is_measured_and_stored_as_verified(db, project):
    target, scope = project
    client = _Client([_manifest(target)])
    result = _run(db, "run-1", client, scope)

    assert result.status == "complete", result.faults
    row = _cell(db, "run-1")
    assert row["artifact_verification"] == artifacts.VERIFIED
    entry = _artifact_of(db, "run-1")[0]
    assert entry["sha256"] == _sha256(target)  # the harness's own measurement
    assert entry["bytes"] == target.stat().st_size
    assert entry["path"] == str(target)


def test_the_reserved_schema_needs_no_schemas_block_and_is_the_builtin_shape():
    """An author references the name and defines nothing (E5): the validator has
    to accept that, or the one schema they must NOT write is the one it demands."""
    spec = validate_spec(_SPEC)
    assert spec.schemas == {}
    from lohra.workflow.nodes import resolve_schema

    resolved = resolve_schema(spec.schemas, spec.nodes[0].fields)
    assert resolved is artifacts.MANIFEST_SCHEMA
    assert artifacts.is_manifest_schema(resolved)


# --- (ii) alegação divergente: fault de AVISO, célula com a medida real -----


def test_a_leaf_lying_about_the_hash_gets_a_warning_fault_not_a_dead_node(db, project):
    target, scope = project
    client = _Client([_manifest(target, sha256="0" * 64, bytes=999_999)])
    result = _run(db, "run-1", client, scope)

    # The NODE survives — the file was written; only the claim about it was
    # wrong, and killing the node would throw away work the harness can describe
    # correctly. The RUN is `degraded`, like every other fault: a leaf that
    # misreports what it produced is exactly the thing a status must not hide.
    assert result.status == "degraded"
    assert result.outputs["writer"] is not None
    assert result.null_count == 0
    assert any("claim not trusted" in fault for fault in result.faults), result.faults
    assert any(fault.startswith("writer: artifact") for fault in result.faults)
    # ...and the cell carries what the HARNESS measured, not what the leaf said.
    entry = _artifact_of(db, "run-1")[0]
    assert entry["sha256"] == _sha256(target) != "0" * 64
    assert entry["bytes"] == target.stat().st_size


# --- (x) a medida do harness NUNCA entra no output_json ---------------------


def test_the_harness_measurement_never_reaches_output_json(db, project):
    """`output_json` is what flows into a downstream `${ref}`. The harness's own
    hash must not be smuggled in there — the leaf's word stays the leaf's word."""
    target, scope = project
    # The leaf CLAIMS a wrong hash: if the store rewrote the payload with its
    # own measurement, the real hash would show up in output_json.
    client = _Client([_manifest(target, sha256="0" * 64)])
    _run(db, "run-1", client, scope)

    row = _cell(db, "run-1")
    payload = json.loads(row["output_json"])
    assert payload["sha256"] == "0" * 64  # verbatim, as the leaf said it
    assert _sha256(target) not in row["output_json"]
    assert _sha256(target) in row["artifact_json"]  # ...it lives in the sidecar


# --- (iii) fora do escopo: unverifiable, e NADA é aberto --------------------


def test_a_path_outside_the_scope_is_unverifiable_and_never_opened(db, tmp_path, monkeypatch):
    """The v4 case: a leaf wrote into the user's project through an
    operator-enabled shell. The harness may not look there, so it says
    `unverifiable` — and, crucially, it does not even stat it."""
    outside = tmp_path / "elsewhere" / "secret.md"
    outside.parent.mkdir()
    outside.write_text("not ours\n", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    scope = ArtifactScope.of(allowed, None)

    touched: list[str] = []
    real_open, real_stat, real_realpath = open, os.stat, os.path.realpath

    def spy_open(file, *args, **kwargs):
        touched.append(str(file))
        return real_open(file, *args, **kwargs)

    def spy_stat(path, *args, **kwargs):
        touched.append(str(path))
        return real_stat(path, *args, **kwargs)

    def spy_realpath(path, *args, **kwargs):
        touched.append(str(path))
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(artifacts, "open", spy_open, raising=False)
    monkeypatch.setattr(os, "stat", spy_stat)
    monkeypatch.setattr(os.path, "realpath", spy_realpath)

    record = artifacts.verify_output({"path": str(outside)}, scope)

    assert record is not None
    assert record.verification == artifacts.UNVERIFIABLE
    assert record.entries[0]["status"] == artifacts.UNVERIFIABLE
    assert "sha256" not in record.entries[0]
    assert not [seen for seen in touched if str(outside) in seen]


def test_a_relative_path_is_unverifiable_because_the_leaf_cwd_is_unknowable(tmp_path):
    scope = ArtifactScope.of(tmp_path, None)
    record = artifacts.verify_output({"path": "docs/report.md"}, scope)
    assert record is not None and record.verification == artifacts.UNVERIFIABLE


def test_a_symlink_pointing_out_of_the_scope_is_refused(tmp_path):
    inside, outside = tmp_path / "in", tmp_path / "out"
    inside.mkdir()
    outside.mkdir()
    real = outside / "secret.txt"
    real.write_text("nope", encoding="utf-8")
    link = inside / "link.txt"
    link.symlink_to(real)

    record = artifacts.verify_output({"path": str(link)}, ArtifactScope.of(inside, None))
    assert record is not None and record.verification == artifacts.UNVERIFIABLE


# --- (iv)/(v)/(vi) o replay: intacto = hit, mudado/sumido = miss + re-spawn -


def _run_tree(tmp_path) -> tuple[Path, Path]:
    """The service's real layout: `runs/<run_id>/work-{fence}` under one tree.

    The DISCRIMINATOR of these tests: the scope has to be the RUN's tree, not
    this acquisition's scratch. A resume owns `work-2` while the cell it is
    replaying declared a file under `work-1` — a per-acquisition scope would
    answer `unverifiable` for every scratch artifact on the very first resume
    and the whole feature would only ever fire for `fs_allow` roots.
    """
    tree = tmp_path / "runs" / "run-1"
    (tree / "work-1").mkdir(parents=True)
    (tree / "work-2").mkdir(parents=True)
    return tree, tree / "work-1"


def test_a_resume_with_the_file_intact_replays_without_spawning(db, tmp_path):
    tree, work = _run_tree(tmp_path)
    target = work / "out.md"
    target.write_text("done\n", encoding="utf-8")
    scope = ArtifactScope.of(tree, None)  # built the way service.py builds it

    first = _Client([_manifest(target)])
    _run(db, "run-1", first, scope)
    assert first.calls == 1

    resume_events: list[dict[str, Any]] = []
    second = _Client([_manifest(target)])
    result = _run(db, "run-1", second, scope, resume_events)

    assert second.calls == 0  # replayed: no leaf spawned at all
    assert result.status == "complete"
    replayed = _events(resume_events, "cache.replayed")
    assert len(replayed) == 1
    assert replayed[0]["data"]["artifact"] == artifacts.VERIFIED
    assert not _events(resume_events, "cache.missed")


def test_a_mutated_file_turns_the_hit_into_an_artifact_changed_miss(db, tmp_path):
    tree, work = _run_tree(tmp_path)
    target = work / "out.md"
    target.write_text("done\n", encoding="utf-8")
    scope = ArtifactScope.of(tree, None)

    _run(db, "run-1", _Client([_manifest(target)]), scope)
    # ...the very thing the investigation caught: a live leaf rewrote it later.
    target.write_text("done, and then some more\n", encoding="utf-8")

    resume_events: list[dict[str, Any]] = []
    second = _Client([_manifest(target)])
    result = _run(db, "run-1", second, scope, resume_events)

    missed = _events(resume_events, "cache.missed")
    assert len(missed) == 1
    assert missed[0]["data"]["reason"] == MISS_ARTIFACT_CHANGED
    assert missed[0]["data"]["artifact"] == artifacts.CHANGED
    assert second.calls == 1  # re-spawned: the cell was NOT replayed
    assert result.status == "complete"
    # ...and the fresh cell carries the NEW measurement.
    assert _artifact_of(db, "run-1")[0]["sha256"] == _sha256(target)


def test_a_removed_file_is_the_same_miss_and_says_missing(db, tmp_path):
    tree, work = _run_tree(tmp_path)
    target = work / "out.md"
    target.write_text("done\n", encoding="utf-8")
    scope = ArtifactScope.of(tree, None)

    _run(db, "run-1", _Client([_manifest(target)]), scope)
    target.unlink()

    resume_events: list[dict[str, Any]] = []
    second = _Client([_manifest(target)])
    _run(db, "run-1", second, scope, resume_events)

    missed = _events(resume_events, "cache.missed")
    assert len(missed) == 1
    assert missed[0]["data"]["reason"] == MISS_ARTIFACT_CHANGED
    assert missed[0]["data"]["artifact"] == artifacts.MISSING
    assert second.calls == 1


def test_an_unverifiable_cell_replays_with_a_note_instead_of_re_paying(db, tmp_path):
    """"We may not look" is not evidence of change: inventing a miss there would
    re-pay for every out-of-scope artifact on every resume."""
    outside = tmp_path / "project" / "report.md"
    outside.parent.mkdir()
    outside.write_text("theirs\n", encoding="utf-8")
    scope = ArtifactScope.of(tmp_path / "runs" / "run-1", None)

    _run(db, "run-1", _Client([_manifest(outside)]), scope)
    assert _cell(db, "run-1")["artifact_verification"] == artifacts.UNVERIFIABLE

    outside.write_text("theirs, rewritten\n", encoding="utf-8")
    resume_events: list[dict[str, Any]] = []
    second = _Client([_manifest(outside)])
    _run(db, "run-1", second, scope, resume_events)

    assert second.calls == 0  # replayed
    replayed = _events(resume_events, "cache.replayed")
    assert replayed[0]["data"]["artifact"] == artifacts.UNVERIFIABLE


def test_a_cell_with_no_manifest_replays_exactly_as_it_always_did(db, tmp_path):
    plain = {
        "meta": {"name": "artifacts", "version": 1},
        "nodes": [{"id": "writer", "type": "agent", "prompt": "just answer"}],
    }
    scope = ArtifactScope.of(tmp_path, None)
    _run(db, "run-1", _Client(["R"]), scope, spec=plain)
    assert _cell(db, "run-1")["artifact_verification"] is None

    resume_events: list[dict[str, Any]] = []
    second = _Client(["R"])
    _run(db, "run-1", second, scope, resume_events, spec=plain)
    assert second.calls == 0
    assert "artifact" not in _events(resume_events, "cache.replayed")[0]["data"]


# --- (vii) o cache_preview anuncia antes de pagar ---------------------------


def test_the_cache_preview_announces_artifact_changed_before_anything_spawns(db, tmp_path):
    tree, work = _run_tree(tmp_path)
    target = work / "out.md"
    target.write_text("done\n", encoding="utf-8")
    scope = ArtifactScope.of(tree, None)

    _run(db, "run-1", _Client([_manifest(target)]), scope)
    chash = db.cache_hashes_for_node("run-1", "writer")[0]
    db.cache_cost_put("run-1", chash, 1_000, 200, reasoning=50)

    parsed = validate_spec(_SPEC)
    clean = preview_resume(db, "run-1", parsed, {}, artifact_scope=scope)
    assert clean["replay"] == 1 and clean["invalidate"] == 0

    target.write_text("done, and then some more\n", encoding="utf-8")
    preview = preview_resume(db, "run-1", parsed, {}, artifact_scope=scope)
    assert preview["replay"] == 0
    assert preview["invalidate"] == 1
    assert preview["invalidated"] == [
        {"node_id": "writer", "reason": MISS_ARTIFACT_CHANGED}
    ]
    assert preview["tokens_to_repay"] == 1_250
    assert preview["never_completed"] == 0


def test_the_preview_reads_nothing_and_writes_nothing(db, tmp_path):
    tree, work = _run_tree(tmp_path)
    target = work / "out.md"
    target.write_text("done\n", encoding="utf-8")
    scope = ArtifactScope.of(tree, None)
    _run(db, "run-1", _Client([_manifest(target)]), scope)
    before = _cell(db, "run-1")

    target.write_text("mutated\n", encoding="utf-8")
    preview_resume(db, "run-1", validate_spec(_SPEC), {}, artifact_scope=scope)

    assert _cell(db, "run-1") == before  # the row is untouched by a preview


# --- (viii) migração idempotente -------------------------------------------


_LEGACY_CACHE_DDL = """
CREATE TABLE workflow_node_cache (
    content_hash TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    output_json  TEXT,
    status       TEXT NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (run_id, content_hash)
);
"""


def test_a_database_created_before_the_columns_migrates_and_keeps_its_cells(tmp_path):
    """The #34 pattern: an ALTER guarded by the OperationalError it raises when
    the column is already there. A pre-E4 database has to open, keep every cell
    it had, read the new columns as NULL, and take a new measurement fine."""
    path = str(tmp_path / "state.db")
    legacy = sqlite3.connect(path)
    legacy.executescript(_LEGACY_CACHE_DDL)
    legacy.execute(
        "INSERT INTO workflow_node_cache "
        "(content_hash, run_id, node_id, output_json, status, updated_at) "
        "VALUES ('h1', 'run-1', 'old', '\"kept\"', 'complete', 1.0)"
    )
    legacy.commit()
    legacy.close()

    database = SessionDB(path)
    try:
        row = database.cache_get("run-1", "h1")
        assert row["output_json"] == '"kept"'
        assert row["artifact_verification"] is None  # never written = never measured
        assert NodeCache(database, "run-1").get_with_artifact("h1") == (True, "kept", None)
        # ...and a NEW cell on the migrated table stores its measurement.
        target = tmp_path / "made.md"
        target.write_text("fresh\n", encoding="utf-8")
        database.cache_put_with_cost(
            "run-1", "h2", "new", '{"path": "x"}', "complete",
            artifact=(artifacts.VERIFIED, json.dumps([{"path": str(target)}])),
        )
        assert database.cache_get("run-1", "h2")["artifact_verification"] == artifacts.VERIFIED
    finally:
        database.close()
    # Re-opening runs the same migration again: idempotent, not an error.
    again = SessionDB(path)
    try:
        assert again.cache_get("run-1", "h1")["output_json"] == '"kept"'
    finally:
        again.close()


# --- (ix) o autor não pode redefinir o nome reservado -----------------------


@pytest.mark.parametrize("name", ["artifact_manifest", "artifact_manifests"])
def test_an_author_redefining_a_reserved_schema_name_is_rejected(name):
    spec = {
        "meta": {"name": "hijack", "version": 1},
        "schemas": {name: {"type": "object", "properties": {"path": {"type": "number"}}}},
        "nodes": [{"id": "a", "type": "agent", "prompt": "go", "schema_ref": name}],
    }
    error = validate_spec(spec)
    assert not hasattr(error, "nodes"), "the spec must NOT validate"
    rules = {issue.rule for issue in error.issues}
    assert "schema_reserved" in rules
    issue = next(i for i in error.issues if i.rule == "schema_reserved")
    assert issue.example == "schema_ref: artifact_manifest"


def test_the_reserved_name_wins_even_in_a_spec_dict_nobody_validated():
    """Defence in depth: the validator refuses the redefinition, so the only way
    a local entry reaches ``resolve_schema`` is a spec nobody validated — exactly
    where the reserved meaning has to hold."""
    from lohra.workflow.nodes import resolve_schema

    hijacked = {"artifact_manifest": {"type": "string"}}
    assert resolve_schema(hijacked, {"schema_ref": "artifact_manifest"}) is (
        artifacts.MANIFEST_SCHEMA
    )


# --- a lista, e os limites ---------------------------------------------------


def test_the_list_form_measures_every_entry(db, tmp_path):
    tree, work = _run_tree(tmp_path)
    first, second = work / "a.md", work / "b.md"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    scope = ArtifactScope.of(tree, None)
    answer = json.dumps([{"path": str(first)}, {"path": str(second)}])

    spec = json.loads(json.dumps(_SPEC))
    spec["nodes"][0]["schema_ref"] = "artifact_manifests"
    _run(db, "run-1", _Client([answer]), scope, spec=spec)

    entries = _artifact_of(db, "run-1")
    assert [entry["sha256"] for entry in entries] == [_sha256(first), _sha256(second)]

    # One of the two moving on is enough to refuse the replay.
    second.write_text("two, revised\n", encoding="utf-8")
    events: list[dict[str, Any]] = []
    client = _Client([answer])
    _run(db, "run-1", client, scope, events, spec=spec)
    assert _events(events, "cache.missed")[0]["data"]["reason"] == MISS_ARTIFACT_CHANGED
    assert client.calls == 1


def test_a_size_claim_alone_can_diverge(tmp_path):
    """The v4 shape verbatim: the cell said "25.091 bytes" and the file is
    25.583. A claim with no hash at all still gets cross-checked."""
    made = tmp_path / "report.md"
    made.write_text("four\n", encoding="utf-8")
    record = artifacts.verify_output(
        {"path": str(made), "bytes": 25_091}, ArtifactScope.of(tmp_path, None)
    )
    assert record is not None and record.verification == artifacts.VERIFIED
    assert "25091 bytes" in record.divergences[0]
    assert f"measured {made.stat().st_size}" in record.divergences[0]


def test_an_entry_that_left_the_scope_is_skipped_by_the_recheck_not_called_changed(tmp_path):
    """An operator who withdrew an ``fs_allow`` root did not change the file."""
    made = tmp_path / "report.md"
    made.write_text("here\n", encoding="utf-8")
    entries = artifacts.verify_output(
        {"path": str(made)}, ArtifactScope.of(tmp_path, None)
    ).as_entry_list()

    verdict = artifacts.recheck(entries, ArtifactScope())  # no roots any more
    assert verdict.stale is False and verdict.status == artifacts.UNVERIFIABLE


def test_a_manifest_past_the_entry_cap_measures_the_first_ones_and_says_so(tmp_path):
    scope = ArtifactScope.of(tmp_path, None)
    claims = []
    for index in range(artifacts.MAX_ENTRIES + 5):
        made = tmp_path / f"f{index}.md"
        made.write_text(str(index), encoding="utf-8")
        claims.append({"path": str(made)})

    record = artifacts.verify_output(claims, scope)
    assert record is not None
    assert len(record.entries) == artifacts.MAX_ENTRIES
    assert any("only the first" in note for note in record.divergences)


def test_a_file_past_the_hash_cap_is_unverifiable_rather_than_a_stall(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "MAX_HASH_BYTES", 4)
    big = tmp_path / "big.bin"
    big.write_bytes(b"0123456789")
    record = artifacts.verify_output({"path": str(big)}, ArtifactScope.of(tmp_path, None))
    assert record is not None and record.verification == artifacts.UNVERIFIABLE


def test_a_scope_with_no_roots_verifies_nothing(tmp_path):
    made = tmp_path / "x.md"
    made.write_text("x", encoding="utf-8")
    record = artifacts.verify_output({"path": str(made)}, ArtifactScope())
    assert record is not None and record.verification == artifacts.UNVERIFIABLE


def test_a_sibling_directory_sharing_a_prefix_is_not_inside_the_scope(tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run-evil").mkdir()
    sneaky = tmp_path / "run-evil" / "x.md"
    sneaky.write_text("x", encoding="utf-8")
    scope = ArtifactScope.of(tmp_path / "run", None)
    assert not scope.contains(str(sneaky))


def test_an_output_that_is_not_a_manifest_stores_no_measurement(db, tmp_path):
    """The schema says manifest but the leaf answered something else: nothing to
    measure, and the cell stores no verdict rather than a made-up one."""
    scope = ArtifactScope.of(tmp_path, None)
    assert artifacts.verify_output({"note": "no path here"}, scope) is None
    assert artifacts.verify_output("prose", scope) is None
