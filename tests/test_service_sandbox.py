"""Native service isolation and grading with a harmless TCP fixture."""

import asyncio
import json
import os
import shlex
import shutil
import subprocess

import pytest
import yaml
from harbor.models.task.config import NetworkPolicy
from harbor.models.task.task import Task

from benchmark.packages import ROOT, load_package
from benchmark.runner import run_trial
from benchmark.tasks import (
    configure_service_environment,
    prepare_trial_instance,
    read_trial_instance,
)
from sandbox.docker_host import ensure_image, select_platform
from sandbox.environment import AriadneDockerEnvironment
from tests.support import native_flag_task

DOCKER = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER") != "1", reason="Unpaid Docker integration"
)


def synthetic_service(tmp_path, image="ariadne-sandbox:test", platform="linux/amd64"):
    root = tmp_path / "service"
    shutil.copytree(ROOT / "tests/fixtures/service-01", root)
    package = load_package(root)
    native_flag_task(package, tmp_path / "template", image, platform)
    configure_service_environment(package)
    return root


def test_native_service_definition_preserves_admitted_artifacts(tmp_path):
    task = synthetic_service(tmp_path)
    original = load_package(ROOT / "tests/fixtures/service-01")
    package = load_package(task)
    assert package.manifest == original.manifest
    config = Task(task).config
    assert config.environment.network_mode.value == "allowlist"
    assert config.environment.allowed_hosts == ["target"]
    assert config.agent.allowed_hosts == ["target"]
    assert config.verifier.environment is not None
    assert config.verifier.environment.network_mode.value == "no-network"
    assert not config.verifier.env
    definition = yaml.safe_load((task / "environment/docker-compose.yaml").read_text())
    assert set(definition["services"]) == {"main", "target"}
    target = definition["services"]["target"]
    assert target["build"]["context"] == ".."
    assert target["build"]["dockerfile"] == "service/Dockerfile"
    assert target["command"] == ["python3", "/opt/server.py"]
    environment = object.__new__(AriadneDockerEnvironment)
    environment.environment_dir = task / "environment"
    environment._service_task = True
    environment._check_definition()


def test_service_trial_ground_truth_is_target_only_and_bound_to_trial(tmp_path):
    task = synthetic_service(tmp_path)
    package = load_package(task)
    trial = tmp_path / "trial"
    trial.mkdir()
    prepared = prepare_trial_instance(package, trial, "trial-1")
    restored = read_trial_instance(package, trial, "trial-1")
    assert prepared.target == restored.target
    assert set(restored.files) == {
        "/workspace/handout.txt",
        "target:/workspace/flag.txt",
    }
    assert not (trial / "private/instance/player").exists()
    with pytest.raises(ValueError, match="this task and trial"):
        read_trial_instance(package, trial, "wrong-trial")
    (trial / "private/instance/target/flag.txt").write_text("flag{changed}\n")
    with pytest.raises(ValueError, match="Service instance changed"):
        read_trial_instance(package, trial, "trial-1")


@pytest.mark.parametrize(
    "case",
    [
        "mount",
        "port",
        "privileged",
        "root",
        "host_network",
        "dns",
        "extra_service",
        "gateway",
        "external_network",
        "seccomp",
    ],
)
def test_unsafe_service_definitions_are_rejected_before_start(tmp_path, case):
    task = synthetic_service(tmp_path)
    path = task / "environment/docker-compose.yaml"
    definition = yaml.safe_load(path.read_text())
    target = definition["services"]["target"]
    if case == "mount":
        target["volumes"] = ["/:/host"]
    elif case == "port":
        target["ports"] = ["4000:4000"]
    elif case == "privileged":
        target["privileged"] = True
    elif case == "root":
        target["user"] = "root"
    elif case == "host_network":
        target["network_mode"] = "host"
    elif case == "dns":
        target["dns"] = ["8.8.8.8"]
    elif case == "extra_service":
        definition["services"]["extra"] = dict(target)
    elif case == "gateway":
        definition["networks"]["challenge"]["driver_opts"] = {}
    elif case == "external_network":
        definition["networks"]["challenge"]["internal"] = False
    else:
        target["security_opt"] = ["seccomp=unconfined"]
    path.write_text(yaml.safe_dump(definition))
    environment = object.__new__(AriadneDockerEnvironment)
    environment.environment_dir = task / "environment"
    environment._service_task = True
    with pytest.raises(ValueError):
        environment._check_definition()


