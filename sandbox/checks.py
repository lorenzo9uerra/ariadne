"""Whether a sandbox's containers and network are isolated, and its service ready."""

import asyncio
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from sandbox.container import script

PROBES = script("probes.sh")


SERVICE_READY_SECONDS = 30


async def wait_for_service(environment, target_id: str, port: int) -> float:
    """Wait for the target port; treat a timeout as a setup failure."""
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError("service_port must be an integer TCP port")
    started = time.monotonic()
    while True:
        try:
            probe = await environment.exec(
                "bash -c " + shlex.quote(f"exec 3<>/dev/tcp/target/{port}"),
                timeout_sec=5,
            )
            ready = probe.return_code == 0
        except RuntimeError:
            # Harbor reports a command that outlives its timeout as a RuntimeError.
            ready = False
        if ready:
            return round(time.monotonic() - started, 2)
        if time.monotonic() - started >= SERVICE_READY_SECONDS:
            logs = subprocess.run(
                ["docker", "logs", "--tail", "40", target_id],
                capture_output=True,
                text=True,
                timeout=15,
            )
            raise RuntimeError(
                f"Target service did not accept connections on port {port} within "
                f"{SERVICE_READY_SECONDS} seconds. Target logs:\n"
                + (logs.stdout + logs.stderr)[-4000:]
            )
        await asyncio.sleep(1)


