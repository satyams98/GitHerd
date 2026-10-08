from pathlib import Path

from typer.testing import CliRunner

from githerd.cli import app

SRC = Path(__file__).resolve().parent.parent / "src" / "githerd"
ALLOWED_CONTROLS = {0x09, 0x0A, 0x0D}  # TAB, LF, CR
THEME = SRC / "ui" / "theme.py"  # the only file that defines non-ASCII glyphs


def _sources():
    files = sorted(SRC.rglob("*.py"))
    assert files, "no sources found"
    return files


def test_no_raw_control_characters_in_sources():
    offenders = []
    for path in _sources():
        data = path.read_bytes()
        for offset, byte in enumerate(data):
            if (byte < 0x20 and byte not in ALLOWED_CONTROLS) or byte == 0x7F:
                line = data.count(b"\n", 0, offset) + 1
                offenders.append(f"{path.relative_to(SRC)}:{line}: 0x{byte:02x}")
    assert offenders == []


def test_only_the_theme_module_contains_non_ascii_text():
    offenders = [
        str(path.relative_to(SRC)) for path in _sources()
        if path != THEME and not path.read_bytes().isascii()
    ]
    assert offenders == []


def test_help_exit_code_tables_keep_one_code_per_line():
    runner = CliRunner()
    pull = runner.invoke(app, ["pull", "--help"]).output.splitlines()
    for code, text in (("0", "everything is up to date"), ("2", "at least one repo still needs"),
                       ("130", "interrupted with Ctrl+C")):
        assert any(ln.split()[:1] == [code] and text in ln for ln in pull if ln.strip()), code
    undo = runner.invoke(app, ["undo", "--help"]).output.splitlines()
    for code, text in (("0", "every repo was restored"), ("2", "at least one repo was skipped"),
                       ("130", "interrupted with Ctrl+C")):
        assert any(ln.split()[:1] == [code] and text in ln for ln in undo if ln.strip()), code
    assert all("\x08" not in ln for ln in pull + undo)


# Bidi controls and Unicode line/paragraph separators must be written as \u escapes in tests,
# never as raw characters (they are invisible and make a file read differently than it runs).
# The set is built from code points so this file stays free of them itself.
_RAW_BIDI = (
    {chr(cp) for cp in range(0x202A, 0x202F)}
    | {chr(cp) for cp in range(0x2066, 0x206A)}
    | {chr(0x2028), chr(0x2029)}
)
TESTS = Path(__file__).resolve().parent


def test_no_raw_bidi_controls_or_line_separators_in_tests():
    files = sorted(TESTS.rglob("*.py"))
    assert files, "no test files found"
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.split("\n"), start=1):
            for ch in sorted(_RAW_BIDI.intersection(line)):
                offenders.append(f"{path.relative_to(TESTS)}:{number}: U+{ord(ch):04X}")
    assert offenders == []
