"""#65 — duas células do MESMO run declarando o MESMO `path` do manifesto.

O experimento #62 (`docs/history/reviews/2026-09-03-exp62-fanout-shared-fs.md`)
achou dois efeitos que o manifesto de 0.0.21 não previa:

1. a colisão de path entre células IRMÃS é indistinguível, no fault, de "o leaf
   mentiu sobre o hash" ou "um terceiro mutou o artefato" — o texto diagnostica
   a causa errada e o autor não recebe o remédio ("separe os paths");
2. `c3-jitter` rep 3: os writers NÃO se sobrepuseram, o run estava CERTO, mas o
   recheck do resume viu o arquivo mudado (pelo irmão, legitimamente),
   invalidou a célula do primeiro, re-spawnou — e o re-spawn re-anexou,
   deixando `A\\nB\\nA\\n` no disco. **O recheck corrompeu um run correto.**

Estes testes fixam o remédio: colisão chaveada por PATH (nunca por sha — em
C3-barrier as duas células mediram o MESMO sha final, que é a assinatura do dano,
não da segurança), advisory nomeando as duas células, e um replay que NÃO
re-spawna quando a mutação é explicada por um irmão do mesmo run. Mutação
EXTERNA continua `artifact_changed` + re-spawn (decisão do dono, 0.0.21 #45 E4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import artifact as artifacts
from lohra.workflow.artifact import ArtifactScope
from lohra.workflow.artifact_paths import RunPaths
from lohra.workflow.budget import Budget
from lohra.workflow.cache import MISS_ARTIFACT_CHANGED, NodeCache
from lohra.workflow.cache_preview import preview_resume
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import _text_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


class _AppendingClient:
    """A leaf that really writes before it answers — the RMW of exp62, minus the
    read (the point here is the manifest, not the lost update).

    Every call APPENDS its scripted line to the path it will then declare, so
    the file's history is the run's history and a re-spawn shows up on disk as a
    duplicated line — exactly the damage `c3-jitter` rep 3 recorded."""

    def __init__(self, lines: list[str], paths: list[Path]) -> None:
        self._lines = list(lines)
        self._paths = list(paths)
        self.calls = 0

    def create(self, **kwargs):
        index = self.calls
        self.calls += 1
        line = self._lines[min(index, len(self._lines) - 1)]
        target = self._paths[min(index, len(self._paths) - 1)]
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line)
        return _text_response(json.dumps({"path": str(target)}))

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


def _core(db, client) -> OrchestrationCore:
    return OrchestrationCore(
        db,
        lambda: Agent(
            model="claude-opus-4-8", provider=get_provider_profile("anthropic"), client=client
        ),
    )


def _run(db, run_id: str, client, scope, spec, events=None):
    core = _core(db, client)
    try:
        return WorkflowEngine(
            core,
            budget=Budget(),
            cache=NodeCache(db, run_id),
            run_id=run_id,
            artifact_scope=scope,
            on_audit=events.append if events is not None else None,
        ).run(validate_spec(spec), {})
    finally:
        core.shutdown()


def _events(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event["event_type"] == event_type]


def _tree(tmp_path) -> tuple[Path, Path]:
    """The service's real layout: `runs/<run_id>/work-{fence}` under one tree."""
    tree = tmp_path / "runs" / "run-1"
    (tree / "work-1").mkdir(parents=True)
    return tree, tree / "work-1"


def _chain(stages: int = 2) -> dict[str, Any]:
    """ONE item through N sequential stages — the deterministic `c3-jitter`.

    Sequential on purpose: rep 3's damage never needed a race. Stage 0 stores the
    sha of the file as IT left it, stage 1 legitimately appends to the same file,
    and the resume's recheck of stage 0 then sees a file that "moved on"."""
    return {
        "meta": {"name": "shared-path", "version": 1},
        "nodes": [
            {
                "id": "chain",
                "type": "pipeline",
                "items": ["x"],
                "stages": [
                    {"prompt": f"stage {i} on ${{item}}", "schema_ref": "artifact_manifest"}
                    for i in range(stages)
                ],
            }
        ],
    }


