# Remote VM setup

A remote VM runs the sandboxes when a task needs a CPU architecture the
evaluation host lacks, typically x86-64 pwn and rev binaries on an ARM machine,
where emulation is slow and debuggers fail. Ariadne itself stays on the
evaluation host and reaches the VM's Docker engine through a Docker context
over SSH, so keys, records and the spending ledger never leave it. The VM also
gives the sandboxes a kernel separate from the evaluation host's.

## Requirements

- **x86-64 Ubuntu 26.04** with its own kernel: a full VM or bare metal, not a
  container-based instance.
- **SSH** with key authentication, and an account with sudo.
- **Capacity:** at least 2 vCPUs, 4 GiB of memory and 40 GB of disk.
- **Network:** inbound SSH from the evaluation host only. Outbound access is
  needed only for setup and image builds; model and search APIs are called
  from the evaluation host, so outbound traffic can be blocked during runs.

Docker access is root-equivalent on the VM, so anyone who can use the context
is a trusted evaluator.

## Setup

Add an alias for the VM to `~/.ssh/config`, then create the local, untracked
`infra/ansible/inventory.ini`:

```ini
[benchmark_hosts]
benchmark-vm ansible_python_interpreter=/usr/bin/python3
```

Run the playbook from the repository root (add `--ask-become-pass` if sudo
needs a password):

```sh
uv sync --group infra
uv run --group infra ansible-playbook -i infra/ansible/inventory.ini infra/ansible/provision.yml
```

It installs the Docker versions pinned in `infra/ansible/versions.yml`, checks
that the engine reports `x86_64`, creates the local context
`ariadne-<alias>`, and runs the synthetic Docker tests through it. The first
run builds the shared sandbox image on the VM. Rerun only the checks with
`--tags verify`; harness changes need no deployment.

## Use

Select the context per command:

```sh
DOCKER_CONTEXT=ariadne-benchmark-vm uv run harbor run -c job.dev.yaml -p tasks/pwn-01
DOCKER_CONTEXT=ariadne-benchmark-vm uv run harbor run -c job.dev.yaml -p tasks/pwn-01 -a oracle
DOCKER_CONTEXT=ariadne-benchmark-vm uv run python -m benchmark.experiment check
```

The environment compares each task's declared `architecture` with the
engine's and refuses a mismatch rather than emulating; tasks declaring `any`
run anywhere. Only images and staged player files reach the VM. Old sandbox
images (`ariadne-sandbox:<tag>`) are not deleted automatically.

To update Docker, list the available versions on the VM and record the full
version strings in `versions.yml`, keeping Engine and CLI matched:

```sh
ssh benchmark-vm 'apt-cache madison docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin'
```
