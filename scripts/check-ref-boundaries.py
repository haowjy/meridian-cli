#!/usr/bin/env python3
"""CI grep gate: reference grammar and process identity have one owner."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRAMMAR = {
    "src/meridian/lib/core/refs.py",
    "src/meridian/pi_runtime/extensions/shared/ids.ts",
}
ENV = {
    "src/meridian/lib/core/identity.py",
    "src/meridian/pi_runtime/extensions/shared/pi_state_paths.ts",
}
ID_CHECK = re.compile(
    r"(?:startswith|startsWith)\([\"']p[\"']\)"
    r"|\[1:\]\.isdigit\(\)"
    r"|\^p(?:\\d|\[0-9\])"
    r"|\[0\]\s*==\s*[\"']p[\"']"
    r"|\[0\]\s+in\s+\{[\"']p[\"']"
)
ENV_READ = re.compile(
    r"(?:\.get|\.getenv|os\.getenv)\([\"']MERIDIAN_(?:SPAWN|RUN)_ID[\"']"
    r"|process\.env\.MERIDIAN_(?:SPAWN|RUN)_ID"
    r"|(?:environ|env)\[[\"']MERIDIAN_(?:SPAWN|RUN)_ID[\"']\](?!\s*=)"
)
violations: list[str] = []
for file in sorted((ROOT / "src" / "meridian").rglob("*")):
    if file.suffix not in {".py", ".ts"} or any(
        part in {"node_modules", "dist"} for part in file.parts
    ) or ".test." in file.name:
        continue
    relative = file.relative_to(ROOT).as_posix()
    source = file.read_text()
    for number, line in enumerate(source.splitlines(), 1):
        if relative not in GRAMMAR and ID_CHECK.search(line):
            violations.append(f"{relative}:{number}: use parse_ref/parseRef for ID grammar")
        if relative not in ENV and ENV_READ.search(line):
            violations.append(f"{relative}:{number}: use the central process identity accessor")
        if relative not in GRAMMAR and re.search(r"\bRunId\(", line):
            violations.append(f"{relative}:{number}: only parse_ref produces exact RunId keys")
if violations:
    raise SystemExit("\n".join(violations))
print("Reference boundary gate passed")
