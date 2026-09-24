# Paragraph quality scoring and selective revision

[简体中文](zh/quality.md)

The optional quality layer scores logical paragraphs using an existing generation
model such as DeepSeek or the native TypeSafe Jev judge. It selects a bounded set
of candidates and compares each candidate against its original.
Accepted replacements enter the Review shadow; the existing Autofix publisher is
the only component that can write formal translations. Whole-book Review still
runs. Quality scoring is **experimental, uncalibrated, and off by default**.

## Modes and scope

| Mode | Added scoring | Added candidate generation | Quality changes |
|---|---|---|---|
| `off` | None | None | None; existing behavior |
| `observe` | All applicable logical units | None | None from the quality layer |
| `optimize` | Baselines, candidates and comparisons | Bounded by unit and request limits | Accepted shadow replacements; publication follows Autofix |

`observe` does not disable existing Review, Fixer or Autofix. Use `--no-autofix`
when formal text must remain unchanged. `optimize --no-autofix` can save scored
candidates and shadow changes without publishing them.

The quality stage starts after the complete book is translated and its glossary
is stable. It covers inputs using book Review, including EPUB, DOCX and supported
PDF paths. SRT retains its independent lightweight workflow and has no quality
stage. Partial chapter translation cannot bypass the whole-book Review checks.

## Configure DeepSeek without Jev

Use an existing generation provider to return structured JSON judgments. For the
DeepSeek preset, the only credential needed is `DEEPSEEK_API_KEY` in the process
environment. This setup has no TypeSafe connection and requires no
`TYPESAFE_API_KEY`:

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

`review.quality_compare` inherits the score route when it has no explicit override.
If migrating from a Jev setup that explicitly routes comparison to Jev, remove
that override or change it to the same generation profile. Changing only the
score route leaves an explicit Jev comparison route active, so optimize would
still require its key.

To use another existing model profile, set `review.quality_score: {model: my_judge}`; keep that profile's normal
provider credentials and generation options. No separate adapter setting is
required. See the complete [DeepSeek example](../examples/quality-deepseek.yaml)
for request limits and experimental thresholds. That file uses a separate
`quality_json` profile on the preset's `default` connection: `deepseek-flash`,
`options.thinking: false`, and `max_output_tokens: 4096`. This controls judging
without changing translation or strong-tier settings. A short response limit can
still truncate a judgment; it is a request cap, not a guarantee that every
response fits. Match the example's languages to the saved book before reusing
existing state.

```bash
# Inspect the configured route without a model request.
uv run wenyi --config examples/quality-deepseek.yaml models explain --operation review.quality_score

# Score all applicable paragraphs and run Review without publishing changes.
uv run wenyi --config examples/quality-deepseek.yaml review book.epub --quality-mode observe --no-autofix

# Generate and compare candidates; keep accepted changes in the shadow only.
uv run wenyi --config examples/quality-deepseek.yaml review book.epub --quality-mode optimize --no-autofix
```

These two Review commands make real model requests; a Jev account is unnecessary,
but the configured generation provider can charge for them. `cheap` is a tier
name, not a measured price or quality guarantee. The current DeepSeek preset
maps all three tiers to `deepseek-flash` with thinking enabled, so selecting
`cheap` alone does not create a distinct lower-cost profile. The complete example
uses its separate judge profile to change those request settings. Inspect the
effective route before using a different profile.
No cost saving or equivalent translation quality has been established by live
comparative evaluation.

Generated JSON judgments and native Jev judgments have different semantics.
The generic adapter asks a generation model to produce rubric-level or option
weights and confidence, then validates their structure and ranges. Wenyi computes
the weighted score and supplies the legend from the trusted rubric. Its probabilities
and confidence are **self-reported values**, not measured model probabilities,
calibrated distributions or translation accuracy. The same rubric and acceptance
checks make results inspectable; they do not make the two judging methods
interchangeable. Keep model and judgment method in evaluation records, and
re-evaluate thresholds when changing either.

