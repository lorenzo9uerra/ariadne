"""Service-package plumbing tested with a harmless synthetic TCP fixture.

These checks verify loader and runner behavior, not the correctness or
solvability of any real challenge.
"""

import shutil
from pathlib import Path

import pytest

from benchmark.packages import ROOT, load_package
from benchmark.tasks import load_config, prepare_service, target_limits

FIXTURE = ROOT / "tests" / "fixtures" / "service-01"
FLAG_KEY = "target:/workspace/flag.txt"


def test_service_package_loads_without_instance_builder():
    package = load_package(FIXTURE)
    assert package.manifest["service"]
    assert "instance" not in package.manifest
    assert package.target == ""
    assert set(package.files) == {"/workspace/handout.txt"}
    assert package.manifest["service_compose"] == "service/compose.yaml"


def test_prepare_service_stages_the_flag_only_in_the_target(tmp_path):
    package = load_package(FIXTURE)
    first = prepare_service(package, tmp_path / "first")
    second = prepare_service(package, tmp_path / "second")
    assert first.target != second.target
    assert package.target == ""
    for instance in (first, second):
        assert set(instance.files) == {"/workspace/handout.txt", FLAG_KEY}
        assert Path(instance.files[FLAG_KEY]).read_text() == instance.target + "\n"
        assert instance.manifest is package.manifest
        assert instance.target not in instance.description
        handout = Path(instance.files["/workspace/handout.txt"]).read_bytes()
        assert instance.target.encode() not in handout
        assert instance.target[5:-1].encode() not in handout


def test_package_can_raise_one_service_limit_without_changing_the_others():
    _, protocol = load_config()
    limits = target_limits({"service_limits": {"cpus": 1.0}}, protocol)
    assert limits["cpus"] == 1.0
    assert limits["memory_bytes"] == protocol["service_sandbox"]["memory_bytes"]
    assert limits["pids"] == protocol["service_sandbox"]["pids"]


def test_unknown_service_limit_is_rejected():
    _, protocol = load_config()
    with pytest.raises(ValueError, match="Unknown service limit"):
        target_limits({"service_limits": {"swap_bytes": 1}}, protocol)


def test_tampered_service_artifact_is_rejected(tmp_path):
    copied = tmp_path / "service-01"
    shutil.copytree(FIXTURE, copied)
    server = copied / "service/server.py"
    server.write_text(server.read_text() + "# changed\n")
    with pytest.raises(ValueError, match="Service artifact changed"):
        load_package(copied)


def test_undeclared_service_artifact_is_rejected(tmp_path):
    copied = tmp_path / "service-01"
    shutil.copytree(FIXTURE, copied)
    (copied / "service/extra.sh").write_text("# extra\n")
    with pytest.raises(ValueError, match="exactly one declared hash"):
        load_package(copied)


def test_missing_service_compose_is_rejected(tmp_path):
    copied = tmp_path / "service-01"
    shutil.copytree(FIXTURE, copied)
    (copied / "service/compose.yaml").unlink()
    with pytest.raises(ValueError, match="compose file"):
        load_package(copied)


def test_service_package_rejects_instance_builder(tmp_path):
    copied = tmp_path / "service-01"
    shutil.copytree(FIXTURE, copied)
    (copied / "instance.py").write_text("# builder\n")
    manifest = copied / "task.toml"
    # Top-level key, before the first table header.
    manifest.write_text(
        manifest.read_text().replace(
            "[metadata.ariadne]", '[metadata.ariadne]\ninstance = "instance.py"'
        )
    )
    with pytest.raises(ValueError, match="target-side injection"):
        load_package(copied)


def test_json_service_package_is_rejected(tmp_path):
    root = tmp_path / "json-service"
    root.mkdir()
    (root / "task.toml").write_text(
        native_record('id = "json-service"\nanswer_type = "json"\nservice = true\n')
    )
    with pytest.raises(ValueError, match="Service packages require"):
        load_package(root)


def native_record(text):
    import tomllib

    import toml

    return toml.dumps(
        {"schema_version": "1.4", "metadata": {"ariadne": tomllib.loads(text)}}
    )