@pytest.mark.parametrize(
    "hosts",
    [["example.com"], ["target", "example.com"], ["*.example.com"], ["127.0.0.1"]],
)
def test_provider_rejects_unrelated_allowlists(hosts):
    environment = object.__new__(AriadneDockerEnvironment)
    environment._is_windows_container = False
    policy = NetworkPolicy(network_mode="allowlist", allowed_hosts=hosts)
    with pytest.raises(ValueError):
        environment.validate_network_policy_support(policy)


def assert_service_cleanup(path, verifier=True):
    records = [json.loads(file.read_text()) for file in path.glob("security-*.json")]
    assert len(records) == (2 if verifier else 1)
    for record in records:
        assert all(record["checks"].values())
        assert record["cleanup_requested"]
        identifiers = [("container", record["container_id"])]
        if "target_container_id" in record:
            identifiers.extend(
                [
                    ("container", record["target_container_id"]),
                    ("network", record["network_id"]),
                ]
            )
        for kind, identifier in identifiers:
            inspected = subprocess.run(
                ["docker", kind, "inspect", identifier], capture_output=True, timeout=15
            )
            assert inspected.returncode != 0
    return records


@DOCKER
def test_service_access_fresh_flags_and_separate_grading(tmp_path):
    platform = select_platform("any")
    task = synthetic_service(tmp_path, ensure_image(platform), platform)
    targets = []
    read_service = "python3 -I -c " + shlex.quote(
        "import socket,pathlib; "
        "connection=socket.create_connection(('target',4000),timeout=5); "
        "data=connection.recv(4096); connection.close(); "
        "pathlib.Path('/logs/artifacts/submission.json').write_bytes(data)"
    )
    for index in range(2):
        result, path = asyncio.run(
            run_trial(
                task,
                tmp_path / "trials",
                None if index == 0 else targets[0],
                [
                    "test ! -e /workspace/flag.txt && test ! -e /workspace/.ariadne-expected-flag",
                    read_service,
                ],
            )
        )
        assert result.exception_info is None
        assert result.verifier_result is not None
        assert result.verifier_result.rewards == {
            "flag_correct": 1 if index == 0 else 0
        }
        targets.append(
            read_trial_instance(load_package(task), path, str(result.id)).target
        )
        records = assert_service_cleanup(path)
        agent = next(record for record in records if "target_container_id" in record)
        assert agent["target_flag_staged"]
        assert agent["service_ready_seconds"] >= 0
        assert agent["checks"]["main_network_denials"]
        assert agent["checks"]["target_network_denials"]
        assert agent["workspace_hashes_verified"]
    assert targets[0] != targets[1]


@DOCKER
def test_service_readiness_failure_prevents_agent_execution_and_cleans_up(
    tmp_path, monkeypatch
):
    platform = select_platform("any")
    task = synthetic_service(tmp_path, ensure_image(platform), platform)
    config = Task(task).config
    config.metadata["ariadne"]["service_port"] = 4001
    (task / "task.toml").write_text(config.model_dump_toml())
    monkeypatch.setattr("sandbox.checks.SERVICE_READY_SECONDS", 0)
    result, path = asyncio.run(run_trial(task, tmp_path / "trials", "flag{wrong}"))
    assert result.exception_info is not None
    assert not (path / "agent/trajectory.json").exists()
    assert_service_cleanup(path, verifier=False)
