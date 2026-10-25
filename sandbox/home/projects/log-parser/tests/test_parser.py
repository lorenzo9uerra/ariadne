from parser import parse


def test_skips_empty_and_malformed_lines():
    lines = ["", "garbage", "2026-09-10 08:00:01 INFO started"]
    assert list(parse(lines)) == [("2026-09-10 08:00:01", "INFO", "started")]


def test_keeps_levels():
    lines = ["2026-09-10 08:00:02 ERROR disk full"]
    assert next(parse(lines))[1] == "ERROR"
