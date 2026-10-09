"""Result plots use complete, comparable trials and preserve missing telemetry."""

import json
from dataclasses import replace

import pytest

from analysis.plot_results import aggregate, digest, export, load_runs


def experiment(tmp_path, *, counted=True, contaminated=False):
    folder = tmp_path / "experiment"
    (folder / "private").mkdir(parents=True)
    job = {
        "challenge": "synthetic",
        "task": "/synthetic/task",
        "condition": "offline",
        "name": "synthetic-offline",
        "attempts": 3,
    }
    plan = {
        "development": False,
        "settings": {
            "models": {"agent": "openrouter/example/model"},
            "live": {"provider": "example", "reasoning": True, "temperature": 0.6},
            "budgets": {"agent_turns": 60},
        },
        "inputs": {"tasks": {job["task"]: "synthetic-hash"}},
        "jobs": [job],
    }
    (folder / "private/plan.json").write_text(json.dumps(plan))
    events = []
    for slot in range(1, 4):
        attempt = f"attempt-{slot}"
        result = {
            "agent_result": {
                "n_output_tokens": 100 * slot,
                "n_reasoning_tokens": 50 * slot,
                "cost_usd": 99,  # Settlement, not this fallback, is authoritative.
                "metadata": {
                    "elapsed_seconds": 10 * slot,
                    "spending": {"billed_usd": slot / 10, "held_usd": 0},
                },
            },
            "verifier_result": {
                "rewards": {"task_success": int(slot == 1), "reward": 10}
            },
        }
        path = folder / f"{attempt}.json"
        path.write_text(json.dumps(result))
        events.extend(
            [
                {
                    "event": "attempt",
                    "attempt": attempt,
                    "planned_job": job["name"],
                    "slot": slot,
                    "path": path.name,
                },
                {
                    "event": "result",
                    "attempt": attempt,
                    "sha256": digest(path.read_bytes()),
                },
            ]
        )
        if counted:
            events.append(
                {
                    "event": "review",
                    "attempt": attempt,
                    "reviewer": "human",
                    "disposition": "counted",
                    "contaminated": contaminated,
                    "scope_violation": False,
                }
            )
    write_events(folder, events)
    return folder, events


