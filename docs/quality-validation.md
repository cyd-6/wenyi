# Quality implementation validation / 质量功能验证记录

This record describes executed checks, not a model quality evaluation.
本记录仅记录实际工程验证，不是模型翻译质量评测。

## Current baseline: main migration / 当前基线：迁移至 main

At the user's request, the feature changes were moved from `jev/ranking` to
`codex/quality-scoring`, created directly from the freshly fetched `origin/main` at
`d99d53be222b2528e6524caddc56648d86a0ac32`. HEAD equals this upstream commit;
the Windows WebUI tip `5edc742` and the previous merge `0b40e56` are not ancestors
of the new branch. Feature changes remain uncommitted and nothing was pushed.

按用户要求，功能修改现已迁移到直接从最新拉取的 `origin/main` 创建的
`codex/quality-scoring`。基线为 `d99d53b`，不包含 Windows WebUI 独有提交或之前的
合并提交。功能修改仍未提交，也未推送。

The migration preserved all 50 tracked feature deltas exactly and restored 39 new
feature files. Two locale files and the Web API bindings were applied to main's existing
content rather than copying their Windows-branch versions. The Windows-only sidebar
test adjustment was excluded. Regenerating the shared types from the main-based API
produced exactly the checked workspace types. The earlier branch and a scoped Git stash
were retained; patch/file backups are in `/tmp/wenyi-main-migration-_mn0cpjy`.

迁移完整保留 50 个已跟踪文件中的功能增删，并恢复 39 个新增文件；两份语言文件和前端
API 绑定以 main 内容为基础应用功能增量，排除了 Windows 专属侧栏测试修改。
共享 API 类型与重新生成结果一致。原分支与限定范围的 Git stash 保留，另有本地补丁和
文件备份；书籍、运行状态及依赖缓存未纳入迁移操作。

| Main-based validation / main 基线验证 | Result / 结果 |
|---|---|
| Locked offline workspace dependency sync, Python 3.12 and isolated 3.10 | Passed |
| Full Python 3.12.14 suite | **1081 passed, 113 skipped, 64 subtests passed; 10.34 s** |
| Full Python 3.10.21 suite | **1081 passed, 113 skipped, 64 subtests passed; 12.33 s** |
| Ruff check / format check and `git diff --check` | Passed; 288 files formatted |
| Node 22 / pnpm 9 typecheck and build | Passed; existing large-chunk warning |
| Full Web E2E | **90 passed; 14.6 s** |
| API schema generation comparison | Identical |
| Core, CLI, API sdist and wheel builds | Six artifacts built in `/tmp/wenyi-main-quality-dist` |
| Wheel resource check | **57 bundled resources verified** |

The 113 skips comprise 109 PostgreSQL tests without an isolated test database and
four optional PDF dependency/sample cases. Windows-specific tests are no longer part
of this baseline. No real model API or production database was used.

113 项跳过包括缺少隔离数据库的 109 项 PostgreSQL 测试，以及 4 项可选 PDF 依赖或
样本测试。Windows 专属测试不属于当前基线。此次没有调用真实模型或生产数据库。

Commands match the follow-up checks below, using `uv sync --locked --all-packages
--group dev --offline` for dependency sync and `/tmp/wenyi-main-quality-dist` for build
output. Logs are `/tmp/wenyi-main-quality-py312.log`, `/tmp/wenyi-main-quality-py310.log`
and `/tmp/wenyi-main-quality-web-{typecheck,build,e2e}.log`.

## Earlier follow-up: generated model judgments / 迁移前追加：通用模型评分

DeepSeek and other configured generation models can now evaluate quality through a strict
JSON bridge. Native Jev remains available. The follow-up preserves default-off behavior,
distinguishes generated self-reports from native distributions, and does not require a
TypeSafe credential when all reachable judgment routes use generation providers.

