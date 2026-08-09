#!/usr/bin/env python3
from pathlib import Path
import re
import tomllib


root = Path(__file__).resolve().parent.parent
with (root / "pyproject.toml").open("rb") as handle:
    version = tomllib.load(handle)["project"]["version"]

client = (root / "src/logister/client.py").read_text(encoding="utf-8")
changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")

if f'logister-python/{version}' not in client:
    raise SystemExit(f"Default SDK user agent does not match package version {version}.")
if not re.search(rf"^## v{re.escape(version)}(?:\s|-|$)", changelog, re.MULTILINE):
    raise SystemExit(f"CHANGELOG.md is missing a v{version} heading.")

print(f"Release metadata is consistent for v{version}.")
