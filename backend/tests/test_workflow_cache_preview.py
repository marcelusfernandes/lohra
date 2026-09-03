"""#44 épico 2 — o raio do estrago de um resume, ANTES de qualquer spawn.

A investigação achou uma invalidação legítima já enfileirada e invisível: o pivô
trocou o `model` de `final_certification`, esse nó TINHA célula, e o próximo
resume ia re-pagar ~2,13M tokens como um `cache.missed` mudo. Estes testes são a
prova de que agora alguém avisa — e, o mais importante, que a recomputação da
chave é IGUAL à que o engine grava (round-trip por um engine de verdade).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import (
    MISS_IDENTITY_CHANGED,
    MISS_IDENTITY_CHANGED_OR_SIBLING,
    NodeCache,
)
from lohra.workflow.cache_preview import preview_resume
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from lohra.workflow.tiers import Tier, TierMap
from tests.test_workflow_token_budget import _core

# The operator's map — the SAME object both the engine and the preview resolve
# `tier: big` through, so a routed node's identity is pinned end to end.
_TIERS = TierMap({"big": Tier(model="claude-opus-4-8")})


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _responder(prompt: str) -> str:
    """One oracle for every rigor shape: each node type's forced JSON, so a run
    over all of them COMPLETES and leaves a cell behind."""
    if '"refuted"' in prompt:
        return json.dumps({"refuted": False, "reason": "holds"})
    if '"score"' in prompt:
        return json.dumps({"score": 8, "rationale": "good"})
    if '"ok"' in prompt:
        return json.dumps({"ok": True, "feedback": ""})
    if '"complete"' in prompt:
        return json.dumps({"complete": True, "missing": []})
    return "R"


# Every node type this preview claims to cover, in one spec.
_COVERED: dict[str, Any] = {
    "meta": {"name": "preview", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "plan it"},
        {"id": "par", "type": "parallel", "branches": ["one ${a}", "two"]},
        {"id": "ver", "type": "verify", "finding": "${a}", "skeptics": 2},
        {
            "id": "jud",
            "type": "judge_panel",
            "attempts": ["try ${a}", "try harder"],
            "judges": 1,
            "synthesize": "merge them",
        },
        {
            "id": "loop",
            "type": "loop_until_dry",
            "body": {"prompt": "harvest ${a}"},
            "stop_after_k_empty": 1,
            "max_rounds": 2,
        },
        {
            "id": "gat",
            "type": "gate",
            "body": {"prompt": "draft ${a}"},
            "validator": "is it fine?",
        },
        {"id": "com", "type": "completeness_check", "task": "the task", "results": "${a}"},
        {"id": "tie", "type": "agent", "prompt": "routed ${a}", "tier": "big"},
        {"id": "chk", "type": "checkpoint", "prompt": "approve ${a}?"},
    ],
}
_ANSWERS = {"chk": "approved"}


def _run(db, run_id: str, spec_dict: dict, answers: dict | None = None, *, loader=None):
    core = _core(db, _responder)
    try:
        return WorkflowEngine(
            core,
            budget=Budget(),
            cache=NodeCache(db, run_id),
            run_id=run_id,
            tiers=_TIERS,
            checkpoint_answers=answers or {},
            loader=loader,
        ).run(validate_spec(spec_dict), {})
    finally:
        core.shutdown()


def _preview(db, run_id: str, spec_dict: dict, answers: dict | None = None, *, loader=None):
    return preview_resume(
        db, run_id, validate_spec(spec_dict), {},
        tiers=_TIERS, checkpoint_answers=answers or {}, loader=loader,
    )


# --- (i) the recomputed key IS the key the engine wrote ---------------------


def test_every_covered_node_type_recomputes_the_hash_the_engine_stored(db):
    """The discriminating test: a REAL engine writes the cells, then the preview
    recomputes the keys from the same spec. One byte of drift anywhere in the
    identity composition and a node shows up as an invalidation."""
    result = _run(db, "run-1", _COVERED, _ANSWERS)
    assert result.status == "complete", result.faults
    covered = [node["id"] for node in _COVERED["nodes"]]
    assert len(db.cache_hashes_for_node("run-1", "a")) == 1
    stored = {
        node_id for node_id in covered if db.cache_hashes_for_node("run-1", node_id)
    }
    assert stored == set(covered)  # every covered type really left a cell

    preview = _preview(db, "run-1", _COVERED, _ANSWERS)
    assert preview == {
        "replay": len(covered),
        "invalidate": 0,
        "never_completed": 0,
        "tokens_to_repay": 0,
        "invalidated": [],
    }


# --- (ii)/(iii) what a resume will replay, and what it will re-pay ----------


def test_a_resume_with_no_change_replays_everything(db):
    _run(db, "run-1", _COVERED, _ANSWERS)
    preview = _preview(db, "run-1", _COVERED, _ANSWERS)
    assert preview["replay"] == 9 and preview["invalidate"] == 0
    assert preview["never_completed"] == 0 and preview["tokens_to_repay"] == 0


def test_a_changed_model_invalidates_that_cell_and_names_the_price(db):
    # The `final_certification` case, in miniature: only `model` moved.
    spec = {
        "meta": {"name": "preview", "version": 1},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "plan it"},
            {"id": "cert", "type": "agent", "prompt": "certify ${a}", "model": "deepseek-v4"},
        ],
    }
    _run(db, "run-1", spec)
    chash = db.cache_hashes_for_node("run-1", "cert")[0]
    db.cache_cost_put("run-1", chash, 355_212, 31_607, cache_read=1_722_112, reasoning=17_451)

    pivoted = json.loads(json.dumps(spec))
    pivoted["nodes"][1]["model"] = "glm-5.3-flash"
    preview = _preview(db, "run-1", pivoted)
    assert preview["replay"] == 1  # `a` is untouched
    assert preview["invalidate"] == 1
    assert preview["invalidated"] == [{"node_id": "cert", "reason": MISS_IDENTITY_CHANGED}]
    assert preview["tokens_to_repay"] == 2_126_382  # all five meters
    assert "cost_unknown" not in preview


def test_a_node_that_never_completed_is_not_an_invalidation(db):
    spec = {
        "meta": {"name": "preview", "version": 1},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "plan it"},
            {"id": "b", "type": "agent", "prompt": "second"},
        ],
    }
    _run(db, "run-1", spec)
    db._connection.execute("DELETE FROM workflow_node_cache WHERE node_id = 'b'")
    db._connection.commit()
    preview = _preview(db, "run-1", spec)
    assert preview["replay"] == 1
    assert preview["never_completed"] == 1
    assert preview["invalidate"] == 0 and preview["tokens_to_repay"] == 0


def test_an_unpriced_invalidated_cell_is_named_instead_of_counted_as_free(db):
    spec = {
        "meta": {"name": "preview", "version": 1},
        "nodes": [{"id": "a", "type": "agent", "prompt": "plan it", "model": "m1"}],
    }
    _run(db, "run-1", spec)
    db._connection.execute("DELETE FROM workflow_node_cost WHERE run_id = 'run-1'")
    db._connection.commit()
    pivoted = {**spec, "nodes": [{**spec["nodes"][0], "model": "m2"}]}
    preview = _preview(db, "run-1", pivoted)
    assert preview["invalidate"] == 1
    assert preview["tokens_to_repay"] == 0
    assert preview["cost_unknown"] == ["a"]


def test_a_version_bump_re_keys_even_untouched_nodes(db):
    # H1, made visible: (name, version) namespaces EVERY cell of the run.
    spec = {
        "meta": {"name": "preview", "version": 1},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "plan it"},
            {"id": "b", "type": "agent", "prompt": "then ${a}"},
        ],
    }
    _run(db, "run-1", spec)
    bumped = {**spec, "meta": {"name": "preview", "version": 2}}
    preview = _preview(db, "run-1", bumped)
    assert preview["invalidate"] == 1  # `a` re-keys...
    assert preview["invalidated"][0]["node_id"] == "a"
    # ...and `b`, whose prompt interpolates a now-unknown output, is not guessed.
    assert preview["unknown"] == [{"node_id": "b", "why": "upstream_unknown"}]


# --- what the preview still refuses to claim -------------------------------


def test_fan_out_is_counted_and_a_template_it_cannot_load_is_not(db):
    """v2 recomputes the pipeline's cells (#61) — but a nested ref this process
    cannot resolve is still named, never counted as a free replay."""
    spec = {
        "meta": {"name": "preview", "version": 1},
        "nodes": [
            {
                "id": "p",
                "type": "pipeline",
                "items": ["x"],
                "stages": [{"prompt": "s ${item}"}],
            },
            {"id": "w", "type": "workflow", "ref": "some-template"},
        ],
    }
    preview = _preview(db, "run-1", spec)
    assert preview["replay"] == 0 and preview["invalidate"] == 0
    assert preview["never_completed"] == 1  # the pipeline's single cell
    assert preview["unknown"] == [{"node_id": "w", "why": "nested_template_unavailable"}]


def test_a_checkpoint_still_waiting_makes_its_downstream_unknown(db):
    spec = {
        "meta": {"name": "preview", "version": 1},
        "nodes": [
            {"id": "chk", "type": "checkpoint", "prompt": "approve?"},
            {"id": "after", "type": "agent", "prompt": "given ${chk}"},
        ],
    }
    preview = _preview(db, "run-1", spec)  # no answer in hand
    assert preview["never_completed"] == 1
    assert preview["unknown"] == [{"node_id": "after", "why": "upstream_unknown"}]
    # ...and WITH the answer the downstream node is computable again.
    answered = _preview(db, "run-1", spec, {"chk": "yes"})
    assert answered["never_completed"] == 2 and "unknown" not in answered


# --- (vii) the preview writes NOTHING ---------------------------------------


def _dump(db) -> list[tuple]:
    rows: list[tuple] = []
    for table in ("workflow_node_cache", "workflow_node_cost", "workflow_run_spend"):
        rows.extend(tuple(row) for row in db._connection.execute(f"SELECT * FROM {table}"))
    return sorted(rows)


def test_the_preview_never_writes_a_row(db):
    _run(db, "run-1", _COVERED, _ANSWERS)
    before = _dump(db)
    pivoted = json.loads(json.dumps(_COVERED))
    pivoted["nodes"][0]["model"] = "another-model"
    _preview(db, "run-1", pivoted, _ANSWERS)
    _preview(db, "run-1", _COVERED, _ANSWERS)
    assert _dump(db) == before


# --- (iv) the acceptance carries it ONLY on a resume ------------------------


def test_a_fresh_start_is_byte_identical_to_before(db, tmp_path):
    from tests.test_workflow_operability import _service

    svc = _service(db, tmp_path, _responder)
    try:
        spec = {
            "meta": {"name": "preview", "version": 1},
            "nodes": [{"id": "a", "type": "agent", "prompt": "plan it"}],
        }
        accepted = svc.start(spec, {})
        # No cache to diff against: a first run answers exactly what it always did.
        assert accepted == {"run_id": accepted["run_id"], "status": "started"}
        assert svc.status(accepted["run_id"], wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


def test_a_resume_reports_the_blast_radius_before_it_starts(db, tmp_path):
    from tests.test_workflow_operability import _service

    svc = _service(db, tmp_path, _responder)
    try:
        spec = {
            "meta": {"name": "preview", "version": 1},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "plan it"},
                {"id": "cert", "type": "agent", "prompt": "certify", "model": "deepseek-v4"},
            ],
        }
        run_id = svc.start(spec, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        pivoted = json.loads(json.dumps(spec))
        pivoted["nodes"][1]["model"] = "glm-5.3-flash"
        accepted = svc.start(pivoted, {}, resume_run_id=run_id)
        assert accepted["cache_preview"]["replay"] == 1
        assert accepted["cache_preview"]["invalidated"] == [
            {"node_id": "cert", "reason": MISS_IDENTITY_CHANGED}
        ]
        assert accepted["cache_preview"]["tokens_to_repay"] > 0
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


def test_a_preview_that_blows_up_never_stops_the_resume(db, tmp_path, monkeypatch):
    from tests.test_workflow_operability import _service

    svc = _service(db, tmp_path, _responder)
    try:
        spec = {
            "meta": {"name": "preview", "version": 1},
            "nodes": [{"id": "a", "type": "agent", "prompt": "plan it"}],
        }
        run_id = svc.start(spec, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        monkeypatch.setattr(
            "lohra.workflow.service.preview_resume",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        accepted = svc.start(spec, {}, resume_run_id=run_id)
        assert accepted == {"run_id": run_id, "status": "started"}  # launched anyway
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


# --- anti-drift: what the author is told matches what the author gets -------


def test_the_guidance_and_the_skill_both_name_the_preview_keys(db):
    from pathlib import Path

    from lohra.skills.store import SkillStore, builtin_root
    from lohra.workflow.tools import RUN_GUIDANCE

    spec = {
        "meta": {"name": "preview", "version": 1},
        "nodes": [{"id": "a", "type": "agent", "prompt": "plan it"}],
    }
    keys = set(_preview(db, "run-1", spec))
    skill = SkillStore(Path("/nonexistent-home"), builtin_roots=(builtin_root(),)).get(
        "workflow-authoring"
    )
    assert skill is not None
    for surface in (RUN_GUIDANCE, skill.body):
        assert "cache_preview" in surface
        assert MISS_IDENTITY_CHANGED in surface
        for key in keys:
            assert key in surface, key


def test_the_agent_really_receives_the_preview_from_the_tool(db, tmp_path):
    """The guidance tells the AGENT to read cache_preview — so the tool reply,
    not just the service dict, has to carry it."""
    from lohra.workflow.tools import WorkflowTool
    from tests.test_workflow_operability import _service

    svc = _service(db, tmp_path, _responder)
    try:
        tool = WorkflowTool(svc)
        spec = {
            "meta": {"name": "preview", "version": 1},
            "nodes": [{"id": "a", "type": "agent", "prompt": "plan it", "model": "m1"}],
        }
        run_id = json.loads(tool.run({"spec": spec}))["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        pivoted = {**spec, "nodes": [{**spec["nodes"][0], "model": "m2"}]}
        reply = json.loads(tool.run({"spec": pivoted, "resume_run_id": run_id}))
        assert reply["cache_preview"]["invalidated"] == [
            {"node_id": "a", "reason": MISS_IDENTITY_CHANGED}
        ]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


# --- v2 (#61): pipeline, cell by cell --------------------------------------

_PIPE: dict[str, Any] = {
    "meta": {"name": "preview", "version": 1},
    "nodes": [
        {
            "id": "pl",
            "type": "pipeline",
            "items": ["a", "b", "c"],
            "stages": ["draft ${item}", "polish ${stage.result}"],
        }
    ],
}


def _pipe(items=None, first="draft ${item}"):
    spec = json.loads(json.dumps(_PIPE))
    if items is not None:
        spec["nodes"][0]["items"] = items
    spec["nodes"][0]["stages"][0] = first
    return spec


def test_a_fully_cached_pipeline_previews_as_a_replay_of_every_cell(db):
    """3 items x 2 stages = 6 cells. The round-trip that proves the chaining is
    the engine's own: stage 1's key is computed from the OUTPUT stage 0 cached,
    so one byte of drift and this reads as six misses."""
    result = _run(db, "run-1", _PIPE)
    assert result.status == "complete", result.faults
    assert len(db.cache_hashes_for_node("run-1", "pl", include_fanout=True)) == 6
    preview = _preview(db, "run-1", _PIPE)
    assert preview["replay"] == 6
    assert preview["invalidate"] == 0 and preview["never_completed"] == 0
    assert "unknown" not in preview


def test_mutating_stage_0_invalidates_its_cells_and_unknowns_the_ones_downstream(db):
    """The honest split: stage 0's cells CHANGED (their rows are right there
    under another hash), and stage 1's cells are not knowable at all — their
    prompt interpolates an output the run has not produced yet."""
    assert _run(db, "run-1", _PIPE).status == "complete"
    preview = _preview(db, "run-1", _pipe(first="rewrite ${item}"))
    assert preview["replay"] == 0
    assert preview["invalidate"] == 3
    assert preview["invalidated"] == [
        {"node_id": "pl", "reason": MISS_IDENTITY_CHANGED, "cells": 3, "stages": [0]}
    ]
    assert preview["unknown"] == [
        {"node_id": "pl", "why": "upstream_unknown", "cells": 3, "stages": [1]}
    ]
    assert preview["tokens_to_repay"] > 0


def test_an_item_with_no_cell_at_stage_0_is_unknown_from_stage_1_on(db):
    """A NEW item: its first stage never completed (a fact), and every stage
    after it is unknown (a refusal) — never a guessed hash."""
    assert _run(db, "run-1", _pipe(items=["a", "b"])).status == "complete"
    preview = _preview(db, "run-1", _pipe(items=["a", "b", "c"]))
    assert preview["replay"] == 4  # the two items that ran, both stages
    assert preview["never_completed"] == 1  # the new item's stage 0
    assert preview["invalidate"] == 0
    assert preview["unknown"] == [
        {"node_id": "pl", "why": "upstream_unknown", "cells": 1, "stages": [1]}
    ]


def test_a_pipeline_downstream_of_a_miss_is_unknown_whole(db):
    spec = {
        "meta": {"name": "preview", "version": 1},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "plan it"},
            {"id": "pl", "type": "pipeline", "items": ["x"], "stages": ["do ${a} ${item}"]},
        ],
    }
    preview = _preview(db, "run-1", spec)  # nothing cached at all
    assert preview["never_completed"] == 1  # only `a`; the pipeline is not knowable
    assert preview["unknown"] == [{"node_id": "pl", "why": "upstream_unknown"}]


# --- v2 (#61): nested workflow ----------------------------------------------

_CHILD: dict[str, Any] = {
    "meta": {"name": "child", "version": 3},
    "nodes": [{"id": "leaf", "type": "agent", "prompt": "do ${args.x}"}],
}
_PARENT: dict[str, Any] = {
    "meta": {"name": "preview", "version": 1},
    "nodes": [
        {"id": "sub", "type": "workflow", "ref": "child", "args": {"x": "hi"}},
        {"id": "after", "type": "agent", "prompt": "got ${sub.leaf}", "depends_on": ["sub"]},
    ],
}


def _loader(ref):
    return _CHILD if ref == "child" else None


def test_a_nested_workflow_previews_its_children_under_the_child_identity(db):
    """The nested node owns no cell; its CHILDREN do, and their keys are
    namespaced by the SUB-template's (name, version). Recomputing them under the
    parent's identity would report a total invalidation that is not happening."""
    result = _run(db, "run-1", _PARENT, loader=_loader)
    assert result.status == "complete", result.faults
    assert len(db.cache_hashes_for_node("run-1", "leaf")) == 1
    preview = _preview(db, "run-1", _PARENT, loader=_loader)
    assert preview["replay"] == 2  # the child's leaf + the parent's `after`
    assert "unknown" not in preview


