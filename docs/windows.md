# Portable Windows WebUI

[中文](zh/windows.md)

The Windows WebUI reuses the official React interface, FastAPI API and translation engine. It targets Windows 10 (1903 or later)/11 x64. The portable distribution is `wenyi-webui-windows-x64.zip`, built **on Windows** by the `Windows portable WebUI` workflow. A source checkout or a successful Linux test is not a validated Windows release. Before distributing a build, complete the Windows acceptance checks below.

## Start and configure

1. Download the ZIP and its `.zip.sha256` file from a successful workflow run. Compare `Get-FileHash .\wenyi-webui-windows-x64.zip -Algorithm SHA256` with the checksum file.
2. Extract the **entire** ZIP into a writable folder, for example `D:\Tools\Wenyi`. Keep `Wenyi.exe`, `_internal/` and `runtime/` together. Do not run from inside the ZIP or put the app in a protected installation directory.
3. Double-click `Wenyi.exe`. First startup initializes its own PostgreSQL cluster. The browser opens when the API and both workers are ready. The normal address is `http://127.0.0.1:8080`; an occupied port is replaced with an available one.
4. Use the tray's **Edit credentials (restart to apply)** item, fill `data/config/credentials.env`, save, exit through the tray and restart. Use the exact environment variable names configured under model settings. The file is UTF-8 dotenv syntax, for example `MY_PROVIDER_API_KEY=your-own-key`. Configure endpoints and model IDs in the WebUI. Keys remain environment variables; they are never stored in project migration archives.

The ZIP contains Python, PostgreSQL 16 including `pg_trgm`, Pango libraries, WeasyPrint, fpdf2, CJK fonts and the tokenizer cache. Docker, WSL, Python, Node.js, Redis, a database installation and administrator rights are not runtime requirements. LLM requests still require your selected provider; MinerU and BabelDOC retain their existing external-service requirements.

The tray provides **Open Wenyi**, **Open data folder**, **View logs**, **Edit credentials** and **Pause and exit**. Opening the same EXE again opens its current instance. Closing the browser leaves tasks running. Exit using the tray before copying, moving or backing up the application. Exit cancels active operations at saved checkpoints and interrupts pending jobs; resume them manually after restarting. If an unresponsive child exceeds the shutdown deadline, the launcher stops its owned process tree; completed checkpoints remain in PostgreSQL.

## Data and portability

```text
Wenyi.exe
_internal/                  Python application and WebUI resources
runtime/                    PostgreSQL, Pango, fonts and tokenizer resources
migration/                  Docker export tools
data/                       Created at first startup
  config/config.yaml        Initial defaults (later WebUI changes are in PostgreSQL)
  config/credentials.env    Your provider credentials
  config/postgres-password  Local database credential; keep it with the database
  postgres/                 Application-owned PostgreSQL cluster
  files/                    Source documents, parser resources, exports, transfer staging
  logs/                     Launcher, database, API and worker logs
  runtime/                  Single-instance state and shutdown control files
```

Back up **all of `data/` after exiting**. Keep the database password file with the cluster. Move the entire application directory after shutdown; owned file references are stored relative to `data/files/`. Chinese characters and spaces are supported. Imported filenames that conflict with Windows restrictions are normalized with deterministic suffixes. Source EPUB/DOCX internal identities remain unchanged. An existing PostgreSQL cluster is never automatically upgraded to another major version.

The service listens on loopback. A fresh session token is passed in the initial browser URL fragment, removed from the URL by the frontend and used for HTTP/WebSocket authentication. Use the tray to reopen the authenticated UI after restarting. Logs do not need to be uploaded to use the app.

## Move selected Docker projects

The first migration format supports the official database schema at commit **`5f206257ddc05b113b0ec224518e31cb5d7b62ad`**. The helper checks the source table columns, and the importer validates format version, table layout, original source identity, member list, sizes and SHA-256 checksums. Unknown structures are rejected. The helper overlays only export code inside the existing API container; it does not upgrade the source app or database.

Copy the three files under `migration/` to the Docker host. In the source WebUI, pause selected projects and wait for exports to finish. Run from the deployment directory containing the official Compose file (with its normal `.env` and Compose project name):

```bash
bash /path/to/migration/docker-transfer.sh list
bash /path/to/migration/docker-transfer.sh export selected.wenyi.zip PROJECT_ID_1 PROJECT_ID_2
# For a different Compose filename:
bash /path/to/migration/docker-transfer.sh --compose /path/to/docker-compose.yml list
```

On a Windows Docker host, use PowerShell:

