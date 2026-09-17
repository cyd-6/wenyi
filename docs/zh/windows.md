# Windows 免安装 WebUI

[English](../windows.md)

Windows 版复用官方 React WebUI、FastAPI 与翻译内核，面向 Windows 10/11 x64。交付文件为 `wenyi-webui-windows-x64.zip`，由 **Windows 环境**中的 `Windows portable WebUI` 工作流生成。源码或 Linux 测试通过不代表 Windows 产物已经验收；发布前仍需完成本文末尾的 Windows 检查。

## 启动和配置

1. 从成功的构建任务下载 ZIP 与 `.zip.sha256`，用 `Get-FileHash .\wenyi-webui-windows-x64.zip -Algorithm SHA256` 对照校验值。
2. 将 **整个 ZIP** 解压到可写目录，例如 `D:\Tools\Wenyi`。保留 `Wenyi.exe`、`_internal/` 和 `runtime/` 的相对位置。不要直接在压缩包里运行，也不要放入受保护的安装目录。
3. 双击 `Wenyi.exe`。首次运行会初始化自带数据库，API 和两个 Worker 就绪后自动打开浏览器。默认地址为 `http://127.0.0.1:8080`，端口占用时会自动换一个空闲端口。
4. 在托盘选择 **Edit credentials (restart to apply)**，编辑 `data/config/credentials.env`，保存并退出、重启。环境变量名称与 WebUI 模型设置中的凭据变量一致；文件采用 UTF-8 dotenv 格式，例如 `MY_PROVIDER_API_KEY=你自己的密钥`。端点、模型和路由在 WebUI 中设置。密钥仍通过环境变量交给内核，不进入项目迁移包。

包内包含 Python、PostgreSQL 16 与 `pg_trgm`、Pango DLL、WeasyPrint、fpdf2、中日韩字体和分词器缓存。使用者无需安装 Docker、WSL、Python、Node.js、Redis 或数据库，也无需管理员权限。实际模型请求仍需连接你选择的供应商；MinerU 与 BabelDOC 沿用外部服务接入方式。

托盘提供打开界面、打开数据目录、查看日志、编辑凭据和 **Pause and exit**。重复双击会打开当前实例。关闭网页后任务继续运行；搬动或备份前请从托盘退出。退出会在已保存的检查点中止活动工作，并中断待执行任务，重启后手动续跑。子进程超过退出等待时间仍无响应时，启动器会结束自己管理的进程；已完成的检查点保留在数据库中。

## 数据保存与搬动

```text
Wenyi.exe
_internal/                  程序与 WebUI 资源
runtime/                    PostgreSQL、Pango、字体、分词器资源
migration/                  Docker 导出工具
data/                       首次运行创建
  config/config.yaml        初始默认配置，后续 WebUI 配置保存在数据库中
  config/credentials.env    供应商凭据
  config/postgres-password  本地数据库密码，必须与数据库一起保留
  postgres/                 此程序拥有的 PostgreSQL 数据目录
  files/                    原文件、解析资源、导出文件、迁移暂存
  logs/                     启动器、数据库、API 与 Worker 日志
  runtime/                  单实例信息与退出控制文件
```

**退出后备份整个 `data/`**，包括数据库密码文件。退出后可搬动整个应用目录：项目文件引用以 `data/files/` 为基准保存，支持中文与空格路径。迁移时，Windows 不允许的文件名使用确定的后缀转换；EPUB、DOCX 内部身份不会改写。程序不会自动把现有 PostgreSQL 数据升级到其他主版本。

服务只监听本机。启动令牌通过首次打开地址的 URL fragment 传给页面，页面读取后移除，再用于 HTTP 和 WebSocket 鉴权。重启后从托盘打开界面即可使用新会话。使用应用无需上传日志。

## 从 Docker 选择项目迁移

首版支持官方提交 **`5f206257ddc05b113b0ec224518e31cb5d7b62ad`** 对应的数据结构。工具检查来源表结构，导入时检查格式版本、字段、原文身份、文件列表、大小和 SHA-256。未知结构会被拒绝。来源工具只把导出代码临时放进现有 API 容器，不升级来源应用或数据库。

把 `migration/` 内三个文件复制到 Docker 主机。先在来源 WebUI 暂停要导出的项目，并等待导出任务结束。在官方 Compose 部署目录运行，使用原有 `.env` 和 Compose 项目名称：

```bash
bash /path/to/migration/docker-transfer.sh list
bash /path/to/migration/docker-transfer.sh export selected.wenyi.zip 项目ID1 项目ID2
# Compose 文件不在当前目录时：
bash /path/to/migration/docker-transfer.sh --compose /path/to/docker-compose.yml list
```

Windows Docker 主机也可以用 PowerShell：