def write_events(folder, events):
    (folder / "private/events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )


def revise_result(folder, events, edit):
    path = folder / "attempt-1.json"
    result = json.loads(path.read_text())
    edit(result)
    path.write_text(json.dumps(result))
    next(e for e in events if e["event"] == "result")["sha256"] = digest(
        path.read_bytes()
    )
    write_events(folder, events)


def two_conditions(run):
    return [run, replace(run, condition="web")]


def test_means_include_failed_attempts_and_do_not_double_count_reasoning(tmp_path):
    folder, _ = experiment(tmp_path)
    run = load_runs(folder)[0]
    points = aggregate(two_conditions(run), [run.model], [run.challenge])
    point = points[0]
    assert point.score_percent == pytest.approx(100 / 3)
    assert point.cost_usd == pytest.approx(0.2)
    assert point.output_tokens == 200
    assert point.elapsed_seconds == 20
    assert point.attempt_count == 3
    export(points, two_conditions(run), tmp_path / "plots")
    assert "score_percent" in (tmp_path / "plots/results.csv").read_text()
    assert len(json.loads((tmp_path / "plots/sources.json").read_text())) == 2


def test_partial_reward_is_not_primary_success(tmp_path):
    folder, events = experiment(tmp_path)
    revise_result(
        folder,
        events,
        lambda r: r["verifier_result"].update(
            rewards={"task_success": 0, "reward": 1, "milestone": 1}
        ),
    )
    assert load_runs(folder)[0].attempts[0]["score"] == 0


def test_contaminated_success_is_clean_failure_but_raw_preview_is_labelled(tmp_path):
    folder, _ = experiment(tmp_path, contaminated=True)
    assert load_runs(folder)[0].attempts[0]["score"] == 0
    run = load_runs(folder, provisional=True)[0]
    point = aggregate(
        two_conditions(run), [run.model], [run.challenge], provisional=True
    )[0]
    assert point.score_percent == pytest.approx(100 / 3)
    assert point.status == "provisional"


def test_replacement_selects_latest_slot_and_never_reads_superseded_result(tmp_path):
    folder, events = experiment(tmp_path)
    old = events[0]
    replacement = dict(old, attempt="replacement", path="replacement.json")
    events.append(replacement)
    old_result = json.loads((folder / old["path"]).read_text())
    old_result["verifier_result"]["rewards"]["task_success"] = 0
    path = folder / "replacement.json"
    path.write_text(json.dumps(old_result))
    events.extend(
        [
            {
                "event": "result",
                "attempt": "replacement",
                "sha256": digest(path.read_bytes()),
            },
            {
                "event": "review",
                "attempt": "replacement",
                "reviewer": "human",
                "disposition": "counted",
                "contaminated": False,
                "scope_violation": False,
            },
        ]
    )
    (folder / old["path"]).unlink()
    write_events(folder, events)
    run = load_runs(folder)[0]
    assert run.attempts[0]["attempt"] == "replacement"
    assert sum(a["score"] for a in run.attempts) == 0


@pytest.mark.parametrize(
    "spending", [None, {"billed_usd": 0.1, "held_usd": 1}, {"held_usd": 0}]
)
def test_unconfirmed_cost_is_not_zero_or_a_partial_average(tmp_path, spending):
    folder, events = experiment(tmp_path)

    def edit(result):
        result["agent_result"]["metadata"]["spending"] = spending
        result["agent_result"]["cost_usd"] = None

    revise_result(folder, events, edit)
    run = load_runs(folder)[0]
    point = aggregate(two_conditions(run), [run.model], [run.challenge])[0]
    assert point.cost_usd is None
    assert point.output_tokens == 200


def test_rejection_billing_correction_remains_incomplete_for_cost_plot(tmp_path):
    folder, events = experiment(tmp_path)
    record = next(e for e in events if e["event"] == "result")
    events.append(
        {
            "event": "billing_adjustment",
            "attempt": record["attempt"],
            "result_sha256": record["sha256"],
            "spending": {
                "billed_usd": 0.1,
                "held_usd": 0,
                "expected_unbilled_requests": 2,
            },
        }
    )
    write_events(folder, events)
    run = load_runs(folder)[0]
    assert run.attempts[0]["held_usd"] == 0
    assert run.attempts[0]["expected_unbilled_requests"] == 2
    assert run.attempts[0]["cost_usd"] is None


def test_missing_result_cannot_be_filled_with_zero(tmp_path):
    folder, events = experiment(tmp_path)
    write_events(folder, [e for e in events if e["attempt"] != "attempt-3"])
    run = load_runs(folder)[0]
    with pytest.raises(ValueError, match="missing result"):
        aggregate(two_conditions(run), [run.model], [run.challenge])


def test_default_requires_review_and_provisional_does_not(tmp_path):
    folder, _ = experiment(tmp_path, counted=False)
    assert "review pending" in load_runs(folder)[0].problems[0]
    run = load_runs(folder, provisional=True)[0]
    assert not run.problems and len(run.attempts) == 3


def test_flagged_automatic_review_waits_for_human(tmp_path):
    folder, events = experiment(tmp_path)
    events[:] = [e for e in events if e["event"] != "review"]
    events.extend(
        [
            {
                "event": "review",
                "attempt": "attempt-1",
                "reviewer": "autoreview-v1",
                "disposition": "counted",
                "contaminated": False,
                "scope_violation": False,
            },
            {
                "event": "autoreview",
                "attempt": "attempt-1",
                "human_sample": True,
                "findings": [],
            },
        ]
    )
    write_events(folder, events)
    assert "slot 1: outcome review pending" in load_runs(folder)[0].problems
    events.append(
        {
            "event": "review",
            "attempt": "attempt-1",
            "reviewer": "human",
            "disposition": "counted",
            "contaminated": False,
            "scope_violation": False,
        }
    )
    write_events(folder, events)
    assert not any("slot 1" in p for p in load_runs(folder)[0].problems)


def test_excluded_active_attempt_needs_replacement_even_in_preview(tmp_path):
    folder, events = experiment(tmp_path)
    next(e for e in events if e["event"] == "review")["disposition"] = (
        "implementation_fault"
    )
    write_events(folder, events)
    assert "excluded" in load_runs(folder, provisional=True)[0].problems[0]


def test_changed_native_result_is_rejected(tmp_path):
    folder, _ = experiment(tmp_path)
    (folder / "attempt-1.json").write_text("{}")
    with pytest.raises(ValueError, match="changed after collection"):
        load_runs(folder)


def test_approved_configuration_update_is_used_and_bound_to_original_plan(tmp_path):
    folder, events = experiment(tmp_path)
    path = folder / "private/plan.json"
    plan = json.loads(path.read_text())
    plan["settings"]["live"]["reasoning"] = False
    events.append(
        {
            "event": "configuration_update",
            "original_plan_sha256": digest(path.read_bytes()),
            "settings": plan["settings"],
            "inputs": plan["inputs"],
            "implementation": {},
        }
    )
    write_events(folder, events)
    assert load_runs(folder)[0].reasoning == '{"reasoning": false}'
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="Changed original plan"):
        load_runs(folder)


def test_unreviewed_exception_needs_attribution_even_for_preview(tmp_path):
    folder, events = experiment(tmp_path, counted=False)
    revise_result(
        folder,
        events,
        lambda r: r.update(exception_info={"exception_type": "SyntheticError"}),
    )
    assert (
        "exception needs attribution"
        in load_runs(folder, provisional=True)[0].problems[0]
    )


def test_development_experiments_are_ignored(tmp_path):
    folder, _ = experiment(tmp_path)
    path = folder / "private/plan.json"
    plan = json.loads(path.read_text())
    plan["development"] = True
    path.write_text(json.dumps(plan))
    assert load_runs(folder) == []


def test_every_model_must_have_the_same_tasks_and_conditions(tmp_path):
    folder, _ = experiment(tmp_path)
    run = load_runs(folder)[0]
    with pytest.raises(ValueError, match="missing experiment"):
        aggregate(two_conditions(run), [run.model, "missing/model"], [run.challenge])


def test_duplicate_runs_and_mixed_settings_are_rejected(tmp_path):
    folder, _ = experiment(tmp_path)
    run = load_runs(folder)[0]
    with pytest.raises(ValueError, match="Duplicate"):
        aggregate([run, run], [run.model], [run.challenge])
    with pytest.raises(ValueError, match="mix provider"):
        aggregate(
            [run, replace(run, condition="web", settings_key="different")],
            [run.model],
            [run.challenge],
        )
    with pytest.raises(ValueError, match="different versions"):
        aggregate(
            [run, replace(run, condition="web", task_hash="changed")],
            [run.model],
            [run.challenge],
        )


def test_equal_weight_per_task_not_per_success_or_task_size(tmp_path):
    folder, _ = experiment(tmp_path)
    run = load_runs(folder)[0]
    other = replace(run, challenge="second", attempts=[dict(run.attempts[0], score=1)])
    points = aggregate(
        two_conditions(run) + two_conditions(other),
        [run.model],
        [run.challenge, other.challenge],
    )
    assert points[0].score_percent == pytest.approx(100 * (1 / 3 + 1) / 2)
    assert points[0].cost_usd == pytest.approx(0.15)
