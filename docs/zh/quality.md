# 段落质量评分与选择性修订

[English](../quality.md)

可选质量层可以使用 DeepSeek 等已有生成模型，或 TypeSafe Jev 原生判断，为逻辑段落
评分、有限生成候选，并将每个候选与原译比较。
通过的修改先进入 Review 影子译文；只有既有 Autofix 发布器能够更新正式译文。
整书 Review 仍完整执行。质量功能**默认关闭，处于实验阶段，阈值尚未校准**。

## 模式与适用范围

| 模式 | 新增评分 | 新增候选生成 | 质量层修改 |
|---|---|---|---|
| `off` | 无 | 无 | 无；保持既有行为 |
| `observe` | 所有适用逻辑段落 | 无 | 质量层不修改文字 |
| `optimize` | 原译、候选与比较 | 受段落数和请求预算约束 | 通过的候选进入影子；是否发布遵循 Autofix |

`observe` 不会关闭原有 Review、Fixer 或 Autofix。需要正式文本逐字不变时，必须同时使用
`--no-autofix`。`optimize --no-autofix` 可以保存候选、评分和影子修改，但不发布。

质量阶段在全书翻译完成、术语表稳定后运行，覆盖 EPUB、DOCX 和受支持 PDF 路径等使用
书籍 Review 的输入。SRT 保持独立轻量流程，不接入质量阶段。部分章节翻译不能绕过整书
Review 的完成检查。

## 使用 DeepSeek，无需 Jev

可以让已有生成提供商返回结构化 JSON 判断。使用 DeepSeek preset 时，只需在运行环境中
设置 `DEEPSEEK_API_KEY`。以下配置没有 TypeSafe 连接，也不需要 `TYPESAFE_API_KEY`：

```yaml
llm:
  preset: deepseek
  routes:
    review.quality_score: {tier: cheap}
pipeline:
  review_autofix: false
  quality:
    mode: "off"
```

`review.quality_compare` 没有显式覆盖时会继承评分路由。若从已有 Jev 配置迁移，而且
比较操作曾显式路由到 Jev，应删除该覆盖使其继承，或同步改为同一个生成模型 profile。
只改评分路由不会覆盖已有的 Jev 比较路由；optimize 此时仍会要求 Jev 密钥。

要使用其它已有模型 profile，可设置
`review.quality_score: {model: my_judge}`，沿用该 profile 的提供商凭证与生成参数，
无需另开适配器开关。请求额度和实验阈值见[完整 DeepSeek 示例](../../examples/quality-deepseek.yaml)。
该文件在 preset 的 `default` 连接上另建 `quality_json` profile，使用 `deepseek-flash`、
`options.thinking: false` 和 `max_output_tokens: 4096`，以便单独控制判断请求，不改变
翻译或 strong 档的参数。输出限额过短仍可能截断 JSON；它是请求上限，并不保证所有判断
响应都能容纳。复用已有书籍状态前，应将示例语言方向改为该书籍保存的方向。

```bash
# Inspect the configured route without a model request.
uv run wenyi --config examples/quality-deepseek.yaml models explain --operation review.quality_score

# Score all applicable paragraphs and run Review without publication.
uv run wenyi --config examples/quality-deepseek.yaml review book.epub --quality-mode observe --no-autofix

# Generate and compare shadow candidates without publication.
uv run wenyi --config examples/quality-deepseek.yaml review book.epub --quality-mode optimize --no-autofix
```

这两个 Review 命令会真实调用模型；不需要 Jev 账户，但所配置的生成提供商仍可能收费。
`cheap` 只是档位名称，不是已验证的低价或质量保证。当前 DeepSeek preset 的三个档位
都使用启用 thinking 的 `deepseek-flash`，因此单独选择 `cheap` 不会自动产生另一套更
便宜的参数。完整示例使用独立判断 profile 修改这些请求参数。切换 profile 前先检查实际
路由。目前没有真实对照评估证明节省了多少成本，或与 Jev 达到同等翻译质量。