The adapter requests JSON through the existing provider path. DeepSeek's guide
requires JSON response mode, a prompt that explicitly requests JSON, and enough
output space; it also documents that empty content can occur. Such output is an
error, not a perfect score. See the official
[JSON mode guide](https://api-docs.deepseek.com/guides/json_mode/) and
[thinking mode guide](https://api-docs.deepseek.com/guides/thinking_mode/).

## Configure native Jev and run

Merge this fragment into the existing configuration, preserving generation models:

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

Set `TYPESAFE_API_KEY` in the process environment, alongside the credentials used
by existing generation models. No key belongs in YAML. A complete example with
all quality settings is [examples/quality.yaml](../examples/quality.yaml); its
default disables quality and publication. Match its languages to the saved book
before using it with existing state.

```bash
# Local route preview; no model request.
uv run wenyi models explain --operation review.quality_score

# Score and run existing Review without writing formal targets.
uv run wenyi review book.epub --quality-mode observe --no-autofix

# Generate, compare and review a shadow version without publishing.
uv run wenyi review book.epub --quality-mode optimize --no-autofix

# Explicitly permit the existing Autofix publication stage.
uv run wenyi review book.epub --quality-mode optimize --autofix

# Translate the complete book, then run quality and Review.
uv run wenyi translate book.epub --quality-mode optimize --review
```

`translate --no-review --quality-mode optimize` is a configuration conflict.
An explicit `review` command remains available when automatic Review is disabled.
Missing required routes, incompatible provider capabilities and missing reachable
credentials fail before model requests. Disabled quality does not require a Jev
key or connection. `observe` does not require the optimize-only generation routes
or their credentials.

| Operation | Capability | Default selection |
|---|---|---|
| `review.quality_score` | judgment | Explicit model or tier route required when enabled; native Jev or a generation model |
| `review.quality_compare` | judgment | Inherits quality score |
| `review.quality_diagnose` | generation | `strong` |
| `review.quality_retranslate` | generation | `translation.body` |
| `review.quality_revise` | generation | `polish.body` |
| `review.quality_verify` | generation | `strong` |

The `typesafe` adapter sends native `state` and a question map to
`POST https://api.typesafe.ai/v1/systemone`. It cannot generate text or serve as a
translation model. Do not configure `max_output_tokens`, temperature or chat
options for Jev. Moving aliases such as `jev-latest` are rejected; keep the pinned
version and re-evaluate thresholds when upgrading. Requests and responses follow
the [TypeSafe API reference](https://docs.typesafe.ai/api).

## Scores, evidence and acceptance

Each logical paragraph has six dimensions: adequacy, coverage, terminology,
reference, fluency and voice. The first four are critical dimensions. Each uses
its own five-level rubric, indexed 0–4. The displayed value is `100 * score / 4`;
it is not a probability of correctness. Complete level distributions and model
confidence are retained. No applicable terminology is `not_applicable`, not 100.
For the generation adapter, both the weights and confidence are self-reported;
for native Jev they are provider judgment signals. Neither represents measured
accuracy on the user's books. Severe-tail rules remain experimental when applied
to generated weights; numeric validation alone does not calibrate them.

Long paragraphs split through `Segment.cont` are scored together. Their original
text indices, segment indices, anchors and format metadata remain stable.
Candidates must return one target for every member; accepted units are published
together within a chapter transaction. `None` means incomplete translation;
intentional empty MinerU targets are recorded separately. Non-language units are
not assigned artificial perfect scores. Headings use heading-appropriate criteria.

Evidence includes source and target languages, complete source/target parts,
applicable terms, same-chapter neighbors and available analysis. The actual
context is fingerprinted. Source and glossary content are data, including any
embedded instructions to award high scores. Candidate comparison holds context
fixed; independent retranslation excludes the current target and prior scores.
Core source/target evidence is never silently truncated. Unusable oversized
evidence remains `context_overflow` or `needs_review`.

Wenyi checks both published Jev limits: 64k tokens per request and 32k for state
plus the longest question. Its input and structured-output reservations are
conservative estimates, not Jev's billing tokenizer. See
[TypeSafe model limits](https://docs.typesafe.ai/models). These are native Jev
limits, not universal limits for generation providers. Generic JSON judgments use
the selected generation provider's request path and normal output limits, plus
Wenyi's shared request/token reservations and quality evidence limit.

All baselines are scored before optimization units are ranked. Critical risk,
uncertainty and stable source order determine selection, not the first paragraphs
encountered. The selected set is persisted for resume. Low confidence first gets
bounded context expansion or independent verification and may remain for manual
review. A diagnosis can report `no_confirmed_issue` and leave the original intact.

At most two content candidates are generated for one unit during the initial
quality stage. Duplicates do not become new alternatives. Each candidate must
beat its original in blinded comparison, show the configured dimensional
improvement, and avoid critical regression. Default comparison swaps order and
requires consistent support. Ties, insufficient evidence, inconsistent order
preferences, incomplete scores and failed comparisons retain the incumbent.
Combined changes also undergo neighboring-context checks. Existing Review Fixer
and final Autofix issue repairs use the same gate in optimize mode.

## Experimental thresholds and budgets

These defaults are engineering starting points, not calibrated literary-quality
thresholds. Adjust them only with held-out evaluation, and keep the rubric and
fixed model version in the evaluation record.

| Setting under `pipeline.quality` | Default | Meaning |
|---|---:|---|
| `max_candidates_per_unit` | 2 | Initial content alternatives; allowed 0–2 |
| `max_units_to_optimize_per_run` | 100 | Selected logical units |
| `max_generation_requests_per_run` | 300 | Local generation-call reservations |
| `max_judge_requests_per_run` | 10000 | Local judgment-call reservations |
| `max_context_expansions` | 1 | Additional evidence windows; allowed 0–2 |
| `max_verifications_per_unit` | 1 | Verification allowance, 0–2; the current flow uses at most one |
| `context.preceding_units` / `following_units` | 2 / 1 | Same-chapter neighbors |
| `context.max_estimated_input_tokens` | 6000 | Evidence estimate before requesting a score |
| `thresholds.adequacy_min` / `coverage_min` / `terminology_min` / `reference_min` | 75 | Critical-dimension routing threshold |
| `thresholds.fluency_min` / `voice_min` | 70 / 65 | Expression routing thresholds |
| `thresholds.min_confidence_to_act` | 0.70 | Minimum confidence signal |
| `thresholds.min_pairwise_support` | 0.75 | Minimum winning-option support |
| `thresholds.min_dimension_improvement` | 5 | Minimum improvement on the 0–100 scale |
| `thresholds.severe_tail_probability` | 0.20 | Combined mass on levels 0 and 1 |
| `comparison.swap_order` | true | Compare both label orders |
| `comparison.keep_original_on_tie` | true | Required conservative tie policy |
| `audit.sample_rate` / `seed` | 0.05 / 42 | Reproducible high-score sampling |
| `on_error` | `continue_review` | Degrade to existing Review, or select `stop` |

Local quality limits supplement `llm.budget` and provider quotas. They never
raise the global limits. Global request limits count transport attempts,
including retries. Local quality counts reserve logical calls; bounded provider
retries can make more HTTP attempts than that local count. Set
`llm.budget.max_requests` as well when an HTTP-attempt ceiling is required.
Generation models need their normal output caps when global token limits are
used; this also applies when they produce JSON judgments. Native Jev reserves
structured output without sending a fake generation parameter. The quality
judge-call limit counts scoring/comparison calls through either method; the
quality generation-call limit counts diagnosis, candidates and verification.
Both still consume the same global request/token budget.
In-flight work and tokenizer estimation prevent an exact monetary ceiling.

Every successful response with valid usage is counted, including rejected
candidates and malformed answers. Jev `input_tokens` and `output_tokens` are
recorded separately; free output pricing does not erase output token counts.
This version does not calculate a configured currency estimate: missing pricing
is unknown, never a claim of zero cost. Operation/provider/model/tier totals are
alternative attribution views and must not be added together.

## Failure, resume and publication

The shared retry policy handles timeouts, HTTP 429, 529 and applicable 5xx,
including `Retry-After`, without nested SDK retries. Ordinary authentication and
protocol 4xx errors do not retry indefinitely. Invalid, incomplete, nonfinite or
duplicate-key answers cannot become passing scores.

With `on_error: continue_review`, unavailable quality is marked degraded and the
workflow returns to existing Review behavior. This disables new quality
optimization; existing Review and Autofix retain their configured permissions.
It is not a guarantee of read-only execution: use `--no-autofix` for that.
`on_error: stop` preserves a resumable interruption. Quality budget exhaustion
marks incomplete results and stops new quality work. Cancellation stops queued
requests and leaves completed artifacts available for resume.

Responses, generation results, comparison evidence and decisions live under the
existing Review artifact prefix. Local storage uses atomic artifacts; Web uses
PostgreSQL through the same storage interfaces and does not create independent
JSON/SQLite state in `DATA_DIR`. Persisted responses and the existing usage journal
are committed together. Resume reuses recorded responses inside that Review run.
Completed native judgments may also be reused from an earlier Review of the
same book when the complete request and inference identity match and the returned
model equals the pinned version. Generated judgments identify the requested
model and are reused within their Review run. Changing text, relevant context, language,
rubric or model invalidates that identity. Threshold changes can reuse raw
judgments while recomputing selection and acceptance; they do not reuse a prior
acceptance decision. Generated content candidates are reused within their run.

A network interruption after a provider generated a response can leave an
ambiguous request. A later retry may be billed again. Local idempotent accounting
does not provide provider exactly-once billing; no unsupported idempotency key is
sent. Keep the request IDs and run artifacts when investigating such a case.

The Review engine writes only its shadow. Quality patches have their own
provenance and no invented issue keys. A subsequent clean scan does not relabel
them as confirmed issue repairs. Autofix saves a recovery index before writing
targets, checks before/after hashes, preserves manual edits, and rejects a
continuation group together if a member conflicts. A partial publication remains
partial; unpublished candidates must not be presented as formal translations.

To stop using quality, set `pipeline.quality.mode: "off"`. Already published text
is not undone by changing this setting. Use recorded before/after history and the
existing editing/publication process to review a reversal while preserving later
manual edits. Do not delete state directories to roll back.

## Web reading and stale scores

Global Settings registers the desired generation provider/model or the optional
TypeSafe connection and Jev model. Project settings select the quality mode,
limits, thresholds and operation routes. To use existing generation models,
route quality scoring to their profile or a tier such as `cheap`; compare
inherits that choice. The existing
Review job carries that configuration; there is no independent quality worker.
Review and proofreading expose logical-unit scores, candidates and decisions,
with separate formal/shadow views. Lists are paginated and can filter score or
status rather than returning a whole book in one response.

Scores belong to a source, target and evidence snapshot. Manual edits and changed
neighbor context make affected stored scores stale on read. Refreshing a page
does not purchase a fresh score. A displayed 95 is a rubric score, not “95%
correct.” Rule reasons and generation-model explanations have different origins;
Jev itself does not author an invented natural-language Review issue.

## Offline smoke and independent evaluation

The engineering smoke test uses the supplied synthetic fixtures and no paid API:

```bash
uv run --no-sync python scripts/evaluate_translation_quality.py \
  --mock --input packages/core/tests/fixtures/quality/smoke.jsonl \
  --output /tmp/wenyi-quality-smoke
```

Each input JSONL row requires string fields `id`, `book_id`, `source` and `target`.
Optional fields include `split`, `genre`, `labels`, `evaluation_context` and a
separately supplied `budget_control` translation. One book cannot occur in
multiple splits. Labels, genre and version identities stay in the evaluation
key and never enter model requests. `evaluation_context` is for human blind
review; these isolated sample calls do not reconstruct a complete book context.

Outputs are `blind.jsonl` (shuffled anonymous versions and human-rating fields),
`key.jsonl` (version mappings and evaluation metadata), and `report.json` (sample
coverage, changes, usage, timing and explicitly unknown human outcomes/cost).
The tool does not manufacture a budget-matched control; supply that text from a
separately budgeted experiment. No empty human-result field is a quality success.

After authorizing sample text and a budget separately, real calls require an
explicit switch; merely loading a config cannot opt in:

```bash
uv run --no-sync python scripts/evaluate_translation_quality.py \
  --allow-paid-api --config examples/quality.yaml \
  --input authorized-samples.jsonl --output /tmp/wenyi-quality-evaluation \
  --max-judge-requests 50 --max-generation-requests 10
```

Compare baseline, quality optimization and the supplied budget control. Split
books between development and final evaluation. Human review should record
wins/ties/losses, major introduced errors, misses, false alarms, cost and coverage,
stratified by language direction, genre and error class. Include unchanged
high-score samples. Do not use the selecting judge's score, whether generated JSON or native Jev,
or the existing Reviewer as an unquestionable gold label. Report sample sizes and uncertainty; a sample
target such as 200–500 units is only an engineering starting point.

No real-model translation-quality gain, relative cost saving or equivalence
between generation-based judging and Jev is established by the offline tests or
mock smoke. TypeSafe reports stronger English performance than other languages;
Chinese literary work needs its own independent evaluation. See
[TypeSafe language guidance](https://docs.typesafe.ai/models#language-support).

### Summarize independent human ratings

After filling pairwise judgments, use the existing blinded labels, without exposing
`key.jsonl` to evaluators. Each ratings JSONL row contains `id`, `compared` (the two
baseline/quality labels), `preferred` (one label or `tie`), and optional nonnegative
integer `major_new_errors`, `misses`, and `false_alarms`. For example, when the two
versions are A and B: `{"id":"weather-1","compared":["A","B"],"preferred":"tie"}`.

```bash
uv run python scripts/evaluate_translation_quality.py --summarize-ratings \
  --input /tmp/human-ratings.jsonl --output /tmp/wenyi-quality-evaluation
```

This reads the existing `key.jsonl` and writes `human-summary.json` without model
calls. It reports win/tie/loss and error counts, coverage, and genre/language/error
category/replaced-versus-unchanged strata. These are descriptive counts; assess
book-level uncertainty and evaluator agreement before claiming effectiveness.
