# Windows runtime components

Wenyi remains MIT licensed. The portable build distributes third-party runtime libraries and their notices separately. `bundle-manifest.json` records Python package versions and SHA-256 values of bundled files; `windows-runtime.json` pins downloaded PostgreSQL and font inputs.

| Component | Source and notices |
| --- | --- |
| PostgreSQL 16 / pg_trgm | The [PostgreSQL Windows page](https://www.postgresql.org/download/windows/) links to the EDB integration ZIP. `runtime/postgres/server_license.txt` and `commandlinetools_3rd_party_licenses.txt` are copied from that ZIP. The build preserves each executable’s original manifest and adds a process-local UTF-8 code page for Unicode installation paths; the downloaded input checksum and final file checksums are recorded separately. |
| Pango and its DLL dependencies | [MSYS2 UCRT64 Pango](https://packages.msys2.org/package/mingw-w64-ucrt-x86_64-pango), built from [MSYS2 package recipes](https://github.com/msys2/MINGW-packages). DLLs remain dynamically linked and replaceable under `runtime/pango/bin`; package notices are in `runtime/pango/licenses`. Source versions and recipes are available through MSYS2's package database. |
| Noto CJK fonts | [Noto CJK sources](https://github.com/notofonts/noto-cjk), SIL Open Font License in `runtime/fonts/OFL.txt`. The build instantiates the sans variable font at weight 400 for fpdf2 and includes the serif OTF for WeasyPrint. |
| Microsoft C++ runtime | App-local x64 redistributable DLLs from the build environment's Visual Studio redistributable directory, following Microsoft's [app-local deployment instructions](https://learn.microsoft.com/en-us/cpp/windows/walkthrough-deploying-a-visual-cpp-application-to-an-application-local-folder?view=msvc-170). Redistribution remains subject to the Visual Studio license. |
| Python packages | Installed from `uv.lock`; available distribution notices are copied to `licenses/python/`. PyInstaller's bootloader exception permits packaging the application. |

WeasyPrint locates the bundled DLLs using `WEASYPRINT_DLL_DIRECTORIES`, as documented in its [Windows installation guide](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows). Fontconfig points at the bundled fonts and keeps generated cache files under `data/runtime/`. BabelDOC is an external service; no BabelDOC Python package is included.

Unicode paths require Windows 10 version 1903 or later (or Windows 11), following Microsoft’s [process-local UTF-8 manifest support](https://learn.microsoft.com/en-us/windows/apps/design/globalizing/use-utf8-code-page). No machine-wide locale change is required. The supervisor uses a restricted token for PostgreSQL and assigns suspended child processes to its Job Object before allowing them to run.