```powershell
& .\migration\docker-transfer.ps1 -Command list -ComposeFile .\deploy\docker-compose.yml
& .\migration\docker-transfer.ps1 -Command export -ComposeFile .\deploy\docker-compose.yml -Output .\selected.wenyi.zip -Project id1,id2
```

Only Docker Compose is required on the source host; the helper runs with Python already inside the API image. It requires the Compose service name `api`. A custom deployment can copy `wenyi-transfer.pyz` into its API container and invoke `python /tmp/wenyi-transfer.pyz list` or `export --project ID --output /tmp/selected.wenyi.zip`. Keep the application paused until the command finishes. Source files and projects are preserved. Existing destination archive files are not overwritten.

In the Windows WebUI choose **Import Docker projects**, upload the archive, inspect the project list, conflicts and missing credential variables, then select projects and import. You can refresh the result page after a browser interruption. Successful projects link to their new copies. An import itself never calls a model or replays an old queue message.

- Every imported project receives a new project ID; a conflicting name gets an import suffix. Chapter/segment indexes, review identities, glossary/conflicts, revisions, subtitle state, usage, task history and existing exports are retained. Database sequence IDs and structured links to those IDs are remapped.
- Identical provider/model/quota definitions are reused. Different definitions with the same ID receive suffixed IDs and imported project routes are updated. A differing provider sharing a credential variable gets a separate suffixed variable. Configure missing credentials before resuming.
- Imported projects use the source's effective configuration. Windows global defaults remain unchanged; historical job configuration snapshots retain their original definitions.
- Files are checked and staged before publication. Database changes commit transactionally per project. An interrupted import is recovered at application startup and can be retried with the original archive. Re-importing that same archive skips completed project copies. A separately generated export is a new package and creates new copies.
- A batch can have successfully imported projects before a later project fails. The results page identifies completed copies; refresh the preview and retry the remaining selection. A registry edit invalidates an old preview.
- Archives are limited to 8 GiB compressed, 16 GiB expanded and 100,000 files; select smaller project groups for larger libraries. Uploads remain under `data/files/.transfers/uploads/` so interrupted previews can be reopened. Remove obsolete upload archives only after exiting; keep the original migration package as a backup.
- BabelDOC projects still require the original bridge's saved session and a reachable service endpoint. The Wenyi archive contains Wenyi's state and local files, not an independent external service's database.

## Build and validate

Use the workflow in `.github/workflows/windows-webui.yml`, or Windows x64 with Python 3.12, uv, Node 22, pnpm 9, MSYS2 UCRT64 Pango and the Visual C++ x64 redistributable DLL directory available as build inputs:

```powershell
uv sync --locked --all-packages --group dev --group desktop-build --extra desktop --extra pdf-output --extra pdf-output-lite --python 3.12
pnpm install --frozen-lockfile
pnpm -C apps/web build
uv run --no-sync python scripts/build_windows_webui.py --msys-root C:\msys64 --crt-dir C:\path\to\Microsoft.VC143.CRT
pnpm -C apps/web exec playwright install chromium
uv run --no-sync python scripts/smoke_windows_webui.py --browser
```

Outputs are `dist/wenyi-webui-windows-x64.zip` and `dist/wenyi-webui-windows-x64.zip.sha256`. The bundle manifest records source revision, dependency versions and file checksums. Downloads are pinned in `scripts/windows-runtime.json`; the Pango DLL versions come from the MSYS2 build environment. See [third-party runtime information](windows-third-party.md).

CI exercises the extracted ZIP with a minimal application PATH, a local mock model, real PostgreSQL and real Chromium: startup/readiness, duplicate launch, port conflict, auth, SPA refresh, WebSocket, migration, translation, manual edits, all book export formats, both PDF engines, shutdown, restart and a moved Chinese/space directory. PostgreSQL integration tests cover duplicate dispatch, locks/recovery, progress reconnect, conflicts, corrupt archives and import rollback. Linux runs of `--source` are development checks only.

Before calling a Windows build accepted, additionally run the final ZIP in clean Windows 10 and 11 x64 as an ordinary user. Confirm the tray opens/exits correctly, closing the browser leaves long work running, a worker crash recovers saved progress, an unwritable directory gives a clear error, and the generated PDFs visibly contain Chinese/Japanese/Korean glyphs. Keep the CI logs, browser screenshot and `font-check-*.pdf` files with the acceptance record. CI on Windows Server is not a substitute for these desktop checks.

Developer runtime selection is `WENYI_RUNTIME_BACKEND=redis|postgres` (default `redis`). Docker retains Arq/Redis. The desktop launcher supplies PostgreSQL mode and owns one workflow worker plus an export worker with two slots. Durable queue records, progress snapshots/NOTIFY and migration journals live in PostgreSQL.