本次追加支持 DeepSeek 等生成模型的 JSON 评分与比较，并保留原生 Jev 路径。默认关闭；
概率和置信度明确区分原生分布信号与模型自报值。评分和比较均路由至生成提供商时无需
Jev 密钥。示例使用独立 DeepSeek profile，关闭 thinking 并设置 4096 输出上限；
通用适配的默认上限为 8192，用户可通过模型 profile 覆盖。

| Latest check / 最新检查 | Result / 结果 |
|---|---|
| Full Python 3.12.14 suite | **1085 passed, 129 skipped, 64 subtests passed; 9.42 s** |
| Full Python 3.10.21 suite in the existing isolated environment | **1085 passed, 129 skipped, 64 subtests passed; 10.94 s** |
| Ruff check and format check, Core / CLI / API / evaluation script | Passed; **307 files already formatted** |
| `git diff --check` | Passed |
| Core, CLI and API sdist + wheel | All six built in `/tmp/wenyi-generated-quality-dist` |
| Wheel resource check | **57 bundled resources verified**, including the generated judgment prompt |
| Node 22.23.3 / pnpm 9.15.9 Web typecheck and build | Passed; existing large-chunk warning |
| Full Web E2E | **93 passed; 13.9 s** |
| Mock evaluation smoke | Two authored samples completed without model calls |

The new regressions exercise an actual RoutedLLMClient and DeepSeek adapter with mocked SDK
responses, without TYPESAFE_API_KEY: observe, optimize, score/compare inheritance, shadow-only
behavior, stable source identity, once-only usage accumulation and response reuse on resume.
Additional cases cover strict JSON, missing usage, native/generated fallback, shared retries,
budget reservations, cancellation, bridge prompt fingerprints, provenance sanitization and
preventing generated judgments from using the pinned-native cross-run cache.

新增回归通过真实路由和 DeepSeek 适配器配合 mock SDK 响应，验证无 Jev 密钥的 observe、
optimize、评分/比较继承、影子修改、稳定身份、用量只归集一次及续跑复用。另覆盖非法 JSON、
缺少用量、原生/通用回退、共享重试、预算预留、取消、提示词指纹、来源脱敏与跨运行缓存隔离。

```bash
UV_CACHE_DIR=/tmp/wenyi-uv-cache uv run --no-sync pytest -q -rs
UV_CACHE_DIR=/tmp/wenyi-uv-cache UV_PROJECT_ENVIRONMENT=/tmp/wenyi-quality-py310 \
  uv run --no-sync --python 3.10 pytest -q -rs
uv run --no-sync ruff check packages/core packages/cli apps/api scripts/evaluate_translation_quality.py
uv run --no-sync ruff format --check packages/core packages/cli apps/api scripts/evaluate_translation_quality.py
uv build --offline --package wenyi-core --out-dir /tmp/wenyi-generated-quality-dist
uv build --offline --package wenyi-cli --out-dir /tmp/wenyi-generated-quality-dist
uv build --offline --package wenyi-api --out-dir /tmp/wenyi-generated-quality-dist
uv run --no-sync python scripts/check_wheel_resources.py /tmp/wenyi-generated-quality-dist
npm_config_cache=/tmp/wenyi-npm-cache npm exec --offline --yes --package=node@22 --package=pnpm@9 -- pnpm -C apps/web typecheck
npm_config_cache=/tmp/wenyi-npm-cache npm exec --offline --yes --package=node@22 --package=pnpm@9 -- pnpm -C apps/web build
npm_config_cache=/tmp/wenyi-npm-cache npm exec --offline --yes --package=node@22 --package=pnpm@9 -- pnpm -C apps/web test:e2e
uv run --no-sync python scripts/evaluate_translation_quality.py --mock \
  --input packages/core/tests/fixtures/quality/smoke.jsonl \
  --output /tmp/wenyi-generated-quality-smoke
```

Python output is retained in `/tmp/wenyi-generated-quality-py312.log` and
`/tmp/wenyi-generated-quality-py310.log`; Web output in
`/tmp/wenyi-web-generated-judge-{typecheck,build,e2e}.log`.

