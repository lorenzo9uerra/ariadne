"""An experiment on disk: its frozen plan, event journal and input fingerprints.

The journal is append-only: runs, results and review decisions are added as
events and never edited, so every score can be traced to untouched records.
"""

import fcntl
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from benchmark.tasks import ROOT

DISPOSITIONS = {
    "counted",
    "external_failure",
    "setup_failure",
    "implementation_fault",
    "unattributed_failure",
    "pending",
}


EXCLUDED = DISPOSITIONS - {"counted", "pending"}


# Endings that are ordinary counted outcomes; any other needs attribution.
ORDINARY = {
    "submitted",
    "elapsed_seconds",
    "agent_turns",
    "total_tool_calls",
    "context_limit",
    "monitor_budget",
}


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def tree_digest(root: Path) -> str:
    """Hash directory paths and file contents deterministically."""
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Experiment inputs cannot contain symlinks")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = digest(path)
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def fingerprint(tasks: list[dict]) -> dict:
    """Behavioral inputs must remain fixed; implementation fixes are recorded separately."""
    files = [
        ROOT / "benchmark/draft.toml",
        ROOT / "config.toml",
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
        ROOT / "job.yaml",
    ]
    files.extend(
        path
        for path in sorted((ROOT / "benchmark/prompts").iterdir())
        if path.is_file() and path.suffix in (".txt", ".json")
    )
    return {
        "files": {str(path): digest(path) for path in files},
        "tasks": {item["task"]: tree_digest(Path(item["task"])) for item in tasks},
    }


def implementation() -> dict:
    files = sorted((ROOT / "benchmark").glob("*.py"))
    files.extend(sorted((ROOT / "sandbox").rglob("*.py")))
    files.extend(
        path
        for path in sorted((ROOT / "sandbox").rglob("*"))
        if path.is_file()
        and (
            path.suffix in (".sh", ".java", ".yaml", ".toml", ".lock", ".env")
            or path.name in ("Dockerfile", "decompile")
        )
    )
    return {str(path.relative_to(ROOT)): digest(path) for path in files}


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


@contextmanager
def journal(folder: Path):
    path = folder / "private/events.jsonl"
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        events = [json.loads(line) for line in stream if line.strip()]

        def append(kind, **fields):
            event = {
                "event": kind,
                "time": datetime.now(timezone.utc).isoformat(),
                **fields,
            }
            stream.seek(0, 2)
            stream.write(json.dumps(event, allow_nan=False) + "\n")
            stream.flush()
            events.append(event)

        try:
            yield events, append
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_plan(folder: Path) -> dict:
    path = folder / "private/plan.json"
    plan = json.loads(path.read_text())
    if plan.get("version") != 3:
        raise ValueError("Unsupported experiment plan version")
    with journal(folder) as (events, _):
        updates = [
            event for event in events if event["event"] == "configuration_update"
        ]
    for update in updates:
        if update["original_plan_sha256"] != digest(path):
            raise ValueError("Original plan changed after the configuration review")
        plan.update(
            {key: update[key] for key in ("settings", "inputs", "implementation")}
        )
    return plan


def verify_inputs(plan: dict) -> None:
    if fingerprint(plan["jobs"]) != plan["inputs"]:
        raise ValueError(
            "Frozen task, prompt, dependency or configuration inputs changed; start a new experiment"
        )


def retry_interruption(row):
    if row.get("stop_reason") != "elapsed_seconds" or not row.get("path"):
        return False
    audit = Path(row["path"]).parent / "private/audit.jsonl"
    if not audit.is_file():
        return False
    requests = {}
    for line in audit.read_text().splitlines():
        entry = json.loads(line)
        if entry.get("stage") == "model_request":
            requests[entry["call_id"]] = entry
    last = list(requests.values())[-1] if requests else {}
    return last.get("status") == "http_error" and last.get("retry_wait_seconds", 0) > 0
