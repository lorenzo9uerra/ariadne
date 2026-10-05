"""Invert the player binary with the upstream solver's arithmetic.

The event solver hardcoded its check array and program string. This copy reads
those two values from the generated ELF. The inversion loop is the upstream
loop; the progress prints are omitted so the tool result is the recovered flag.
"""

import re
import struct
import subprocess
import sys
from pathlib import Path


def program_string(blob: bytes) -> str:
    found = {
        match.decode("ascii")
        for match in re.findall(rb"\?[ -~]{8,}?!", blob)
        if set(match.decode("ascii")) <= set("?WHAT!")
    }
    if len(found) != 1:
        raise RuntimeError("Expected one embedded program string")
    return found.pop()


def command(args: list[str]) -> bytes:
    result = subprocess.run(args, check=True, capture_output=True)
    return result.stdout


def symbol_span(path: str, name: str) -> tuple[int, int]:
    for line in command(["nm", "-S", "--defined-only", path]).decode().splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[3] == name:
            return int(parts[0], 16), int(parts[1], 16)
    raise RuntimeError("The check array symbol is missing")


def file_offset(path: str, address: int, size: int) -> int:
    for line in command(["readelf", "-S", "-W", path]).decode().splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[0].startswith("["):
            continue
        try:
            start = int(parts[3], 16)
            offset = int(parts[4], 16)
            span = int(parts[5], 16)
        except ValueError:
            continue
        if start <= address and address + size <= start + span:
            return offset + (address - start)
    raise RuntimeError("The check array is not in a loaded section")


def load_checks(path: str) -> tuple[list[int], str]:
    blob = Path(path).read_bytes()
    program = program_string(blob)
    address, size = symbol_span(path, "solution")
    if size % 8:
        raise RuntimeError("The check array is not a list of 64-bit values")
    offset = file_offset(path, address, size)
    raw = blob[offset : offset + size]
    if len(raw) != size:
        raise RuntimeError("The check array is truncated")
    values = list(struct.unpack(f"<{size // 8}q", raw))
    groups = [group for group in program[:-1].split("?")[1:] if group != "T"]
    if len(values) != len(groups):
        raise RuntimeError("The check array does not match the program")
    return values, program


def recover(values: list[int], program: str) -> str:
    instrs = program[:-1].split("?")[1:]
    instrs = [group for group in instrs if group != "T"]
    n = ["W", "H", "A", "T"]
    n = [ord(char) for char in n]
    solve = ""
    total_idx = 0
    for index, solution in enumerate(values):
        idx = (total_idx + len(instrs[index]) - 1) % 4
        total_idx += len(instrs[index]) - 1
        for operation in reversed(instrs[index]):
            if operation == "W":
                solution = solution ^ n[idx]
            if operation == "H":
                solution = solution - n[idx]
            if operation == "A":
                solution = solution // n[idx]
            idx -= 1
            if idx == -1:
                idx = 3
        solve += chr(solution)
    return solve


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(1)
    values, program = load_checks(sys.argv[1])
    print(recover(values, program))


if __name__ == "__main__":
    main()