def _fanout() -> dict[str, Any]:
    """Two ITEMS through one stage — two sibling cells, one path."""
    return {
        "meta": {"name": "shared-path", "version": 1},
        "nodes": [
            {
                "id": "fan",
                "type": "pipeline",
                "items": ["a", "b"],
                "stages": [
                    {"prompt": "write for ${item}", "schema_ref": "artifact_manifest"}
                ],
            }
        ],
    }


# --- (1) a colisão nomeia o IRMÃO, não o hash ------------------------------


def test_two_cells_of_one_run_on_one_path_get_an_advisory_naming_both(db, tmp_path):
    """The fault has to say "a sibling of this run declared this path too", with
    the remedy. Today nothing compares two DECLARATIONS: the only thing that can
    fire is the claim-vs-measurement divergence, whose text blames the leaf."""
    tree, work = _tree(tmp_path)
    shared = work / "shared.txt"
    client = _AppendingClient(["A\n", "B\n"], [shared, shared])

    result = _run(db, "run-1", client, ArtifactScope.of(tree, None), _fanout())

    assert result.status == "complete", result.faults
    collision = [
        fault
        for fault in result.faults
        if str(shared) in fault and "sibling" in fault
    ]
    assert collision, result.faults
    # Both cells are named — the author has to know WHICH two collided.
    assert "fan#0#0" in collision[0] and "fan#1#0" in collision[0], collision
    # ...and it is an ADVISORY: two writers on one path is a warning about the
    # spec's SHAPE that the harness cannot prove is damage, not a dead run.
    assert collision[0] in result.advisory_faults
    # ...and nothing about a lying leaf, which is the wrong diagnosis (#62 §2.3).
    assert "claimed sha256" not in collision[0]


# --- (2) o resume NÃO re-spawna o que o irmão mudou -------------------------


def test_a_resume_does_not_respawn_a_cell_a_sibling_legitimately_changed(db, tmp_path):
    """`c3-jitter` rep 3, deterministic: the run was CORRECT and the resume's
    recheck duplicated a write. The file must come out of the resume byte for
    byte as the run left it."""
    tree, work = _tree(tmp_path)
    shared = work / "shared.txt"

    first = _AppendingClient(["A\n", "B\n"], [shared, shared])
    _run(db, "run-1", first, ArtifactScope.of(tree, None), _chain())
    assert first.calls == 2
    assert shared.read_text(encoding="utf-8") == "A\nB\n"

    events: list[dict[str, Any]] = []
    second = _AppendingClient(["A\n", "B\n"], [shared, shared])
    result = _run(db, "run-1", second, ArtifactScope.of(tree, None), _chain(), events)

    assert second.calls == 0, "a sibling's legitimate write must not re-spawn"
    assert shared.read_text(encoding="utf-8") == "A\nB\n"  # NOT "A\nB\nA\n"
    assert result.status == "complete", result.faults
    assert not [
        event
        for event in _events(events, "cache.missed")
        if event["data"].get("reason") == MISS_ARTIFACT_CHANGED
    ]
    # ...and the kept replay says WHY it was kept, with the path.
    kept = [
        fault for fault in result.faults if str(shared) in fault and "sibling" in fault
    ]
    assert kept, result.faults
    assert kept[0] in result.advisory_faults


# --- (3) o controle: mutação EXTERNA continua re-spawnando ------------------


def test_an_outside_mutation_still_invalidates_when_the_path_is_not_shared(db, tmp_path):
    """The owner's decision "mismatch = re-spawn" is unchanged for a genuine
    mismatch. Distinct paths per stage, and a THIRD party rewrites stage 0's."""
    tree, work = _tree(tmp_path)
    one, two = work / "one.txt", work / "two.txt"
    first = _AppendingClient(["A\n", "B\n"], [one, two])
    _run(db, "run-1", first, ArtifactScope.of(tree, None), _chain())
    assert first.calls == 2

    one.write_text("somebody else was here\n", encoding="utf-8")

    events: list[dict[str, Any]] = []
    second = _AppendingClient(["A\n", "B\n"], [one, two])
    _run(db, "run-1", second, ArtifactScope.of(tree, None), _chain(), events)

    missed = [
        event
        for event in _events(events, "cache.missed")
        if event["data"].get("reason") == MISS_ARTIFACT_CHANGED
    ]
    assert missed, "an external mutation is still artifact_changed"
    assert second.calls >= 1