def inspect_docker(kind: str, identifier: str) -> dict:
    """Inspect a container or network by id, capturing the output instead of printing it."""
    return json.loads(
        subprocess.run(
            ["docker", kind, "inspect", identifier],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    )[0]


@dataclass(frozen=True)
class Controls:
    """A container's isolation settings, as the checks read them."""

    # Identity and privileges
    user: str
    privileged: bool
    capabilities_dropped: list[str]
    capabilities_added: list[str]
    no_new_privileges: bool
    custom_seccomp: bool
    security_options: list[str]
    apparmor_profile: str
    # Namespace modes for pid, ipc, userns, uts and cgroup. "" or "private"
    # means its own.
    namespaces: dict[str, str]
    has_devices: bool
    hostname: str
    environment: list[str]  # "NAME=value" entries.

    # Filesystem
    read_only_root: bool
    bind_mounts: list[str]
    tmpfs: dict[str, int | None]  # Mount point -> size in bytes, if declared.
    mounts: list[tuple[str, str]]  # (type, mount point) for every mount.

    # Network
    network: str  # "none", "host", "bridge", or the network the container joins
    has_published_ports: bool
    extra_hosts: list[str]
    dns: list[str]
    sysctls: dict[str, str]

    # Resources
    memory_bytes: int
    memory_and_swap_bytes: int
    cpu_nanos: int  # Billionths of a CPU.
    pids_limit: int | None


EPHEMERAL_MOUNTS = {"/workspace", "/tmp"}
# The agent's container also gets an in-memory home, outside the task's
# workspace.
HOME = "/home/agent"
HOME_BYTES = 16 * 1024 * 1024

# The agent container's limits come from [sandbox] in draft.toml, frozen in an
# experiment's plan. Each task's docker-compose.yaml refers to them through
# these variables, which the environment sets before Docker Compose runs.
LIMIT_VARIABLES = {
    "cpus": "ARIADNE_CPUS",
    "memory_bytes": "ARIADNE_MEMORY_BYTES",
    "pids": "ARIADNE_PIDS",
    "workspace_bytes": "ARIADNE_WORKSPACE_BYTES",
    "temp_bytes": "ARIADNE_TEMP_BYTES",
}


def check_sandbox_limits(limits: dict) -> dict:
    """The agent container's limits from [sandbox], each a positive number."""
    if set(limits) != set(LIMIT_VARIABLES) or any(
        isinstance(value, bool) or type(value) not in (int, float) or value <= 0
        for value in limits.values()
    ):
        raise ValueError(
            "[sandbox] needs positive cpus, memory_bytes, pids, workspace_bytes "
            "and temp_bytes"
        )
    return dict(limits)


def limit_reference(key: str) -> str:
    """How a compose file refers to one of the agent container's limits."""
    return f"${{{LIMIT_VARIABLES[key]}:?set by the secure environment}}"


# Host variables whose values must never appear in a container.
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
# The agent loads the API keys from .env only after the sandbox starts.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def host_secrets() -> list[str]:
    """Values of the host's secret-looking variables, from the environment and .env."""
    variables = {**dotenv_values(ENV_FILE), **os.environ}
    return [
        value
        for name, value in variables.items()
        if value and SECRET_NAME.search(name) and len(value) >= 16
    ]


# The only variables a task's compose file may substitute: the harness sets
# these. Compose would fill any other $NAME from the host's environment,
# which holds the API keys.
COMPOSE_VARIABLES = {*LIMIT_VARIABLES.values(), "SANDBOX_PLATFORM", "SANDBOX_IMAGE_TAG"}
# Compose substitutes $NAME and ${NAME...}; $$ is a literal dollar sign.
SUBSTITUTION = re.compile(
    r"(?<!\$)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*))"
)


def compose_substitutions(text: str) -> set[str]:
    """The variable names a compose file asks Compose to substitute."""
    return {braced or plain for braced, plain in SUBSTITUTION.findall(text)}


def bridge_gateways() -> list[str]:
    """The Docker host's address on its default bridge, as its daemon sets it."""
    try:
        bridge = inspect_docker("network", "bridge")
    except subprocess.CalledProcessError:
        return []  # A daemon without a default bridge has nothing to reach.
    return [
        config["Gateway"]
        for config in (bridge.get("IPAM") or {}).get("Config") or []
        if config.get("Gateway")
    ]


def tmpfs_size(options: str) -> int | None:
    """The size in bytes from tmpfs options such as "rw,size=65536"."""
    for option in options.split(","):
        if option.startswith("size=") and option[5:].isdigit():
            return int(option[5:])
    return None


def docker_controls(details: dict) -> Controls:
    """Read a container's controls from `docker inspect` output."""
    host, config = details["HostConfig"], details["Config"]
    security_options = host["SecurityOpt"] or []
    return Controls(
        user=config["User"],
        privileged=host["Privileged"],
        capabilities_dropped=host["CapDrop"] or [],
        capabilities_added=host["CapAdd"] or [],
        no_new_privileges=any(
            option in ("no-new-privileges", "no-new-privileges:true")
            for option in security_options
        ),
        custom_seccomp=any(
            option.startswith(("seccomp=", "seccomp:")) for option in security_options
        ),
        security_options=security_options,
        apparmor_profile=details.get("AppArmorProfile") or "",
        namespaces={
            field: host.get(field) or ""
            for field in ("PidMode", "IpcMode", "UsernsMode", "UTSMode", "CgroupnsMode")
        },
        has_devices=any(
            host.get(field)
            for field in ("Devices", "DeviceRequests", "DeviceCgroupRules")
        ),
        hostname=config["Hostname"],
        environment=config.get("Env") or [],
        read_only_root=host["ReadonlyRootfs"],
        bind_mounts=host["Binds"] or [],
        tmpfs={path: tmpfs_size(options) for path, options in host["Tmpfs"].items()},
        mounts=[(mount["Type"], mount["Destination"]) for mount in details["Mounts"]],
        network=host["NetworkMode"],
        has_published_ports=bool(
            host["PortBindings"] or details["NetworkSettings"]["Ports"]
        ),
        extra_hosts=host.get("ExtraHosts") or [],
        dns=host.get("Dns") or [],
        sysctls=host.get("Sysctls") or {},
        memory_bytes=host["Memory"],
        memory_and_swap_bytes=host["MemorySwap"],
        cpu_nanos=host["NanoCpus"],
        pids_limit=host["PidsLimit"],
    )


def container_checks(
    controls: Controls,
    expected_limits: dict | None,
    hostname: str = "workspace",
    networked: bool = False,
    home: bool = False,
) -> dict:
    """Whether a container's controls match the sandbox policy, check by check.

    `home` is set for the agent's container, which also mounts the user's home.
    """
    mounts = (EPHEMERAL_MOUNTS | {HOME}) if home else EPHEMERAL_MOUNTS
    checks = {
        "fixed_nonroot_user": controls.user == "1000:1000",
        "unprivileged": not controls.privileged,
        "all_capabilities_dropped": controls.capabilities_dropped == ["ALL"]
        and not controls.capabilities_added,
        "no_new_privileges": controls.no_new_privileges,
        # Options such as apparmor=unconfined or systempaths=unconfined would
        # lift a default restriction. A custom seccomp profile is checked
        # separately, against the one each container may use.
        "only_known_security_options": all(
            option.startswith(("no-new-privileges", "seccomp=", "seccomp:"))
            for option in controls.security_options
        ),
        "apparmor_confined": controls.apparmor_profile != "unconfined",
        "no_host_namespaces": "host" not in controls.namespaces.values(),
        "no_added_devices": not controls.has_devices,
        "no_host_secrets": not any(
            secret in entry
            for secret in host_secrets()
            for entry in controls.environment
        ),
        "root_filesystem_read_only": controls.read_only_root,
        "only_ephemeral_mounts": not controls.bind_mounts
        and set(controls.tmpfs) == mounts
        and all(kind == "tmpfs" and path in mounts for kind, path in controls.mounts),
        "no_published_ports": not controls.has_published_ports,
        "memory_bounded_without_extra_swap": controls.memory_bytes > 0
        and controls.memory_and_swap_bytes == controls.memory_bytes,
        "cpu_bounded": controls.cpu_nanos > 0,
        "processes_bounded": controls.pids_limit is not None
        and controls.pids_limit > 0,
    }
    if expected_limits is not None:
        checks.update(
            configured_memory=controls.memory_bytes == expected_limits["memory_bytes"],
            configured_cpu=controls.cpu_nanos
            == int(expected_limits["cpus"] * 1_000_000_000),
            configured_processes=controls.pids_limit == expected_limits["pids"],
            configured_workspace=controls.tmpfs.get("/workspace")
            == expected_limits["workspace_bytes"],
            configured_temp=controls.tmpfs.get("/tmp") == expected_limits["temp_bytes"],
            neutral_hostname=controls.hostname == hostname,
        )
    if home:
        checks["configured_home"] = controls.tmpfs.get(HOME) == HOME_BYTES
    if not networked:
        checks["network_disabled"] = controls.network == "none"
        return checks
    checks.update(
        # A named private network, never the default bridge or the host's.
        network_mode_verified=controls.network not in ("none", "host", "bridge"),
        separate_namespaces=all(
            mode in ("", "private") for mode in controls.namespaces.values()
        ),
        no_host_aliases=not controls.extra_hosts,
        external_dns_disabled=controls.dns == ["127.0.0.1"],
        ipv6_disabled=all(
            controls.sysctls.get(name) == "1"
            for name in (
                "net.ipv6.conf.all.disable_ipv6",
                "net.ipv6.conf.default.disable_ipv6",
            )
        ),
    )
    return checks


def network_checks(
    agent: dict, target: dict, network: dict, *, agent_service: str = "default"
) -> dict:
    """Require one project-scoped, isolated network with exactly two members."""
    project = agent["Config"].get("Labels", {}).get("com.docker.compose.project")
    return {
        "internal_bridge": network["Driver"] == "bridge" and network["Internal"],
        "isolated_gateway": network.get("Options", {}).get(
            "com.docker.network.bridge.gateway_mode_ipv4"
        )
        == "isolated",
        "ipv6_network_disabled": not network["EnableIPv6"],
        "only_expected_members": set(network.get("Containers", {}))
        == {agent["Id"], target["Id"]},
        "one_network_per_container": all(
            len(item["NetworkSettings"]["Networks"]) == 1
            and next(iter(item["NetworkSettings"]["Networks"].values()))["NetworkID"]
            == network["Id"]
            for item in (agent, target)
        ),
        "project_scoped": bool(project)
        and all(
            item.get("Labels", {}).get("com.docker.compose.project") == project
            for item in (target["Config"], network)
        ),
        "expected_services": agent["Config"]["Labels"].get("com.docker.compose.service")
        == agent_service
        and target["Config"]["Labels"].get("com.docker.compose.service") == "target",
    }


NETWORK_PROBES = script("network_probes.py")
