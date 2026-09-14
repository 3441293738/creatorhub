"""Fail a release build if an Inno message would fall back to English."""
import argparse
from collections import Counter
import configparser
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read_messages(path):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    with Path(path).open(encoding="utf-8-sig") as stream:
        parser.read_file(stream)
    return dict(parser["Messages"])


def validate_messages(default_file, translated_file):
    original = read_messages(default_file)
    translated = read_messages(translated_file)
    missing = sorted(original.keys() - translated.keys())
    if missing:
        raise ValueError("Missing Chinese installer messages: " + ", ".join(missing))
    # Newlines and accelerator keys may vary, but substitution arguments must not.
    tokens = lambda value: Counter(re.findall(r"%\d+|\[[a-z]+(?:/[a-z]+)?\]", value))
    changed = [key for key, value in original.items() if tokens(value) != tokens(translated[key])]
    if changed:
        raise ValueError("Changed installer message placeholders: " + ", ".join(changed))
    return len(original)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compiler", required=True, help="Path to the ISCC.exe used for this build")
    args = parser.parse_args()
    count = validate_messages(Path(args.compiler).resolve().with_name("Default.isl"),
                              ROOT / "desktop/languages/ChineseSimplified.isl")
    print(f"PASS: {count} Inno messages translated; all substitution arguments preserved")