The 129 skips have the same causes listed in the initial record below. No real database,
paid model or human holdout evaluation was substituted. The generated judge is not claimed
to match Jev quality or provide measured cost savings. The implementation remains uncommitted.

129 项跳过原因与下方初次记录一致。本次没有真实数据库、付费模型或人工留出集验证，
不宣称通用评分与 Jev 等效，也不报告未经测量的成本节省。功能修改仍未提交。

## Initial implementation record / 初次实现记录

### Source baseline / 源码基线

- Initial HEAD: `5edc742e49043466f8c010c85a5cd9cd2e0ca7e0`; working tree initially clean.
- At the user's request, fetched `origin/main` at `d99d53be222b2528e6524caddc56648d86a0ac32`.
- Explicitly approved merge commit: `0b40e56` on `jev/ranking`. Existing Windows desktop and migration features were retained alongside upstream contents/sidebar changes.
- Quality implementation remains uncommitted. No push or pull request was created.
- Initially failing regressions covered missing typed judgment/unit modules, incorrect no-issue patch status, partial continuation publication, failed-response resumption, and publication comparison overwriting generation provenance. They passed after implementation/fixes.

### Executed checks / 实际检查

| Check | Result |
|---|---|
| `uv sync --locked --all-packages --group dev` | Passed |
| Full Python 3.12.14 suite: `uv run --no-sync pytest -q -rs` | **1027 passed, 129 skipped, 64 subtests passed; 8.96 s** |
| Full Python 3.10.21 suite in a separate temporary environment | **1027 passed, 129 skipped, 64 subtests passed; 11.75 s** |
| `ruff check packages/core packages/cli apps/api scripts/evaluate_translation_quality.py` | Passed |
| `ruff format --check` for the same paths | Passed; 305 files already formatted |
| `git diff --check` | Passed |
| Clean default CLI/Core installation with `uv sync --locked --no-dev --python 3.12` | Passed; `wenyi --version` and `wenyi review --help` succeeded |
| Core, CLI and API sdist + wheel builds | All six artifacts built successfully |
| `python scripts/check_wheel_resources.py /tmp/wenyi-quality-dist` | Passed; 56 bundled resources verified |
| `pnpm install --frozen-lockfile` | Passed |
| Web typecheck and build under Node 22.23.3 / pnpm 9.15.9 | Passed; Vite emitted its large-chunk size warning |
| Full `pnpm -C apps/web test:e2e` | **90 passed; 14.1 s** |
| Evaluation tool `--mock` on the two authored fixtures | Passed; produced blind tasks, private version key and machine report; no quality claim |
| Human rating aggregation tests | Passed; labels stayed out of model requests and blind materials, counts/strata were generated without model calls |

The full suites include architecture boundaries, Orchestrator contracts, Storage injection, legacy provider and FakeClient behavior, routing and usage, Review/checkpoint/Autofix, EPUB/DOCX identity preservation, CLI modes and API contracts. New integration regressions cover prelude checkpoint interruption, all three modification gates, unresolved issue retention, no-autofix with a pending publication index, edits before planning, grouped publication conflicts, final-score recovery after chapter commit, and interruption after durable usage preparation.

完整测试包含架构、装配、存储注入、旧 provider/FakeClient、路由与用量、Review/Autofix、格式身份、CLI 和 API 回归。新增用例覆盖质量 checkpoint 中断、三个修改入口门控、未解决问题保留、已有发布索引时的 no-autofix、规划前人工编辑、续片整体冲突、正式写入后评分恢复、用量 journal 已持久化但尚未返回时的中断。

### Commands / 命令

Commands ran from the repository root. `UV_CACHE_DIR=/tmp/wenyi-uv-cache` was used. Python 3.10 used `UV_PROJECT_ENVIRONMENT=/tmp/wenyi-quality-py310`; the clean install used `/tmp/wenyi-quality-cli-install`. Build output used a temporary directory to preserve existing user artifacts.