通用 JSON 判断与 Jev 原生判断的语义不同。通用适配器要求生成模型输出等级或选项权重和
confidence，再校验结构与数值范围；Wenyi 计算加权分数，并从可信量表补齐等级说明。
其中概率和 confidence 均是**模型自报值**，不代表
测得的模型概率、校准分布或翻译正确率。共用量表和接受检查方便审查结果，并不说明两种
判断方式可以直接等价替换。评估时应记录模型和判断方式；更换任一项都需重新评估阈值。

适配器沿用已有提供商的 JSON 请求路径。DeepSeek 官方要求 JSON response mode、在提示
中明确要求 JSON，并留足输出空间，也说明可能返回空内容。这类响应应作为错误处理，不能
计为满分。详见官方 [JSON mode 说明](https://api-docs.deepseek.com/guides/json_mode/) 和
[thinking mode 说明](https://api-docs.deepseek.com/guides/thinking_mode/)。

## 配置原生 Jev 与运行

将以下片段合入现有配置，保留原有生成模型：

```yaml
llm:
  preset: deepseek
  providers:
    quality_judge:
      kind: typesafe
      api_key_env: TYPESAFE_API_KEY
      max_retries: 2
      max_concurrency: 2
  models:
    jev:
      provider: quality_judge
      model: jev-1.13.0
  routes:
    review.quality_score: {model: jev}
pipeline:
  review_autofix: false
  quality:
    mode: "off"
```

在运行环境设置 `TYPESAFE_API_KEY`，并保留原有生成模型使用的环境变量。密钥不写入 YAML。
完整配置示例见 [examples/quality.yaml](../../examples/quality.yaml)，其中质量和发布默认均
关闭。复用已有书籍状态之前，需将示例语言方向改为该书籍保存的方向。

```bash
# Preview routes locally without model requests.
uv run wenyi models explain --operation review.quality_score

# Score and review without changing formal targets.
uv run wenyi review book.epub --quality-mode observe --no-autofix

# Generate, compare and review shadow candidates without publication.
uv run wenyi review book.epub --quality-mode optimize --no-autofix

# Explicitly enable the existing Autofix publication stage.
uv run wenyi review book.epub --quality-mode optimize --autofix

# Run quality and Review after the complete translation.
uv run wenyi translate book.epub --quality-mode optimize --review
```

`translate --no-review --quality-mode optimize` 属于配置冲突。关闭自动 Review 后仍可使用
独立 `review` 命令。缺少必需路由、提供商能力不匹配、缺少当前流程可达凭证，均在模型
请求前报错。质量关闭时不要求 Jev 密钥，也不构建其连接；`observe` 不要求仅优化使用的
生成路由或其凭证。

| 操作 | 能力 | 默认选择 |
|---|---|---|
| `review.quality_score` | judgment | 启用时必须显式指定模型或档位；可用原生 Jev 或生成模型 |
| `review.quality_compare` | judgment | 继承质量评分 |
| `review.quality_diagnose` | generation | `strong` |
| `review.quality_retranslate` | generation | `translation.body` |
| `review.quality_revise` | generation | `polish.body` |
| `review.quality_verify` | generation | `strong` |

`typesafe` 适配器使用原生 `state` 与问题映射，请求
`POST https://api.typesafe.ai/v1/systemone`。它不能生成正文或用作翻译模型。不要为 Jev
配置 `max_output_tokens`、温度或聊天参数。`jev-latest` 等移动别名会被拒绝；应固定版本，
升级时重新评估阈值。请求和响应遵循 [TypeSafe API 文档](https://docs.typesafe.ai/api)。

## 评分、证据与接受规则

每个逻辑段落包含原意、覆盖、术语、指代、流畅度、文体六维；前四维为关键维度。各维使用
自己的五级量表，索引为 0–4，展示值为 `100 * score / 4`，不表示正确率。完整等级概率
分布和模型 confidence 均保留。没有适用术语时记为 `not_applicable`，不补成 100 分。
生成适配器的权重和 confidence 是模型自报值；原生 Jev 返回的是提供商的判断信号。
二者均不代表用户书籍上的实测准确率。严重等级尾部规则用于生成权重时同样属于实验策略；
数值通过校验并不意味着分布已经校准。

通过 `Segment.cont` 拆分的长段落合为一个评分单位，同时保留各成员原有 text index、
segment index、锚点和格式元数据。候选必须为每个成员返回一份目标文本；同组发布在章节
提交边界整体执行。`None` 表示未完成；MinerU 有意保存的空译文另行标记。非自然语言
单位不获得虚假的满分，标题使用适合标题的标准。

评价证据包含语言方向、完整原文与译文数组、适用术语、同章相邻段落及可用分析信息，
实际上下文参与指纹。原文和术语中的“给我满分”等指令仍作为待评估数据。候选比较使用
固定上下文；独立重译不携带当前译文和旧评分。核心原文和译文不会被截断后冒充完整评价；
无法容纳的输入保留 `context_overflow` 或 `needs_review` 状态。

Wenyi 同时检查 Jev 公布的两项上下文限制：每次请求 64k token，以及 state 加最长问题
32k token。输入和结构化输出预留使用保守估算，不是 Jev 精确计费 tokenizer。详见
[TypeSafe 模型限制](https://docs.typesafe.ai/models)。这些限制专用于原生 Jev，并非所有
生成提供商通用的上限。通用 JSON 判断沿用所选生成提供商的请求路径与正常输出限制，
并受 Wenyi 共享请求/token 预留和质量证据额度约束。

先完成全部原译评分，再按关键风险、不确定性策略和稳定原文顺序选择优化单位，不简单选取
最前面的段落。选中集合持久化以供续跑。低 confidence 先尝试有限上下文扩展或独立核查，
仍不明确则待人工复核。诊断可以返回 `no_confirmed_issue`，无需为配合低分制造错误。

初始质量阶段中，每个单位最多生成两个内容候选，重复文字不成为新的备选。每个候选必须
在盲比较中胜过原译，达到配置的分项改进，并且关键维度不得退步。默认交换 A/B 顺序且
要求支持一致；平局、证据不足、顺序冲突、评分不完整或比较失败均保留原有效版本。
组合修改还会检查相邻上下文。optimize 模式下，原有 Review Fixer 与 Autofix 最终问题
修复也进入同一门控。

## 实验阈值与预算

以下默认值是工程初值，不是经过文学翻译校准的阈值。调整时需使用独立留出集，并记录
量表和固定模型版本。

| `pipeline.quality` 下的设置 | 默认值 | 含义 |
|---|---:|---|
| `max_candidates_per_unit` | 2 | 初始内容候选数，范围 0–2 |
| `max_units_to_optimize_per_run` | 100 | 选中逻辑段落数 |
| `max_generation_requests_per_run` | 300 | 局部生成调用预留数 |
| `max_judge_requests_per_run` | 10000 | 局部判断调用预留数 |
| `max_context_expansions` | 1 | 追加证据窗口，范围 0–2 |
| `max_verifications_per_unit` | 1 | 核查额度，范围 0–2；当前流程最多执行一次 |
| `context.preceding_units` / `following_units` | 2 / 1 | 同章前后邻段 |
| `context.max_estimated_input_tokens` | 6000 | 请求评分前的证据估算上限 |
| `thresholds.adequacy_min` / `coverage_min` / `terminology_min` / `reference_min` | 75 | 关键维度分流阈值 |
| `thresholds.fluency_min` / `voice_min` | 70 / 65 | 表达分流阈值 |
| `thresholds.min_confidence_to_act` | 0.70 | 最低 confidence 信号 |
| `thresholds.min_pairwise_support` | 0.75 | 胜出选项最低支持度 |
| `thresholds.min_dimension_improvement` | 5 | 0–100 分项量表上的最低改进 |
| `thresholds.severe_tail_probability` | 0.20 | 等级 0、1 的合计概率质量 |
| `comparison.swap_order` | true | 交换标签顺序比较 |
| `comparison.keep_original_on_tie` | true | 必须保持的平局保留策略 |
| `audit.sample_rate` / `seed` | 0.05 / 42 | 可复现高分抽检 |
| `on_error` | `continue_review` | 降级回原有 Review；也可选择 `stop` |

质量局部额度叠加于 `llm.budget` 和提供商配额，不会放宽全局限制。全局请求预算统计
传输尝试，包含重试；局部质量额度预留逻辑调用，提供商的有限重试可能使 HTTP 尝试数
高于局部计数。需要限制 HTTP 尝试总数时同时设置 `llm.budget.max_requests`。
使用全局 token 限额时，生成模型仍需正常输出上限，生成 JSON 判断时也如此；原生 Jev
根据结构化结果预留，不发送虚构生成参数。无论哪种判断方式，评分和比较都计入质量
judge 调用额度；诊断、候选生成、独立核查计入质量 generation 调用额度。两类调用同时
消耗同一全局请求/token 预算。有限在途工作和 tokenizer 估算误差意味着不能精确保证实际金额封顶。

有合法 usage 的成功响应均进入用量统计，包括最终被拒绝的候选和业务答案错误的响应。
Jev 的 `input_tokens`、`output_tokens` 分别映射；输出免费不代表输出 token 为零。
本版不计算可配置币种费用，缺少价格时显示未知，不宣称零费用。操作、提供商、模型、档位
是同一账本的不同归属视图，不能相加得到总量。

## 故障、续跑与发布

共享重试处理超时、HTTP 429、529 及适用 5xx，并遵循 `Retry-After`，没有 SDK 嵌套重试。
普通鉴权和协议 4xx 不无限重试。错误、不完整、非有限数或重复 JSON key 响应不能成为
通过评分。

`on_error: continue_review` 将不可用的质量阶段标记为 degraded，回到原有 Review 行为。
这会停止新增质量优化；原有 Review 和 Autofix 保留配置中的权限。该设置不保证只读，
需要只读仍应使用 `--no-autofix`。`on_error: stop` 保存可恢复中断。质量预算耗尽会标记
incomplete 并停止新增质量请求；取消会停止排队请求并保留已完成产物。

响应、生成结果、比较证据与决定保存在现有 Review artifact 前缀内。本地使用原子产物，
Web 通过同一存储接口保存在 PostgreSQL，不在 `DATA_DIR` 建立独立 JSON/SQLite 状态。
已保存响应与既有 usage journal 一起提交。续跑复用同一 Review 运行内的已存响应。
同一书籍先前 Review 中的完整原生判断，也可在完整请求与推理身份一致、返回模型等于
固定版本时复用。生成式判断记录的是所请求的模型，在同一 Review 运行内复用。文本、相关上下文、语言、量表或模型变化使该身份失效。阈值变化可复用
原始判断，但重新计算选择和接受，不复用旧接受决定。生成内容候选在其运行内复用。

若供应商已经生成响应但网络中断，请求可能处于 ambiguous 状态，再次尝试可能再次计费。
本地幂等归集不等于供应商 exactly-once 计费，也不会发送未经官方支持确认的幂等键。
排查此类情况时应保留请求 ID 和运行产物。

Review 引擎只写影子。质量补丁使用独立来源，不伪造 issue key；后续无问题盲审也不会将
其改标为“已证实修复”。Autofix 更新正式 target 前先写恢复索引，检查 before/after hash，
保留人工修改；续片任一成员冲突时整体拒绝该组。部分发布仍显示为 partial，未发布候选
不能冒充正式译文。

停用时设置 `pipeline.quality.mode: "off"`。这不会撤销已经发布的文字；需要撤回时沿
既有 before/after 历史及编辑、发布流程审查处理，保留后续人工编辑。不要删除状态目录
来回退。

## Web 阅读与 stale 评分

全局设置注册所需生成提供商/模型，或可选的 TypeSafe 连接和 Jev 模型；项目设置选择
模式、额度、阈值及操作路由。使用已有生成模型时，将质量评分路由指向该 profile 或
`cheap` 等档位，比较自动继承。配置由既有 Review job 携带，不新增独立质量 worker。审校和校对可查看逻辑段落评分、
候选和决定，并切换正式/影子视图。列表分页且可按评分或状态过滤，不一次返回整本书。

评分属于特定原文、译文和证据快照。人工编辑或相邻上下文变化会在读取时使相关评分 stale；
刷新页面不会购买新评分。95 分不能理解为“95% 正确”。规则原因与生成模型解释具有不同
来源；Jev 本身不会生成虚构的自然语言 Review 问题。

## 离线 smoke 与独立评估

工程 smoke 使用仓库合成样本，不调用付费 API：

```bash
uv run --no-sync python scripts/evaluate_translation_quality.py \
  --mock --input packages/core/tests/fixtures/quality/smoke.jsonl \
  --output /tmp/wenyi-quality-smoke
```

JSONL 每行必须含字符串字段 `id`、`book_id`、`source`、`target`。可选字段包括 `split`、
`genre`、`labels`、`evaluation_context`，以及另行提供的对照译文 `budget_control`。
同一本书不能出现在多个拆分中。标签、文体和版本身份只进入评估 key，不发送给模型。
`evaluation_context` 供人工盲评参考；这些独立样本请求不还原完整书籍上下文。

输出包括 `blind.jsonl`（随机匿名版本和人工评分字段）、`key.jsonl`（版本映射与评估
元数据）、`report.json`（覆盖、修改数、用量、耗时，以及明确未知的人工结论与金额）。
工具不自动制造同预算对照；需从另一个预算匹配的实验提供文本。空的人工结果字段不代表
质量验证通过。

另行授权样本文本和预算后，真实调用必须显式开启，仅加载配置不会自动授权：

```bash
uv run --no-sync python scripts/evaluate_translation_quality.py \
  --allow-paid-api --config examples/quality.yaml \
  --input authorized-samples.jsonl --output /tmp/wenyi-quality-evaluation \
  --max-judge-requests 50 --max-generation-requests 10
```

比较基线、质量优化和提供的同预算对照，按书籍划分开发与最终评估集。人工记录胜/平/负、
重大新增错误、漏检、误报、成本和覆盖，并按语言方向、文体、错误类型分层。还应抽查没有
被修改的高分段落，不能将用于选择候选的通用 JSON 评分、原生 Jev 评分或原有 Reviewer
当作绝对可靠 gold label。
报告样本数及不确定性；200–500 个逻辑段落只可作为工程试验起点。

离线测试和 mock smoke 不证明真实翻译质量提升、相对成本节省，也不证明生成模型判断与
Jev 等价。TypeSafe 提示英语表现优于其它语言；
中文文学任务需要独立实测。见 [TypeSafe 语言说明](https://docs.typesafe.ai/models#language-support)。

### 汇总独立人工盲评

评审者使用盲化版本标签，不应看到 `key.jsonl`。人工结果 JSONL 每行包含
`id`、`compared`（基线与质量版本的两个盲化标签）、`preferred`（其中一个标签
或 `tie`），可另填非负整数 `major_new_errors`、`misses`、`false_alarms`。
例如两个版本为 A、B 时：
`{"id":"weather-1","compared":["A","B"],"preferred":"tie"}`。

```bash
uv run python scripts/evaluate_translation_quality.py --summarize-ratings \
  --input /tmp/human-ratings.jsonl --output /tmp/wenyi-quality-evaluation
```

此命令读取已有 `key.jsonl`，生成 `human-summary.json`，不调用模型。结果包含
胜／平／负、错误数量、覆盖率，以及文体、语言方向、错误类别、替换／未替换分层。
这些是描述性统计；判断质量有效性仍需报告按书籍划分的不确定性与评审一致性。
