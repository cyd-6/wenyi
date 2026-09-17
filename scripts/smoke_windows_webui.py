"""Exercise the extracted portable application with a local mock model, then move it.

The controller uses Python, but the frozen application is launched with only the
Windows system directory in PATH. --source runs the same checks from a Linux
checkout for development; it does not constitute Windows artifact acceptance.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = request["messages"][-1]["content"]
        count = len(re.findall(r"^\[\d+\]", text, re.MULTILINE))
        content = (
            json.dumps(
                {
                    "translations": [f"中文译文 {i} 日本語 한국어" for i in range(count)],
                    "titles": [f"标题 {i}" for i in range(count)],
                },
                ensure_ascii=False,
            )
            if count
            else "[]"
        )
        data = json.dumps(
            {
                "id": "local-smoke",
                "object": "chat.completion",
                "created": 1,
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def multipart(fields: dict, name: str, content: bytes) -> tuple[bytes, str]:
    boundary = "wenyi-smoke-boundary"
    result = bytearray()
    for key, value in fields.items():
        result.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    result.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
    )
    result.extend(content)
    result.extend(f"\r\n--{boundary}--\r\n".encode())
    return bytes(result), f"multipart/form-data; boundary={boundary}"


class Application:
    def __init__(self, root: Path, source: bool, port: int):
        self.root, self.source, self.port = root, source, port
        self.process = None
        self.base, self.token = "", ""
        self.env = os.environ.copy()
        if not source:
            self.env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
            for key in (
                "PYTHONPATH",
                "PYTHONHOME",
                "WENYI_POSTGRES_BIN",
                "WEASYPRINT_DLL_DIRECTORIES",
                "FONTCONFIG_FILE",
                "TRANS_NOVEL_PDF_FONT",
            ):
                self.env.pop(key, None)

    def command(self) -> list[str]:
        return (
            [sys.executable, "-m", "wenyi_api.desktop.launcher"]
            if self.source
            else [str(self.root / "Wenyi.exe")]
        ) + ["--root", str(self.root), "--headless", "--port", str(self.port)]

    def start(self):
        self.process = subprocess.Popen(
            self.command(), env=self.env, cwd=ROOT if self.source else self.root
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Launcher exited with {self.process.returncode}; logs in {self.root / 'data/logs'}"
                )
            path = self.root / "data/runtime/instance.json"
            if path.exists():
                info = json.loads(path.read_text(encoding="utf-8"))
                url = urlsplit(info["url"])
                self.base = f"{url.scheme}://{url.netloc}"
                self.token = parse_qs(url.fragment)["wenyi-token"][0]
                if self.request("/api/health")["status"] == "ok":
                    return
            time.sleep(0.2)
        raise TimeoutError(f"Launcher not ready; logs in {self.root / 'data/logs'}")

    def request(self, path: str, body=None, *, method=None, content_type=None, raw=False):
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
                content_type = "application/json"
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base + path, data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
                return data if raw else json.loads(data)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"{path}: {error.code} {error.read().decode()}") from error

    def idle(self, pid: str, expected: str):
        for _ in range(600):
            project = self.request(f"/api/projects/{pid}")
            if project["status"] == expected:
                return project
            if project["status"] in {"error", "paused"}:
                raise RuntimeError(f"Unexpected project status: {project}")
            time.sleep(0.2)
        raise TimeoutError(f"Project did not reach {expected}")

    def export(self, pid: str, format: str, **options):
        job = self.request(f"/api/projects/{pid}/exports", {"format": format, **options})
        for _ in range(600):
            rows = self.request(f"/api/projects/{pid}/exports")
            row = next(item for item in rows if item["id"] == job["export_id"])
            if row["status"] == "done":
                return self.request(f"/api/projects/{pid}/exports/{row['id']}/download", raw=True)
            if row["status"] == "error":
                raise RuntimeError(row["error"])
            time.sleep(0.2)
        raise TimeoutError("Export did not complete")

    def stop(self):
        if self.process and self.process.poll() is None:
            (self.root / "data/runtime/launcher.stop").touch()
            self.process.wait(timeout=130)
            if self.process.returncode:
                raise RuntimeError(f"Launcher shutdown failed: {self.process.returncode}")


def smoke(args):
    directory = args.work_dir or Path(tempfile.mkdtemp(prefix="wenyi-smoke-"))
    root = directory / "中文 path with spaces"
    root.mkdir(parents=True)
    if args.source and args.font_dir:
        shutil.copytree(args.font_dir, root / "runtime/fonts")
    if not args.source:
        with zipfile.ZipFile(args.archive) as archive:
            archive.extractall(root)
        inventory = json.loads((root / "bundle-manifest.json").read_text(encoding="utf-8"))
        import hashlib

        for name, checksum in inventory["files"].items():
            with (root / name).open("rb") as handle:
                assert hashlib.file_digest(handle, "sha256").hexdigest() == checksum, name
    model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    config = root / "data/config"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text(
        "language:\n  source: en\n  target: zh\nllm:\n  providers:\n    mock:\n"
        f"      kind: openai\n      base_url: http://127.0.0.1:{model.server_port}/v1\n      api_key_env: WENYI_SMOKE_API_KEY\n"
        "  models:\n    smoke:\n      provider: mock\n      model: smoke\n  tiers:\n    strong: smoke\n    cheap: smoke\n    fast: smoke\n"
        "pipeline:\n  book_understanding: false\n  polish: false\n  review: false\n  review_autofix: false\n",
        encoding="utf-8",
    )
    (config / "credentials.env").write_text(
        "WENYI_SMOKE_API_KEY=offline-fixture\n", encoding="utf-8"
    )
    conflict = socket.socket()
    conflict.bind(("127.0.0.1", 0))
    conflict.listen()
    app = Application(root, args.source, conflict.getsockname()[1])
    try:
        app.start()
        assert urlsplit(app.base).port != app.port
        subprocess.run(
            app.command(), env=app.env, cwd=ROOT if args.source else root, check=True, timeout=30
        )
        assert len(app.request("/api/health")["workers"]) == 2
        assert b"<html" in app.request("/transfers", raw=True)
        try:
            urllib.request.urlopen(app.base + "/api/projects", timeout=10)
            raise AssertionError("Unauthenticated API request was accepted")
        except urllib.error.HTTPError as error:
            assert error.code == 401
        body, content_type = multipart(
            {
                "project": json.dumps(
                    {
                        "name": "Portable 中文",
                        "source_lang": "en",
                        "target_lang": "zh",
                        "strategy": {"template": "快速出稿"},
                    }
                )
            },
            "book.txt",
            b"A quiet morning.\n\nThe sun rose.",
        )
        project = app.request("/api/projects", body, content_type=content_type)
        pid = project["id"]
        app.idle(pid, "uploaded")
        app.request(f"/api/projects/{pid}/translate", {})
        app.idle(pid, "done")
        chapter = app.request(f"/api/projects/{pid}/chapters/0")
        assert "中文译文" in chapter["segments"][0]["target"]
        app.request(
            f"/api/projects/{pid}/review/0/segments/0",
            {
                "target": "人工修订 中文 日本語 한국어",
                "expected_target": chapter["segments"][0]["target"],
            },
            method="PUT",
        )
        for format in ("txt", "html", "epub", "docx"):
            assert len(app.export(pid, format)) > 20
        if not args.skip_pdf:
            for engine in ("weasyprint", "fpdf2"):
                pdf = app.export(pid, "pdf", pdf_engine=engine)
                assert pdf.startswith(b"%PDF")
                from pypdf import PdfReader

                text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
                assert all(sample in text for sample in ("人工修订", "日本語", "한국어")), text
                (directory / f"font-check-{engine}.pdf").write_bytes(pdf)
        if args.browser:
            os.environ["WENYI_CONFIG"] = str(config / "config.yaml")
            os.environ["DATA_DIR"] = str(root / "data/files")
            from psycopg_pool import ConnectionPool
            from wenyi_api.transfer.exporter import export_projects

            password = (root / "data/config/postgres-password").read_text(encoding="ascii").strip()
            port = (
                (root / "data/postgres/postmaster.pid").read_text(encoding="utf-8").splitlines()[3]
            )
            archive = directory / "docker-project.wenyi.zip"
            # The isolated application owns this cluster. Never connect to a user's database.
            with ConnectionPool(f"postgresql://wenyi:{password}@127.0.0.1:{port}/wenyi") as pool:
                pool.wait()
                export_projects(pool, root / "data/files", [pid], archive)
            info = json.loads((root / "data/runtime/instance.json").read_text(encoding="utf-8"))
            result = directory / "browser-result.json"
            subprocess.run(
                [shutil.which("node"), str(ROOT / "apps/web/tests/native-browser.mjs")],
                input=json.dumps(
                    {
                        "url": info["url"],
                        "archive": str(archive),
                        "screenshot": str(directory / "migration.png"),
                        "result": str(result),
                    }
                ),
                text=True,
                check=True,
                timeout=120,
            )
            imported = json.loads(result.read_text(encoding="utf-8"))["project_id"]
            assert imported != pid
            assert "人工修订" in app.export(imported, "txt").decode("utf-8")
        app.stop()
        moved = directory / "搬动后的 folder"
        shutil.move(root, moved)
        app.root = moved
        app.start()
        assert "人工修订" in app.request(f"/api/projects/{pid}/chapters/0")["segments"][0]["target"]
        assert "人工修订" in app.export(pid, "txt").decode("utf-8")
        environment = (
            "source runtime (not Windows acceptance)"
            if args.source
            else "extracted Windows runtime with minimal PATH"
        )
        print(
            f"PASS: {environment}, loopback auth, duplicate launch, port collision, upload, mock translation, revision, exports, restart and moved data. Evidence: {directory}",
            flush=True,
        )
    finally:
        app.stop()
        model.shutdown()
        model.server_close()
        conflict.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "archive", type=Path, nargs="?", default=ROOT / "dist/wenyi-webui-windows-x64.zip"
    )
    parser.add_argument("--source", action="store_true")
    parser.add_argument("--font-dir", type=Path, help="Bundled font fixtures for --source checks")
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Check the real host with Playwright and migrate a project",
    )
    parser.add_argument("--work-dir", type=Path)
    smoke(parser.parse_args())
