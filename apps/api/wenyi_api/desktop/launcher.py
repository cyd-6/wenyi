"""Own a portable PostgreSQL, API and worker process tree for one data directory."""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

from .platform import InstanceLock, ProcessOwner

log = logging.getLogger(__name__)


def resources() -> Path:
    return (
        Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[4]
    )


def application_root() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else resources()


def free_port(preferred: int) -> int:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def launch_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "wenyi_api.desktop.launcher", *args]


class Supervisor:
    def __init__(self, root: Path, *, port: int = 8080):
        if os.name == "nt" and sys.getwindowsversion().build < 18362:
            raise RuntimeError("Wenyi requires Windows 10 version 1903 or later, or Windows 11.")
        self.root = root.resolve()
        self.data = self.root / "data"
        self.control = self.data / "runtime"
        self.logs = self.data / "logs"
        self.config = self.data / "config"
        try:
            for directory in (self.control, self.logs, self.config, self.data / "files"):
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RuntimeError(
                "Extract Wenyi into a writable directory, then try again."
            ) from error
        probe = self.control / f"write-test-{secrets.token_hex(4)}"
        try:
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as error:
            raise RuntimeError(
                "Extract Wenyi into a writable directory, then try again."
            ) from error
        self.lock = InstanceLock(self.control)
        self.owner = ProcessOwner()
        self.children: list[subprocess.Popen] = []
        self.handles = []
        self.port = free_port(port)
        self.pg_port = free_port(15432)
        self.token = secrets.token_urlsafe(32)
        self.url = f"http://127.0.0.1:{self.port}/#wenyi-token={self.token}"
        self.env = os.environ.copy()
        self.pg = Path(
            os.environ.get("WENYI_POSTGRES_BIN", str(self.root / "runtime" / "postgres" / "bin"))
        )
        self.database = self.data / "postgres"
        self.closed = False

    def _binary(self, name: str) -> str:
        return str(self.pg / (name + (".exe" if os.name == "nt" else "")))

    def _run(self, args: list[str], *, timeout=120) -> None:
        with (self.logs / "postgres.log").open("ab") as output:
            subprocess.run(
                args,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=self.env,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

    def _spawn(
        self, name: str, args: list[str], *, stop_file: str | None = None
    ) -> subprocess.Popen:
        output = (self.logs / f"{name}.log").open("ab")
        self.handles.append(output)
        env = dict(self.env)
        env["WENYI_CHILD_LOG"] = str(self.logs / f"{name}.log")
        if stop_file:
            env["WENYI_STOP_FILE"] = str(self.control / stop_file)
        process = self.owner.spawn(
            args,
            cwd=self.root,
            env=env,
            output=output,
            restrict_admin=name == "postgres",
        )
        self.children.append(process)
        return process

    def initialize(self) -> None:
        from dotenv import dotenv_values

        credentials = self.config / "credentials.env"
        if not credentials.exists():
            credentials.write_text(
                "# Provider credentials only. Edit, save, then restart Wenyi.\n"
                "# Use the environment variable names selected in Settings.\n"
                "DEEPSEEK_API_KEY=\nOPENAI_API_KEY=\nMINERU_API_KEY=\n",
                encoding="utf-8",
            )
        self.env.update(
            {
                key: value
                for key, value in dotenv_values(credentials, encoding="utf-8-sig").items()
                if value is not None
            }
        )
        temporary = self.control / "tmp"
        temporary.mkdir(exist_ok=True)
        self.env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
        default_config = self.config / "config.yaml"
        if not default_config.exists():
            default_config.write_bytes((resources() / "config.yaml").read_bytes())
        for name in ("api.stop", "workflow.stop", "export.stop", "launcher.stop"):
            (self.control / name).unlink(missing_ok=True)
        (self.control / "instance.json").unlink(missing_ok=True)
        password_file = self.config / "postgres-password"
        if not password_file.exists():
            password_file.write_text(secrets.token_hex(32), encoding="ascii")
            password_file.chmod(0o600)
        password = password_file.read_text(encoding="ascii").strip()
        self.env.update(
            {
                "WENYI_RUNTIME_BACKEND": "postgres",
                "WENYI_CONFIG": str(default_config),
                "DATA_DIR": str(self.data / "files"),
                "DATABASE_URL": f"postgresql://wenyi:{password}@127.0.0.1:{self.pg_port}/wenyi",
                "WENYI_API_TOKEN": self.token,
                "WENYI_CORS_ORIGINS": f"http://127.0.0.1:{self.port}",
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        native = self.root / "runtime" / "pango" / "bin"
        tokenizer = self.root / "runtime" / "tiktoken"
        if tokenizer.is_dir():
            self.env["TIKTOKEN_CACHE_DIR"] = str(tokenizer)
        if native.is_dir():
            self.env["WEASYPRINT_DLL_DIRECTORIES"] = str(native)
        fonts = self.root / "runtime" / "fonts"
        if fonts.is_dir():
            from xml.sax.saxutils import escape

            fontconfig = self.control / "fonts.conf"
            fontconfig.write_text(
                '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
                f"<dir>{escape(fonts.as_posix())}</dir><cachedir>{escape((self.control / 'font-cache').as_posix())}</cachedir>"
                "</fontconfig>",
                encoding="utf-8",
            )
            self.env["FONTCONFIG_FILE"] = str(fontconfig)
            ttf = fonts / "NotoSansCJKsc-Regular.ttf"
            if ttf.is_file():
                self.env.setdefault("TRANS_NOVEL_PDF_FONT", str(ttf))
        if not Path(self._binary("postgres")).is_file():
            raise RuntimeError(
                "Bundled PostgreSQL is missing. Extract the entire Windows ZIP, including runtime/."
            )
        if not (self.database / "PG_VERSION").exists():
            if self.database.exists() and any(self.database.iterdir()):
                raise RuntimeError(
                    "The database directory is incomplete. Preserve data/ and inspect data/logs/postgres.log before restoring a backup."
                )
            bootstrap = Path(tempfile.mkdtemp(prefix=".postgres-init-", dir=self.data))
            self._run(
                [
                    self._binary("initdb"),
                    "-D",
                    str(bootstrap),
                    "-U",
                    "wenyi",
                    "--encoding=UTF8",
                    "--locale=C",
                    "--auth=scram-sha-256",
                    "--pwfile",
                    str(password_file),
                ]
            )
            if self.database.exists():
                self.database.rmdir()
            bootstrap.rename(self.database)
        # Refuse a major-version mismatch instead of modifying an existing cluster.
        version = subprocess.check_output(
            [self._binary("postgres"), "--version"],
            env=self.env,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        major = version.split()[-1].split(".")[0]
        if (self.database / "PG_VERSION").read_text().strip() != major:
            raise RuntimeError(
                "The database needs a PostgreSQL major-version migration; keep the existing runtime."
            )

    def start(self) -> None:
        import psycopg

        self.initialize()
        self._spawn(
            "postgres",
            [
                self._binary("postgres"),
                "-D",
                str(self.database),
                "-h",
                "127.0.0.1",
                "-p",
                str(self.pg_port),
                "-c",
                "unix_socket_directories=",
            ],
        )
        dsn = self.env["DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"
        deadline = time.monotonic() + 90
        while True:
            self.check_children()
            try:
                with psycopg.connect(dsn, connect_timeout=2, autocommit=True) as conn:
                    if not conn.execute(
                        "SELECT 1 FROM pg_database WHERE datname='wenyi'"
                    ).fetchone():
                        conn.execute("CREATE DATABASE wenyi ENCODING 'UTF8'")
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "PostgreSQL did not become ready; see data/logs/postgres.log"
                    )
                time.sleep(0.25)
        self._spawn("workflow", launch_command("--child", "workflow"), stop_file="workflow.stop")
        self._spawn("export", launch_command("--child", "export"), stop_file="export.stop")
        self._spawn(
            "api", launch_command("--child", "api", "--port", str(self.port)), stop_file="api.stop"
        )
        deadline = time.monotonic() + 120
        while True:
            self.check_children()
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            try:
                connection.request("GET", "/api/health")
                result = json.loads(connection.getresponse().read())
                if result.get("status") == "ok" and result.get("db") == "ok":
                    break
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            if time.monotonic() > deadline:
                raise RuntimeError("Wenyi did not become ready; see data/logs.")
            time.sleep(0.3)
        write_json(
            self.control / "instance.json", {"url": self.url, "pid": os.getpid(), "port": self.port}
        )

    def check_children(self) -> None:
        for child in self.children:
            if child.poll() is not None:
                raise RuntimeError(f"A Wenyi service exited ({child.returncode}); see data/logs.")

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        for name in ("api.stop", "workflow.stop", "export.stop"):
            (self.control / name).touch()
        deadline = time.monotonic() + 90
        for child in self.children[1:]:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=10)
        if self.children and self.children[0].poll() is None:
            try:
                self._run(
                    [self._binary("pg_ctl"), "-D", str(self.database), "-m", "fast", "-w", "stop"],
                    timeout=60,
                )
            except (subprocess.SubprocessError, OSError):
                self.children[0].terminate()
            self.children[0].wait(timeout=10)
        self.owner.close()
        for output in self.handles:
            output.close()
        (self.control / "instance.json").unlink(missing_ok=True)
        self.lock.close()


def existing_instance(control: Path) -> str:
    for _ in range(120):
        try:
            return json.loads((control / "instance.json").read_text(encoding="utf-8"))["url"]
        except (OSError, ValueError, KeyError):
            time.sleep(1)
    raise RuntimeError("Wenyi is already starting. Inspect data/logs if it does not open.")


def run_tray(supervisor: Supervisor) -> None:
    import pystray
    from PIL import Image

    emblem = resources() / "wenyi-emblem.png"
    if not emblem.exists():
        emblem = resources() / "apps/web/src/assets/wenyi-emblem.png"

    def open_path(path: Path):
        os.startfile(str(path))

    icon = pystray.Icon(
        "wenyi",
        Image.open(emblem),
        "Wenyi",
        menu=pystray.Menu(
            pystray.MenuItem("Open Wenyi", lambda: webbrowser.open(supervisor.url), default=True),
            pystray.MenuItem("Open data folder", lambda: open_path(supervisor.data)),
            pystray.MenuItem("View logs", lambda: open_path(supervisor.logs)),
            pystray.MenuItem(
                "Edit credentials (restart to apply)",
                lambda: subprocess.Popen(
                    ["notepad.exe", str(supervisor.config / "credentials.env")]
                ),
            ),
            pystray.MenuItem("Pause and exit", lambda: icon.stop()),
        ),
    )

    def monitor():
        while not supervisor.closed:
            try:
                supervisor.check_children()
                if (supervisor.control / "launcher.stop").exists():
                    icon.stop()
                    return
            except RuntimeError:
                log.exception("A service stopped")
                icon.notify(
                    "A service stopped. See data/logs; saved progress can be resumed.", "Wenyi"
                )
                icon.stop()
                return
            time.sleep(1)

    threading.Thread(target=monitor, daemon=True).start()
    icon.run()


def main(argv: list[str] | None = None) -> int:
    # Windowed PyInstaller executables start without Python's standard streams,
    # including child modes. Uvicorn and model libraries expect writable streams.
    if sys.stdout is None or sys.stderr is None:
        output = open(
            os.environ.get("WENYI_CHILD_LOG", os.devnull), "a", encoding="utf-8", buffering=1
        )
        sys.stdout = sys.stdout or output
        sys.stderr = sys.stderr or output
    if sys.platform == "win32":
        # Psycopg's asynchronous connections require selector support on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    parser = argparse.ArgumentParser(description="Wenyi portable Web UI")
    parser.add_argument("--root", type=Path, default=application_root())
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--headless", action="store_true", help="Run without tray or browser (automation)"
    )
    parser.add_argument("--child", choices=["api", "workflow", "export"], help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__import__("wenyi_api").__version__)
    args = parser.parse_args(argv)
    if args.child:
        if args.child == "api":
            from .server import serve

            web = resources() / ("web" if getattr(sys, "frozen", False) else "apps/web/dist")
            asyncio.run(serve(web, args.port))
        else:
            from ..runtime.queue import EXPORT_QUEUE, WORKFLOW_QUEUE, serve

            asyncio.run(serve(EXPORT_QUEUE if args.child == "export" else WORKFLOW_QUEUE))
        return 0
    supervisor = None
    try:
        supervisor = Supervisor(args.root, port=args.port)
        if not supervisor.lock.acquire():
            url = existing_instance(supervisor.control)
            supervisor.lock.close()
            supervisor.owner.close()
            if not args.headless:
                webbrowser.open(url)
            return 0
        logging.basicConfig(
            filename=supervisor.logs / "launcher.log",
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            encoding="utf-8",
        )
        supervisor.start()
        if args.headless:
            while not (supervisor.control / "launcher.stop").exists():
                supervisor.check_children()
                time.sleep(0.5)
        else:
            webbrowser.open(supervisor.url)
            run_tray(supervisor)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        log.exception("Wenyi could not start")
        if os.name == "nt" and not args.headless:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, str(error), "Wenyi", 0x10)
        else:
            print(str(error), file=sys.stderr)
        return 1
    finally:
        if supervisor is not None and supervisor.children:
            supervisor.shutdown()
        elif supervisor is not None:
            supervisor.owner.close()
            supervisor.lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
