"""Use the active Docker context for local or SSH-hosted sandboxes.

Logs, spending records and credentials remain on the evaluation host.
"""

import contextlib
import hashlib
import os
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

SANDBOX_DIR = Path(__file__).resolve().parent

ARCHITECTURES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def docker_architecture() -> str:
    """The CPU architecture of the Docker engine that will run the sandboxes."""
    try:
        reported = docker("info", "--format", "{{.Architecture}}")
    except subprocess.CalledProcessError as error:
        detail = (
            error.stderr or f"Docker exited with status {error.returncode}"
        ).strip()
        raise SystemExit(
            f"Docker host check failed:\n{detail}\n"
            "Use 'docker context ls' to check the name in DOCKER_CONTEXT."
        ) from None
    if reported not in ARCHITECTURES:
        raise RuntimeError(f"Unsupported Docker host architecture: {reported}")
    return ARCHITECTURES[reported]


def select_platform(required: str) -> str:
    """Set SANDBOX_PLATFORM for a package, refusing to emulate another architecture.

    Emulation can break debuggers used by binary tasks.
    """
    host = docker_architecture()
    if required not in ("any", host):
        raise SystemExit(
            f"This package needs a linux/{required} Docker host, but the active "
            f"Docker context runs linux/{host}. Select a matching context with "
            "DOCKER_CONTEXT (see docs/vm_setup.md)."
        )
    platform = f"linux/{host}"
    preset = os.environ.get("SANDBOX_PLATFORM")
    if preset and preset != platform:
        raise SystemExit(
            f"SANDBOX_PLATFORM={preset} does not match the {platform} host"
        )
    os.environ["SANDBOX_PLATFORM"] = platform
    os.environ["SANDBOX_IMAGE_TAG"] = image_tag(platform)
    return platform


def image_inputs() -> list[Path]:
    """The Dockerfile and every file its COPY instructions read, in a fixed order."""
    dockerfile = SANDBOX_DIR / "Dockerfile"
    files = [dockerfile]
    for line in dockerfile.read_text().splitlines():
        words = line.split()
        if not words or words[0] != "COPY":
            continue
        sources = [w for w in words[1:-1] if not w.startswith("--")]
        for source in sources:
            path = SANDBOX_DIR / source
            if path.is_dir():
                files += sorted(
                    f
                    for f in path.rglob("*")
                    if f.is_file() and not {"__pycache__", ".venv"} & set(f.parts)
                )
            else:
                files.append(path)
    return files


def image_tag(platform: str) -> str:
    """Derive the shared image tag from its platform and build inputs."""
    digest = hashlib.sha256(platform.encode())
    for path in image_inputs():
        digest.update(str(path.relative_to(SANDBOX_DIR)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def ensure_image(platform: str) -> str:
    """Build the shared image on the active Docker host if its tag is missing."""
    name = f"ariadne-sandbox:{image_tag(platform)}"
    present = subprocess.run(
        ["docker", "image", "inspect", name], capture_output=True, timeout=30
    )
    if present.returncode == 0:
        return name
    result = subprocess.run(
        ["docker", "build", "--platform", platform, "-t", name, str(SANDBOX_DIR)],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Building {name} failed:\n{result.stderr[-4000:]}")
    return name


def docker_endpoint() -> str:
    """The endpoint of the active Docker context, honoring DOCKER_HOST."""
    if os.environ.get("DOCKER_HOST"):
        return os.environ["DOCKER_HOST"]
    return docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")


def ssh_command(endpoint: str) -> list[str]:
    """An ssh invocation for an ``ssh://[user@]host[:port]`` Docker endpoint."""
    parts = urlsplit(endpoint)
    if parts.scheme != "ssh" or not parts.hostname:
        raise ValueError("Expected an ssh:// Docker endpoint")
    target = f"{parts.username}@{parts.hostname}" if parts.username else parts.hostname
    port = ["-p", str(parts.port)] if parts.port else []
    return ["ssh", "-o", "BatchMode=yes", *port, target]


@contextlib.contextmanager
def host_canary() -> Iterator[str]:
    """Yield a path that exists on the Docker host and must be invisible to containers.

    For a remote context the canary is created on the VM, which is the host
    whose files a container could reach through a mistaken mount.
    """
    endpoint = docker_endpoint()
    if not endpoint.startswith("ssh://"):
        with tempfile.NamedTemporaryFile(prefix="ariadne-host-canary-") as canary:
            yield canary.name
        return
    ssh = ssh_command(endpoint)
    path = subprocess.run(
        [*ssh, "mktemp", "/tmp/ariadne-host-canary-XXXXXXXX"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if not path.startswith("/tmp/ariadne-host-canary-"):
        raise RuntimeError("Unexpected remote canary path")
    try:
        yield path
    finally:
        subprocess.run(
            [*ssh, "rm", "-f", "--", path], check=False, capture_output=True, timeout=30
        )
