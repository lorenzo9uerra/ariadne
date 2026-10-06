"""Scripts that run inside the containers.

They are sent inline (`python3 -I -c` or `bash -c`), so nothing is written into
a container to run them. They use only the standard library and plain syntax,
because they run on whatever Python 3 a target image provides.
"""

from pathlib import Path


def script(name: str) -> str:
    return (Path(__file__).parent / name).read_text()