def test_a_nested_child_that_changed_is_invalidated_under_its_namespaced_id(db):
    assert _run(db, "run-1", _PARENT, loader=_loader).status == "complete"
    moved = json.loads(json.dumps(_CHILD))
    moved["nodes"][0]["prompt"] = "redo ${args.x}"
    preview = _preview(db, "run-1", _PARENT, loader=lambda ref: moved if ref == "child" else None)
    assert preview["invalidated"] == [
        {"node_id": "sub[child]:leaf", "reason": MISS_IDENTITY_CHANGED_OR_SIBLING}
    ]
    # ...and the parent node downstream of it cannot be resolved either.
    assert preview["unknown"] == [{"node_id": "after", "why": "upstream_unknown"}]


def test_a_nested_template_that_cannot_be_loaded_is_unknown_with_a_why(db):
    preview = _preview(db, "run-1", _PARENT, loader=lambda ref: None)
    assert preview["unknown"] == [
        {"node_id": "sub", "why": "nested_template_unavailable"},
        {"node_id": "after", "why": "upstream_unknown"},
    ]
    preview = _preview(db, "run-1", _PARENT)  # no loader at all
    assert preview["unknown"][0] == {"node_id": "sub", "why": "nested_template_unavailable"}


def test_the_preview_never_writes_a_row_for_a_pipeline_or_a_nested_run(db):
    """(vii) extended to the two node types v2 added: still zero writes."""
    _run(db, "run-1", _PIPE)
    _run(db, "run-1", _PARENT, loader=_loader)
    before = _dump(db)
    _preview(db, "run-1", _PIPE)
    _preview(db, "run-1", _pipe(first="rewrite ${item}"))
    _preview(db, "run-1", _PARENT, loader=_loader)
    assert _dump(db) == before


