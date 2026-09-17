"""Build a dependency-free overlay that runs inside the official Docker API image."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(ROOT / "scripts/transfer_helper_main.py", "__main__.py")
        for name in ("__init__.py", "archive.py", "exporter.py"):
            archive.write(
                ROOT / "apps/api/wenyi_api/transfer" / name,
                f"wenyi_api/transfer/{name}",
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=ROOT / "dist/wenyi-transfer.pyz")
    build(parser.parse_args().output)
