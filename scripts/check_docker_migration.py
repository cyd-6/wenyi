"""Check both export wrappers against an isolated, unmodified official Compose API."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "5f206257ddc05b113b0ec224518e31cb5d7b62ad"
SENTINEL = "offline-docker-migration-fixture-not-a-real-key"

SEED = """
import json
from pathlib import Path
from uuid import uuid4
from wenyi_api.config import settings
from wenyi_api.db.pool import init_pool, close_pool
from wenyi_api import dal, project_service
from wenyi_core.ingest.models import Document, Chapter, Segment
from wenyi_core.pipeline.runstore import source_sha256

init_pool(settings.psycopg_dsn)
projects = []
for name in ("Selected project", "Preserved project"):
    pid = uuid4().hex[:16]
    path = Path(settings.data_dir) / pid / "source.txt"
    path.parent.mkdir(parents=True)
    path.write_text("An isolated migration fixture.", encoding="utf-8")
    digest = source_sha256(str(path))
    dal.create_project(name, "en", "zh", {"template": "快速出稿"}, project_id=pid,
        source={"source_path":str(path), "source_sha256":digest, "fmt":"text",
                "source_meta":{"original_filename":path.name}})
    storage = project_service.storage_for(pid)
    storage.init_from_document(Document(title=name, source_path=str(path), fmt="text",
        source_lang="en", target_lang="zh", chapters=[Chapter(index=0, title="One",
            segments=[Segment(index=0, source="Original", target="Docker 译文")])]))
    storage.save_usage({"total_tokens":42})
    dal.set_project_status(pid, "paused")
    projects.append({"id":pid, "sha256":digest})
print(json.dumps(projects))
close_pool()
"""


def check_archive(path: Path, selected: dict) -> None:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert [project["id"] for project in manifest["projects"]] == [selected["id"]]
        for name, record in manifest["entries"].items():
            value = archive.read(name)
            assert len(value) == record["size"]
            assert hashlib.sha256(value).hexdigest() == record["sha256"]
            assert SENTINEL.encode() not in value
        document = json.loads(archive.read(f"projects/{selected['id']}.json"))
        assert document["tables"]["segments"][0]["target"] == "Docker 译文"
        project = document["tables"]["projects"][0]
        assert project["source_sha256"] == selected["sha256"]
        assert project["usage"]["total_tokens"] == 42


def check(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    official = directory / "official"
    snapshot = subprocess.check_output(["git", "archive", BASELINE], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive:
        archive.extractall(official, filter="data")
    (official / "deploy/.env").write_text(f"DEEPSEEK_API_KEY={SENTINEL}\n", encoding="utf-8")
    migration = directory / "migration tools"
    migration.mkdir()
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/build_transfer_helper.py"),
            str(migration / "wenyi-transfer.pyz"),
        ],
        check=True,
    )
    for name in ("docker-transfer.sh", "docker-transfer.ps1"):
        shutil.copy2(ROOT / "scripts" / name, migration / name)
    environment = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": "wenyi_migration_" + uuid4().hex[:8],
        "INSTALL_PDF_OUTPUT": "false",
    }
    compose_file = str(official / "deploy/docker-compose.yml")
    compose = ["docker", "compose", "-f", compose_file]

    def run(command, **kwargs):
        return subprocess.run(command, env=environment, check=True, **kwargs)

    try:
        run([*compose, "up", "-d", "--build", "api"])
        for _ in range(90):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
                    if json.load(response)["status"] == "ok":
                        break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        else:
            raise TimeoutError("The official Compose API did not become healthy")
        seeded = run(
            [*compose, "exec", "-T", "api", "python", "-"],
            input=SEED,
            text=True,
            capture_output=True,
        )
        projects = json.loads(seeded.stdout)
        selected = projects[0]
        listing = run(
            ["bash", str(migration / "docker-transfer.sh"), "--compose", compose_file, "list"],
            capture_output=True,
            text=True,
        )
        assert all(project["id"] in listing.stdout for project in projects)
        for wrapper in ("bash", "powershell"):
            output = directory / f"{wrapper} selected.wenyi.zip"
            command = (
                [
                    "bash",
                    str(migration / "docker-transfer.sh"),
                    "--compose",
                    compose_file,
                    "export",
                    str(output),
                    selected["id"],
                ]
                if wrapper == "bash"
                else [
                    "pwsh",
                    "-NoProfile",
                    "-File",
                    str(migration / "docker-transfer.ps1"),
                    "-Command",
                    "export",
                    "-ComposeFile",
                    compose_file,
                    "-Output",
                    str(output),
                    "-Project",
                    selected["id"],
                ]
            )
            run(command)
            check_archive(output, selected)
            previous = hashlib.sha256(output.read_bytes()).hexdigest()
            duplicate = subprocess.run(command, env=environment, capture_output=True)
            assert duplicate.returncode != 0
            assert hashlib.sha256(output.read_bytes()).hexdigest() == previous
        # The original projects and source bytes survive both selected exports.
        verify = """
import json
from pathlib import Path
from wenyi_api.config import settings
from wenyi_api.db.pool import init_pool, close_pool
from wenyi_api import dal
from wenyi_core.pipeline.runstore import source_sha256
init_pool(settings.psycopg_dsn)
print(json.dumps([{"id":p["id"],"sha256":source_sha256(dal.get_project(p["id"])["source_path"])}
                  for p in dal.list_projects()]))
close_pool()
"""
        result = run(
            [*compose, "exec", "-T", "api", "python", "-"],
            input=verify,
            text=True,
            capture_output=True,
        )
        assert sorted(json.loads(result.stdout), key=lambda p: p["id"]) == sorted(
            projects, key=lambda p: p["id"]
        )
        print(
            "PASS: Bash and PowerShell wrappers export selected official Docker projects; content, usage, hashes, source preservation and overwrite protection verified."
        )
    except BaseException:
        subprocess.run([*compose, "logs", "--no-color", "--tail", "80"], env=environment)
        raise
    finally:
        # Only this randomly named, disposable Compose project is removed.
        subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], env=environment)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    check(parser.parse_args().work_dir)