def test_a_pipeline_the_engine_will_null_reports_no_cell_at_all(db):
    """items that don't resolve to a list, or an empty list: the engine records a
    fault (or returns []) without ONE lookup, so there is nothing to re-pay."""
    for items in ("${args.nope}", [], 7):
        preview = _preview(db, "run-1", _pipe(items=items))
        assert preview["replay"] == 0 and preview["invalidate"] == 0
        assert preview["never_completed"] == 0 and "unknown" not in preview


def test_a_nested_template_that_no_longer_validates_is_named_not_counted(db):
    broken = {"meta": {"name": "child"}, "nodes": [{"id": "leaf", "type": "no-such-type"}]}
    preview = _preview(db, "run-1", _PARENT, loader=lambda ref: broken)
    assert preview["unknown"][0] == {"node_id": "sub", "why": "nested_template_invalid"}


def test_nesting_past_the_depth_cap_previews_the_null_the_engine_will_produce(db):
    """Depth 2 makes the engine raise and null the node — deterministic, and no
    lookup happens. Reporting it as an unknown would invent a doubt."""
    deep = {
        "meta": {"name": "child", "version": 3},
        "nodes": [{"id": "deeper", "type": "workflow", "ref": "child"}],
    }
    preview = _preview(db, "run-1", _PARENT, loader=lambda ref: deep)
    # The nested outputs ARE knowable ({"deeper": null}), so nothing is refused:
    # `after` reads ${sub.leaf}, which that dict has not got, and the engine will
    # null it without a lookup — exactly what the preview reports by counting
    # nothing at all.
    assert preview["replay"] == 0 and preview["never_completed"] == 0
    assert preview["invalidate"] == 0 and "unknown" not in preview
