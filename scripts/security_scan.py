"""Scan the release tree for credential-like strings and local identifiers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {".git", "var", "dist", "__pycache__", ".pytest_cache", ".venv"}
TEXT_SUFFIXES = {".md", ".json", ".txt", ".log", ".py", ".sql", ".toml", ".cfg", ".ini"}
EXCLUDE_FILES = {"scripts/sanitize_release.py", "scripts/security_scan.py"}

PATTERNS = {
    "google_api_key": r"AIza[0-9A-Za-z_\-]{20,}",
    "oauth_token": r"ya29\.[0-9A-Za-z_\-]+",
    "bearer": r"Bearer\s+[A-Za-z0-9\-._~+/]+=*",
    "private_key": r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "service_account_email": r"[a-z0-9\-]+@[a-z0-9\-]+\.iam\.gserviceaccount\.com",
    "personal_email": r"[A-Za-z0-9._%+\-]+@(gmail|outlook|exu)\.(com|ac\.uk)",
    "local_user_path": r"C:\\\\Users\\\\[^\\\\\s\"]+",
    "local_python_path": r"C:\\\\Python[0-9]+",
}


def iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT, topdown=True, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in TEXT_SUFFIXES and str(path.relative_to(ROOT)).replace("\\", "/") not in EXCLUDE_FILES:
                yield path


def main() -> None:
    findings = []
    for path in iter_files():
        raw = path.read_bytes()
        if raw.startswith(b"\xff\xfe"):
            text = raw.decode("utf-16", errors="replace")
        elif raw.startswith(b"\xef\xbb\xbf"):
            text = raw.decode("utf-8-sig", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in PATTERNS.items():
                if re.search(pattern, line):
                    findings.append({"file": str(path.relative_to(ROOT)),
                                     "line": lineno, "type": name,
                                     "excerpt": line.strip()[:160]})
    out = ROOT / "reports" / "security-scan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"findings": findings, "count": len(findings)},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"count": len(findings),
                      "types": sorted({f["type"] for f in findings})},
                     ensure_ascii=False))
    for f in findings:
        print(f"{f['file']}:{f['line']} [{f['type']}] {f['excerpt']}")
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
