"""Tool-wrapper checks and opt-in analysis of a harmless synthetic program."""

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def exporter(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader(
        "decompile", str(ROOT / "sandbox/decompile")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    config = tmp_path / "tools.env"
    binary = tmp_path / "binary with spaces"
    binary.write_bytes(b"Synthetic input; never executed.")
    fake = tmp_path / "analyzeHeadless"
    monkeypatch.setattr(module, "CONFIG", config)
    monkeypatch.setattr(module, "GHIDRA", fake)
    cache = tmp_path / "compiled-bundles"
    cache.mkdir()
    (cache / "synthetic.class").write_bytes(b"Synthetic cached tool; no analysis data.")
    monkeypatch.setattr(module, "CACHE", cache)
    real_temporary = tempfile.TemporaryDirectory
    monkeypatch.setattr(
        module.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: real_temporary(prefix=kwargs["prefix"], dir=tmp_path),
    )

    def behavior(code, seconds=10):
        # Generous by default: the fake tool's own start-up must never hit it.
        config.write_text(
            f"DECOMPILE_SECONDS={seconds}\nDECOMPILE_OUTPUT_BYTES=256\n"
            "DECOMPILE_FUNCTION_SECONDS=1\nGHIDRA_HEAP=384m\nGHIDRA_DIRECTORY=synthetic\n"
        )
        fake.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, time\nfrom pathlib import Path\n"
            "args = sys.argv[sys.argv.index('Decompile.java') + 1:]\n"
            "output, status, selector = Path(args[0]), Path(args[1]), args[2]\n" + code
        )
        fake.chmod(0o755)
        return module, binary

    return behavior


@pytest.mark.parametrize("status", ["ok", "truncated", "partial"])
def test_exporter_requires_completion_and_preserves_selector(exporter, capfd, status):
    module, binary = exporter(
        "assert selector == 'name; literal text'\n"
        "assert (Path(os.environ['XDG_CONFIG_HOME']) / 'ghidra/synthetic/osgi/compiled-bundles/synthetic.class').is_file()\n"
        f"output.write_text('synthetic output')\nstatus.write_text('{status}')\n"
    )
    assert module.run(binary, "name; literal text") == (1 if status == "partial" else 0)
    assert capfd.readouterr().out == "synthetic output"
    assert not list(binary.parent.glob("decompile-*"))
    assert (module.CACHE / "synthetic.class").is_file()


def test_zero_exit_without_completion_is_failure_with_bounded_diagnostics(
    exporter, capfd
):
    module, binary = exporter("print('x' * 10000)\n")
    assert module.run(binary, "") == 1
    diagnostic = capfd.readouterr().err
    assert "analysis failed" in diagnostic
    assert len(diagnostic.encode()) <= 256


def test_exporter_rejects_oversized_output(exporter, capfd):
    module, binary = exporter(
        "output.write_bytes(b'x' * 257)\nstatus.write_text('ok')\n"
    )
    assert module.run(binary, "") == 1
    result = capfd.readouterr()
    assert result.out == ""
    assert "output limit" in result.err


def test_exporter_reports_missing_function(exporter, capfd):
    module, binary = exporter("status.write_text('missing')\n")
    assert module.run(binary, "absent") == 1
    assert "no function matched" in capfd.readouterr().err


def test_exporter_deadline_stops_process_and_cleans_workspace(exporter, capfd):
    module, binary = exporter(
        "print('synthetic analysis started', flush=True)\ntime.sleep(60)\n", seconds=1
    )
    started = time.monotonic()
    assert module.run(binary, "") == 1
    assert time.monotonic() - started < 3
    diagnostic = capfd.readouterr().err
    assert "1-second time limit" in diagnostic
    assert "synthetic analysis started" in diagnostic
    assert len(diagnostic.encode()) <= 256
    assert not list(binary.parent.glob("decompile-*"))


@pytest.mark.skipif(
    os.environ.get("RUN_DOCKER") != "1", reason="Unpaid synthetic analysis tools check"
)
def test_analysis_tools_through_docker(tmp_path):
    import asyncio
    import shlex

    from benchmark.tasks import Package
    from sandbox.docker_host import ensure_image, select_platform
    from tests.support import SAFE, assert_isolation_and_cleanup, export_task, run_trial

    platform = select_platform("any")
    has_ltrace = platform == "linux/amd64"
    ltrace = "ltrace" if has_ltrace else ""
    source = tmp_path / "probe.c"
    source.write_text(
        "#include <stdio.h>\n"
        "int identity(int value) { return value + 1; }\n"
        'int main(void) { puts("synthetic output"); return identity(6) == 7 ? 0 : 1; }\n'
    )
    package = Package(
        tmp_path,
        {
            "id": "synthetic-tools",
            "answer_type": "json",
            "service": False,
            "architecture": "any",
        },
        "Check the supplied synthetic program.",
        SAFE,
        {"/workspace/probe.c": str(source)},
    )
    task = export_task(package, tmp_path / "task", ensure_image(platform), platform)
    commands = [
        "set -e; command -v base64 sha256sum grep sed awk find tar file strings readelf objdump nm xxd gcc make python gdb strace "
        f"{ltrace} patchelf jq rg nc zip unzip decompile; python --version",
        "gcc -g -O0 -o /workspace/probe /workspace/probe.c && /workspace/probe",
        "readelf -s /workspace/probe",
        "python -c "
        + shlex.quote(
            "from pwn import ELF,process; binary=ELF('/workspace/probe',checksec=False); assert 'identity' in binary.symbols; child=process(['/workspace/probe']); assert child.recvline(timeout=2)==b'synthetic output\\n'; child.close(); print('pwntools passed')"
        ),
        "gdb --batch -q -nx -ex 'set disable-randomization off' -ex 'break identity' -ex run -ex 'print value' -ex continue /workspace/probe",
        "decompile /workspace/probe identity",
        "python -c "
        + shlex.quote(
            "import requests; from z3 import BitVec, Solver, sat; "
            "value=BitVec('value',8); solver=Solver(); solver.add(value+1==0); "
            "assert solver.check()==sat and solver.model()[value].as_long()==255; "
            "assert requests.Request('GET','http://target/example').prepare().url=='http://target/example'; "
            "print('Z3 and requests passed')"
        ),
        "strace -o /workspace/syscalls.trace /workspace/probe && grep -F 'write(' /workspace/syscalls.trace",
        (
            "ltrace -o /workspace/library.trace /workspace/probe && grep -F 'puts(' /workspace/library.trace"
            if has_ltrace
            else "if command -v ltrace >/dev/null 2>&1; then exit 1; fi; printf 'ltrace unavailable on ARM64\\n'"
        ),
        "cp /workspace/probe /workspace/probe-copy && patchelf --set-rpath /workspace /workspace/probe-copy "
        '&& test "$(patchelf --print-rpath /workspace/probe-copy)" = /workspace '
        "&& /workspace/probe-copy && printf 'patchelf passed\\n'",
        "printf '{\"answer\":42}\\n' | jq -e '.answer == 42'",
        "printf 'alpha\\nbeta\\n' > /workspace/text.txt && rg -n '^beta$' /workspace/text.txt",
    ]
    result, folder = asyncio.run(run_trial(task, tmp_path / "jobs", SAFE, commands))
    assert result.exception_info is None, (
        "Synthetic tools trial failed; inspect its local logs"
    )
    trace = json.loads((folder / "agent/trajectory.json").read_text())
    observations = [
        json.loads(step["observation"]["results"][0]["content"])
        for step in trace["steps"]
        if step.get("observation")
    ]
    for record, marker in zip(
        observations,
        (
            "Python 3.12.14",
            "synthetic output",
            "identity",
            "pwntools passed",
            "$1 = 6",
            "identity",
            "Z3 and requests passed",
            "write(",
            "puts(" if has_ltrace else "ltrace unavailable on ARM64",
            "patchelf passed",
            "true",
            "2:beta",
        ),
    ):
        assert record["exit_code"] == 0 and marker in record["stdout"], (
            "Synthetic tool check failed"
        )
    assert len(observations) == len(commands) + 1
    assert_isolation_and_cleanup(folder)
