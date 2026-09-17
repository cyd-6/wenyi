"""Build the portable Windows x64 WebUI on Windows (Python 3.12).

Install locked desktop/pdf dependencies and build apps/web first. MSYS2's UCRT64
Pango package and the Visual C++ x64 redistributable DLL directory are build inputs,
not prerequisites on the user's machine. No build step uses application data/.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from build_transfer_helper import build as build_transfer

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def download(record: dict, cache: Path) -> Path:
    path = cache / record["url"].rsplit("/", 1)[1]
    if not path.exists() or sha256(path) != record["sha256"]:
        partial = path.with_suffix(".partial")
        with (
            urllib.request.urlopen(record["url"], timeout=120) as source,
            partial.open("wb") as target,
        ):
            shutil.copyfileobj(source, target)
        if sha256(partial) != record["sha256"]:
            partial.unlink()
            raise RuntimeError(f"Download checksum mismatch: {record['url']}")
        partial.replace(path)
    return path


def copy_python_licenses(target: Path) -> dict:
    packages = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        packages[name] = distribution.version
        for file in distribution.files or []:
            if not any(word in file.name.lower() for word in ("license", "copying", "notice")):
                continue
            source = Path(distribution.locate_file(file))
            if source.is_file() and ".dist-info" in str(file):
                directory = target / name
                directory.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, directory / file.name)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, target / "Python-LICENSE.txt")
    return packages


def build(msys: Path, crt: Path) -> Path:
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.version_info[:2] != (3, 12):
        raise RuntimeError(
            "Build on Windows x64 using Python 3.12; PyInstaller cannot cross-compile"
        )
    if not (ROOT / "apps/web/dist/index.html").is_file():
        raise RuntimeError("Build the WebUI first: pnpm -C apps/web build")
    if not (msys / "ucrt64/bin/libpango-1.0-0.dll").exists():
        raise RuntimeError("Install mingw-w64-ucrt-x86_64-pango in the MSYS2 build environment")
    if not (crt / "msvcp140.dll").is_file():
        raise RuntimeError(
            "--crt-dir must contain the Visual C++ x64 app-local redistributable DLLs"
        )
    (ROOT / "build").mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="windows-webui-", dir=ROOT / "build"))
    cache = ROOT / "build/windows-downloads"
    cache.mkdir(exist_ok=True)
    manifest = json.loads((ROOT / "scripts/windows-runtime.json").read_text(encoding="utf-8"))
    downloads = {name: download(record, cache) for name, record in manifest.items()}
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--name",
        "Wenyi",
        "--onedir",
        "--windowed",
        "--clean",
        "--noconfirm",
        "--distpath",
        str(work),
        "--workpath",
        str(work / "pyinstaller"),
        "--specpath",
        str(work),
        "--paths",
        str(ROOT / "packages/core"),
        "--paths",
        str(ROOT / "apps/api"),
    ]
    for package in (
        "wenyi_core",
        "wenyi_api",
        "uvicorn",
        "psycopg",
        "psycopg_binary",
        "pystray",
        "tiktoken",
        "tiktoken_ext",
        "weasyprint",
        "fpdf",
        "google.genai",
    ):
        command += ["--collect-all", package]
    for package in ("wenyi-core", "wenyi-api"):
        command += ["--copy-metadata", package]
    for source, destination in (
        ("apps/web/dist", "web"),
        ("config.yaml", "."),
        ("apps/web/src/assets/wenyi-emblem.png", "."),
    ):
        command += ["--add-data", f"{ROOT / source}{os.pathsep}{destination}"]
    command += [str(ROOT / "scripts/desktop_entry.py")]
    subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        env={**os.environ, "WEASYPRINT_DLL_DIRECTORIES": str(msys / "ucrt64/bin")},
    )
    stage = work / "Wenyi"
    runtime = stage / "runtime"
    tokenizer = runtime / "tiktoken"
    tokenizer.mkdir(parents=True)
    subprocess.run(
        [sys.executable, "-c", "import tiktoken; tiktoken.get_encoding('cl100k_base')"],
        env={**os.environ, "TIKTOKEN_CACHE_DIR": str(tokenizer)},
        check=True,
    )
    pg = runtime / "postgres"
    with zipfile.ZipFile(downloads["postgres"]) as archive:
        for info in archive.infolist():
            path = Path(info.filename)
            if info.is_dir() or path.parts[0] != "pgsql":
                continue
            if (
                path.parts[1] not in {"bin", "lib", "share"}
                and not path.name.endswith("licenses.txt")
                and path.name != "server_license.txt"
            ):
                continue
            target = pg.joinpath(*path.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    if (
        not (pg / "lib/pg_trgm.dll").exists()
        or not (pg / "share/extension/pg_trgm.control").exists()
    ):
        raise RuntimeError("Bundled PostgreSQL must include pg_trgm")
    pango = runtime / "pango/bin"
    pango.mkdir(parents=True)
    for file in (msys / "ucrt64/bin").glob("*.dll"):
        shutil.copy2(file, pango / file.name)
    shutil.copytree(msys / "ucrt64/share/licenses", runtime / "pango/licenses")
    # MSVC runtime is loaded app-locally; end users never run a redistributable installer.
    for file in crt.glob("*.dll"):
        for directory in (stage, stage / "_internal", pg / "bin"):
            shutil.copy2(file, directory / file.name)
    fonts = runtime / "fonts"
    fonts.mkdir()
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    with TTFont(downloads["sans"]) as variable:
        static = instantiateVariableFont(variable, {"wght": 400}, inplace=False)
        static.save(fonts / "NotoSansCJKsc-Regular.ttf")
    shutil.copy2(downloads["serif"], fonts / "NotoSerifCJKsc-Regular.otf")
    shutil.copy2(downloads["font_license"], fonts / "OFL.txt")
    transfer = stage / "migration"
    build_transfer(transfer / "wenyi-transfer.pyz")
    for name in ("docker-transfer.sh", "docker-transfer.ps1"):
        shutil.copy2(ROOT / "scripts" / name, transfer / name)
    docs = stage / "docs"
    docs.mkdir()
    for name, source in (
        ("windows.md", "docs/windows.md"),
        ("zh/windows.md", "docs/zh/windows.md"),
        ("windows-third-party.md", "docs/windows-third-party.md"),
    ):
        (docs / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, docs / name)
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE")
    shutil.copy2(ROOT / "scripts/windows-runtime.json", docs / "windows-runtime.json")
    licenses = stage / "licenses/python"
    licenses.mkdir(parents=True)
    packages = copy_python_licenses(licenses)
    (stage / "README.txt").write_text(
        "Wenyi portable WebUI\n\nExtract the entire ZIP to a writable folder and double-click Wenyi.exe.\n"
        "All state is stored beside Wenyi.exe in data/. Use the tray menu to pause and exit before moving the folder.\n"
        "Credentials: data/config/credentials.env (restart after editing).\n"
        "Instructions: docs/windows.md and docs/zh/windows.md\n",
        encoding="utf-8",
    )
    inventory = {
        "python": sys.version,
        "packages": packages,
        "downloads": manifest,
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "files": {
            str(path.relative_to(stage).as_posix()): sha256(path)
            for path in sorted(stage.rglob("*"))
            if path.is_file()
        },
    }
    (stage / "bundle-manifest.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    destination = dist / "wenyi-webui-windows-x64.zip"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(stage.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(stage).as_posix())
    destination.with_suffix(".zip.sha256").write_text(
        f"{sha256(destination)}  {destination.name}\n", encoding="ascii"
    )
    print(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--msys-root", type=Path, default=Path("C:/msys64"))
    parser.add_argument("--crt-dir", type=Path, required=True)
    args = parser.parse_args()
    build(args.msys_root, args.crt_dir)
