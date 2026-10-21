"""Write SHA256SUMS.txt next to the installer in dist."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(dist: Path, version: str) -> None:
    installer = dist / f"XynClaim_{version}_x64-setup.exe"
    if not installer.is_file():
        raise SystemExit(f"Installer missing: {installer.name}")
    sums = f"{sha256(installer)}  {installer.name}\n"
    (dist / "SHA256SUMS.txt").write_text(sums, encoding="ascii", newline="\n")
    print(sums, end="")


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