```bash
uv sync --locked --all-packages --group dev
uv run --no-sync pytest -q -rs

UV_PROJECT_ENVIRONMENT=/tmp/wenyi-quality-py310 uv sync --locked --all-packages --group dev --python 3.10
UV_PROJECT_ENVIRONMENT=/tmp/wenyi-quality-py310 uv run --no-sync --python 3.10 pytest -q -rs

uv run --no-sync ruff check packages/core packages/cli apps/api scripts/evaluate_translation_quality.py
uv run --no-sync ruff format --check packages/core packages/cli apps/api scripts/evaluate_translation_quality.py
git diff --check

uv build --offline --package wenyi-core --out-dir /tmp/wenyi-quality-dist
uv build --offline --package wenyi-cli --out-dir /tmp/wenyi-quality-dist
uv build --offline --package wenyi-api --out-dir /tmp/wenyi-quality-dist
uv run --no-sync python scripts/check_wheel_resources.py /tmp/wenyi-quality-dist

npm_config_cache=/tmp/wenyi-npm-cache npm exec --offline --yes --package=node@22 --package=pnpm@9 -- pnpm -C apps/web typecheck
npm_config_cache=/tmp/wenyi-npm-cache npm exec --offline --yes --package=node@22 --package=pnpm@9 -- pnpm -C apps/web build
npm_config_cache=/tmp/wenyi-npm-cache npm exec --offline --yes --package=node@22 --package=pnpm@9 -- pnpm -C apps/web test:e2e

uv run --no-sync python scripts/evaluate_translation_quality.py --mock \
  --input packages/core/tests/fixtures/quality/smoke.jsonl \
  --output /tmp/wenyi-quality-smoke
```

### Skipped and unverified / 跳过与未验证

Both final Python runs skipped the same 129 tests:

- 109 PostgreSQL storage/domain tests: `WENYI_TEST_DATABASE_URL` was not configured.
- 16 native runtime integration tests: the same isolated database prerequisite was absent.
- 3 real PDF-font tests: optional `fpdf` dependency was not installed.
- 1 optional BabelDOC sample test: the Weaver sample PDF was absent.

No production database or user book was substituted for missing fixtures. PostgreSQL/Redis deployment behavior is **not verified by these skipped tests**. The code uses the existing PostgreSQL artifact/transaction adapter and includes a shared response-receipt recovery contract, but actual isolated database execution remains outstanding. Docker was present but its daemon socket was inaccessible to the current user.

两次最终运行均跳过以上 129 项。没有用生产数据库或私有书籍补测。**不能将跳过项算作真实 PostgreSQL/Redis 后端验证通过**；隔离数据库执行仍需补充。

Initial sandbox runs encountered missing dependency/tokenizer downloads and an AnyIO TestClient local-communication hang. Public dependencies and the tokenizer were cached, and full tests were rerun with approved local-communication access. Earlier interrupted or environment-failed runs are superseded by the actual final results above; they were not counted as passes.

No paid TypeSafe/Jev or generation API was called. The protocol was checked against official documentation and mock HTTP responses. No independent human holdout evaluation was run. Thresholds remain uncalibrated, monetary cost remains unknown without rates, and configured generation-model metadata is not a claim about the provider-resolved fallback model. Local quality budgets count logical calls; provider retries are separately bounded. Durable local accounting does not promise supplier exactly-once billing after an ambiguous network interruption.

未调用真实付费模型、未运行人工留出集盲评。阈值未经校准；未配置费率时费用显示未知；生成模型来源是配置路由，不冒充回退后实际响应模型。质量局部预算统计逻辑调用，HTTP 重试由已有机制另行限制；本地幂等记账不承诺供应商 exactly-once 计费。

Existing book/state/output/cache data was retained. `.pnpm-store/` is a dependency cache created while preparing this task and is not part of the implementation source.

See [quality guide](quality.md), [中文指南](zh/quality.md), and [changed-file inventory](quality-changed-files.md).
