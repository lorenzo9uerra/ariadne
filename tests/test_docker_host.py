"""Docker host selection and the host canary, with Docker and SSH mocked."""

import subprocess

import pytest

from sandbox import docker_host


@pytest.fixture
def host(monkeypatch):
    monkeypatch.delenv("SANDBOX_PLATFORM", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def use(architecture):
        monkeypatch.setattr(docker_host, "docker_architecture", lambda: architecture)

    return use


@pytest.mark.parametrize(
    ("host_architecture", "required", "expected"),
    [("amd64", "amd64", "linux/amd64"), ("arm64", "any", "linux/arm64")],
)
def test_matching_and_any_architecture_select_the_host_platform(
    host, host_architecture, required, expected
):
    host(host_architecture)
    assert docker_host.select_platform(required) == expected


def test_architecture_mismatch_is_refused_instead_of_emulated(host):
    host("arm64")
    with pytest.raises(SystemExit, match="linux/amd64 Docker host"):
        docker_host.select_platform("amd64")


def test_conflicting_preset_platform_is_refused(host, monkeypatch):
    host("amd64")
    monkeypatch.setenv("SANDBOX_PLATFORM", "linux/arm64")
    with pytest.raises(SystemExit, match="does not match"):
        docker_host.select_platform("amd64")


def test_reported_architectures_are_normalized(monkeypatch):
    monkeypatch.setattr(docker_host, "docker", lambda *args: "x86_64")
    assert docker_host.docker_architecture() == "amd64"
    monkeypatch.setattr(docker_host, "docker", lambda *args: "riscv64")
    with pytest.raises(RuntimeError):
        docker_host.docker_architecture()


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("ssh://bench", ["ssh", "-o", "BatchMode=yes", "bench"]),
        (
            "ssh://ana@bench:2222",
            ["ssh", "-o", "BatchMode=yes", "-p", "2222", "ana@bench"],
        ),
    ],
)
def test_ssh_endpoints_become_ssh_commands(endpoint, expected):
    assert docker_host.ssh_command(endpoint) == expected


def test_local_endpoint_uses_a_local_canary(monkeypatch):
    monkeypatch.setattr(
        docker_host, "docker_endpoint", lambda: "unix:///var/run/docker.sock"
    )
    with docker_host.host_canary() as path:
        assert "ariadne-host-canary-" in path


def test_remote_canary_is_created_and_removed_on_the_vm(monkeypatch):
    monkeypatch.setattr(docker_host, "docker_endpoint", lambda: "ssh://bench")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, "/tmp/ariadne-host-canary-abc123\n", ""
        )

    monkeypatch.setattr(docker_host.subprocess, "run", run)
    with docker_host.host_canary() as path:
        assert path == "/tmp/ariadne-host-canary-abc123"
    assert calls[0][-2:] == ["mktemp", "/tmp/ariadne-host-canary-XXXXXXXX"]
    assert calls[-1][-4:] == ["rm", "-f", "--", "/tmp/ariadne-host-canary-abc123"]


def test_unexpected_remote_canary_path_is_rejected(monkeypatch):
    monkeypatch.setattr(docker_host, "docker_endpoint", lambda: "ssh://bench")
    monkeypatch.setattr(
        docker_host.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "/etc/passwd", ""),
    )
    with pytest.raises(RuntimeError):
        with docker_host.host_canary():
            pass


def test_image_tag_covers_every_copied_input_and_the_platform(tmp_path, monkeypatch):
    names = {
        str(p.relative_to(docker_host.SANDBOX_DIR)) for p in docker_host.image_inputs()
    }
    assert {"Dockerfile", "tool-versions.env", "python/uv.lock", "decompile"} <= names
    arm = docker_host.image_tag("linux/arm64")
    assert arm != docker_host.image_tag("linux/amd64")
    # Changing any input changes the tag.
    copy = tmp_path / "sandbox"
    for path in docker_host.image_inputs():
        target = copy / path.relative_to(docker_host.SANDBOX_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    monkeypatch.setattr(docker_host, "SANDBOX_DIR", copy)
    assert docker_host.image_tag("linux/arm64") == arm
    (copy / "tool-versions.env").write_text("changed\n")
    assert docker_host.image_tag("linux/arm64") != arm


def test_select_platform_names_the_image(host, monkeypatch):
    monkeypatch.delenv("SANDBOX_IMAGE_TAG", raising=False)
    host("arm64")
    docker_host.select_platform("arm64")
    assert docker_host.os.environ["SANDBOX_IMAGE_TAG"] == docker_host.image_tag(
        "linux/arm64"
    )


def _docker_runs(monkeypatch, outcomes):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        code = outcomes.pop(0)
        return subprocess.CompletedProcess(args, code, "", "build log tail")

    monkeypatch.setattr(docker_host.subprocess, "run", run)
    return calls


def test_present_image_is_not_rebuilt(monkeypatch):
    calls = _docker_runs(monkeypatch, [0])
    name = docker_host.ensure_image("linux/arm64")
    assert name == f"ariadne-sandbox:{docker_host.image_tag('linux/arm64')}"
    assert [c[:3] for c in calls] == [["docker", "image", "inspect"]]


def test_missing_image_is_built_once_for_its_platform(monkeypatch):
    calls = _docker_runs(monkeypatch, [1, 0])
    name = docker_host.ensure_image("linux/amd64")
    assert calls[1][:4] == ["docker", "build", "--platform", "linux/amd64"]
    assert calls[1][4:6] == ["-t", name]


def test_failed_build_reports_its_output(monkeypatch):
    _docker_runs(monkeypatch, [1, 1])
    with pytest.raises(RuntimeError, match="build log tail"):
        docker_host.ensure_image("linux/arm64")