```powershell
& .\migration\docker-transfer.ps1 -Command list -ComposeFile .\deploy\docker-compose.yml
& .\migration\docker-transfer.ps1 -Command export -ComposeFile .\deploy\docker-compose.yml -Output .\selected.wenyi.zip -Project id1,id2
```

主机只需 Docker Compose；Python 使用 API 容器中已有的版本。脚本使用 Compose 服务名 `api`。自定义部署可把 `wenyi-transfer.pyz` 复制进 API 容器，运行 `python /tmp/wenyi-transfer.pyz list` 或 `export --project ID --output /tmp/selected.wenyi.zip`。命令完成前保持项目暂停；源项目及文件会保留，已有输出包不会被覆盖。

在 Windows 页面选择 **导入 Docker 项目**，上传迁移包，查看项目列表、冲突结果和缺失的凭据变量，勾选项目后导入。浏览器中断后可刷新结果页，成功的项目可以直接打开。导入过程不会触发模型请求，也不会重放旧队列消息。

- 每个导入项目获得新 ID，同名时追加导入后缀。章节和段落索引、审校身份、术语与冲突、修订历史、字幕状态、用量、任务历史及已有导出会保留；数据库序列 ID 和关联引用会同步转换。
- 相同供应商、模型和配额定义会复用。同名但不同的定义使用后缀 ID，并更新项目路由。不同供应商连接若共用同一凭据变量，会分配新的带后缀变量，避免误用已有连接的密钥。续跑前补齐缺失凭据。
- 项目采用来源有效配置，Windows 全局默认值保持不变；历史任务配置快照保留原始定义。
- 文件先校验、暂存，再按项目通过事务发布。应用启动时恢复未完成导入，可用原包重试。同一迁移包重复提交会跳过已导入项目；重新导出的包属于新迁移，会创建新副本。
- 多项目导入可能在后面的项目失败前已完成部分项目。结果页会显示已完成副本；刷新预览后重试剩余项目。全局模型注册表有变化时需要刷新旧预览。
- 单包上限为压缩后 8 GiB、解压后 16 GiB、100,000 个文件，大型书库请分批。上传包保存在 `data/files/.transfers/uploads/`，便于中断后恢复预览。退出后可清理不再需要的上传缓存，原迁移包建议保留备份。
- BabelDOC 项目仍依赖原 bridge 保存的会话及可访问的服务地址。迁移包包含 Wenyi 状态和本地文件，不包含独立外部服务的数据库。

## 构建和验收

可运行 `.github/workflows/windows-webui.yml`，或在 Windows x64 准备 Python 3.12、uv、Node 22、pnpm 9、MSYS2 UCRT64 Pango 和 Visual C++ x64 可再分发 DLL 目录。这些是构建依赖，不是使用者的安装要求：

```powershell
uv sync --locked --all-packages --group dev --group desktop-build --extra desktop --extra pdf-output --extra pdf-output-lite --python 3.12
pnpm install --frozen-lockfile
pnpm -C apps/web build
uv run --no-sync python scripts/build_windows_webui.py --msys-root C:\msys64 --crt-dir C:\path\to\Microsoft.VC143.CRT
pnpm -C apps/web exec playwright install chromium
uv run --no-sync python scripts/smoke_windows_webui.py --browser
```

输出为 `dist/wenyi-webui-windows-x64.zip` 和 `.zip.sha256`。包内清单记录源码提交、依赖版本和文件校验值。下载版本固定在 `scripts/windows-runtime.json`，Pango DLL 来自构建时的 MSYS2 环境。第三方运行库说明见 [windows-third-party.md](../windows-third-party.md)。

CI 对实际解压的 ZIP 使用精简 PATH、本地模拟模型、真实 PostgreSQL 和 Chromium，验证就绪、重复启动、端口冲突、鉴权、SPA 刷新、WebSocket、迁移、翻译、人工修订、书籍多格式导出、两种 PDF 引擎、退出重启和中文空格目录搬动。数据库测试另外覆盖重复投递、锁与恢复、进度重连、模型冲突、坏包和导入回滚。Linux 的 `--source` 只用于开发验证。

Windows 产物正式验收前，还应在干净的 Windows 10 和 11 x64 普通用户环境运行最终 ZIP，确认托盘可用、关闭网页后长任务继续、Worker 崩溃后保留检查点、不可写目录提示明确，并目视检查 PDF 中日韩字体。保存 CI 日志、浏览器截图和 `font-check-*.pdf`。Windows Server CI 不能代替这些桌面系统检查。

开发环境使用 `WENYI_RUNTIME_BACKEND=redis|postgres`，默认 `redis`。Docker 继续用 Arq/Redis；Windows 启动器配置 PostgreSQL 后端，普通 Worker 并发 1，导出 Worker 并发 2。持久化队列、进度快照／通知及迁移日志均保存在 PostgreSQL。
