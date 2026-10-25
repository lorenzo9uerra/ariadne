"""Summarize an application log by level and message."""

import argparse
import re
from collections import Counter

LINE = re.compile(r"^(\S+ \S+) (DEBUG|INFO|WARNING|ERROR) (.*)$")


def parse(lines):
    """Yield (timestamp, level, message) for each well-formed line."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = LINE.match(line)
        if match:
            yield match.groups()


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("path")
    arguments.add_argument("--level")
    options = arguments.parse_args()
    with open(options.path) as stream:
        entries = list(parse(stream))
    if options.level:
        entries = [e for e in entries if e[1] == options.level.upper()]
    levels = Counter(level for _, level, _ in entries)
    for level, count in levels.most_common():
        print(f"{level:8} {count}")
    print()
    for message, count in Counter(m for _, _, m in entries).most_common(5):
        print(f"{count:5}  {message}")


if __name__ == "__main__":
    main()