def test_a_missing_file_is_still_stale_even_on_a_shared_path(tmp_path):
    """A sibling's write explains different CONTENT, never a file that is not
    there — and replaying a description of an absent file is the original lie."""
    tree = tmp_path / "runs" / "run-1"
    tree.mkdir(parents=True)
    gone = tree / "gone.txt"
    entries = [{"path": str(gone), "status": artifacts.VERIFIED, "sha256": "0" * 64,
                "bytes": 3}]
    shared = RunPaths({str(gone): {"a", "b"}})

    verdict = artifacts.recheck(entries, ArtifactScope.of(tree, None), shared)

    assert verdict.stale is True
    assert verdict.status == artifacts.MISSING


def test_a_private_path_still_invalidates_a_cell_that_also_shares_one(db, tmp_path):
    """Per ENTRY, never per cell: a cell declaring a shared file AND its own
    still re-spawns when ITS file moved. The suppression is about the path whose
    change has an owner, not about the cell that happened to touch one."""
    tree = tmp_path / "runs" / "run-1"
    tree.mkdir(parents=True)
    shared, private = tree / "shared.txt", tree / "private.txt"
    shared.write_text("s\n", encoding="utf-8")
    private.write_text("p\n", encoding="utf-8")
    scope = ArtifactScope.of(tree, None)
    entries = [
        dict(artifacts.measure(str(shared), scope)),
        dict(artifacts.measure(str(private), scope)),
    ]
    index = RunPaths({str(shared): {"a", "b"}, str(private): {"a"}})

    shared.write_text("s, by the sibling\n", encoding="utf-8")
    kept = artifacts.recheck(entries, scope, index)
    assert kept.stale is False and kept.status == artifacts.SHARED_PATH

    private.write_text("p, by nobody we know\n", encoding="utf-8")
    assert artifacts.recheck(entries, scope, index).stale is True


def test_the_index_is_keyed_by_node_id_so_a_node_never_collides_with_itself():
    """An ``identity_changed`` cell leaves its old row in the table forever.
    Keyed by content hash, a node's OWN previous version would look like a
    sibling and suppress a genuine invalidation."""
    index = RunPaths()
    assert index.claim("writer", ["/p"]) == []
    assert index.claim("writer", ["/p"]) == []  # re-stored: still nobody else
    assert index.is_shared("/p") is False

    collisions = index.claim("other", ["/p"])
    assert collisions == [("/p", ("writer",))]
    assert index.is_shared("/p") is True
    assert index.owners_of("/p") == ("other", "writer")
    # ...and ONE advisory per path for the whole run, however wide the fan-out.
    assert index.claim("third", ["/p"]) == []
    assert index.owners_of("/p") == ("other", "third", "writer")


def test_the_index_survives_an_unreadable_sidecar_and_a_broken_cache():
    """Not knowing who declared what degrades to today's behaviour (every
    mismatch re-spawns) — never to a run taken down inside a cache lookup."""

    class _Corrupt:
        def artifact_rows(self):
            return [("a", "{not json"), ("b", '[{"path": "/p"}]'), ("c", '"nope"')]

    class _Broken:
        def artifact_rows(self):
            raise RuntimeError("no db")

    assert RunPaths.load(_Corrupt()).owners_of("/p") == ("b",)
    assert RunPaths.load(_Broken()).is_shared("/p") is False


def test_the_preview_does_not_announce_an_invalidation_the_engine_will_not_do(db, tmp_path):
    """The preview exists to say what the resume will cost BEFORE it runs. An
    engine that keeps a replay must not be contradicted here, or the author
    confirms a re-payment that never happens."""
    tree, work = _tree(tmp_path)
    shared = work / "shared.txt"
    _run(db, "run-1", _AppendingClient(["A\n", "B\n"], [shared, shared]),
         ArtifactScope.of(tree, None), _chain())

    preview = preview_resume(
        db, "run-1", validate_spec(_chain()), {},
        artifact_scope=ArtifactScope.of(tree, None),
    )
    assert preview["invalidate"] == 0
    assert preview["replay"] == 2
    assert preview["invalidated"] == []
