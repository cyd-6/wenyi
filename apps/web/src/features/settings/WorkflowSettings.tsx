import { useI18n } from "@/i18n";
import { Disclosure } from "@/components/ui/disclosure";
import { Input, Label, Select } from "@/components/ui/form";

const section = (config: Record<string, unknown>, key: string) =>
  (config[key] || {}) as Record<string, unknown>;

export function WorkflowSettings({
  config,
  disabled,
  error,
  subtitles = false,
  pdf = false,
  models = {},
  onField,
}: {
  config: Record<string, unknown>;
  disabled: boolean;
  error?: unknown;
  subtitles?: boolean;
  pdf?: boolean;
  models?: Record<string, unknown>;
  onField: (group: string, key: string, value: unknown) => void;
}) {
  const { t: tr } = useI18n();
  const routes = section(section(config, "llm"), "routes");
  const judge = section(routes, "translation.judge");
  const judgeValue = judge.model || (judge.tier ? `tier:${judge.tier}` : "");
  const PIPELINE: [string, string][] = [
    ["best_of_three", tr("candidates.mode")],
    ["book_understanding", tr("settings.bookUnderstanding")],
    ["polish", tr("settings.polishing")],
    ["review", tr("common.wholeBookReview")],
    ["review_autofix", tr("settings.applyAutofixesToTheSavedTranslation")],
  ];

  return (
    <fieldset disabled={disabled} className="space-y-4 disabled:opacity-60">
      {!subtitles && (
        <div className="grid sm:grid-cols-2 gap-3">
          {PIPELINE.map(([key, label]) => (
            <label key={key} className="flex gap-2 items-center text-sm">
              <input
                type="checkbox"
                checked={Boolean(section(config, "pipeline")[key])}
                onChange={(e) => onField("pipeline", key, e.target.checked)}
              />
              {label}
            </label>
          ))}
        </div>
      )}
      {subtitles && (
        <p className="text-sm text-muted-foreground">
          {tr("settings.subtitlesUseASeparateWorkflowWithoutBook")}
        </p>
      )}
      {!subtitles && Boolean(section(config, "pipeline").best_of_three) && (
        <div className="space-y-2 rounded-lg border p-4">
          <Label htmlFor="candidate-judge">{tr("candidates.judge")}</Label>
          <Select
            id="candidate-judge"
            value={String(judgeValue)}
            onChange={(event) => {
              const routes = { ...section(section(config, "llm"), "routes") };
              const old = section(routes, "translation.judge");
              const selected = event.target.value;
              if (!selected) delete routes["translation.judge"];
              else
                routes["translation.judge"] = {
                  ...(selected.startsWith("tier:")
                    ? { tier: selected.slice(5) }
                    : { model: selected }),
                  fallbacks: old.fallbacks || [],
                };
              onField("llm", "routes", routes);
            }}
          >
            <option value="">{tr("candidates.chooseJudge")}</option>
            {Object.entries(models).map(([id, raw]) => {
              const model = raw as Record<string, unknown>;
              return (
                <option key={id} value={id}>
                  {id} · {String(model.model)}
                </option>
              );
            })}
            {["strong", "cheap", "fast"].map((tier) => (
              <option key={tier} value={`tier:${tier}`}>
                {tier}
              </option>
            ))}
          </Select>
          <p className="text-sm text-muted-foreground">
            {tr("candidates.modeHelp")}
          </p>
        </div>
      )}
      <Disclosure
        title={tr("settings.performance")}
        error={error}
        summary={tr("settings.performanceSummary", {
          tokens: Number(
            section(config, "segment").max_tokens_per_batch ?? 1800,
          ),
        })}
      >
        {!subtitles && (
          <label className="flex gap-2 items-center text-sm">
            <input
              type="checkbox"
              checked={Boolean(
                section(config, "pipeline").annotation_alignment,
              )}
              onChange={(e) =>
                onField("pipeline", "annotation_alignment", e.target.checked)
              }
            />
            {tr("settings.paragraphAnnotationAlignment")}
          </label>
        )}
        <div className="grid sm:grid-cols-2 gap-4">
          <div>
            <Label htmlFor="batch-tokens">
              {tr("settings.tokensPerBatch")}
            </Label>
            <Input
              id="batch-tokens"
              type="number"
              min={1}
              value={Number(
                section(config, "segment").max_tokens_per_batch ?? 1800,
              )}
              onChange={(e) =>
                onField(
                  "segment",
                  "max_tokens_per_batch",
                  Number(e.target.value),
                )
              }
              className="mt-2"
            />
          </div>
          <div>
            <Label htmlFor="segment-tokens">
              {tr("settings.tokensPerParagraph")}
            </Label>
            <Input
              id="segment-tokens"
              type="number"
              min={1}
              value={Number(
                section(config, "segment").max_tokens_per_segment ?? 1200,
              )}
              onChange={(e) =>
                onField(
                  "segment",
                  "max_tokens_per_segment",
                  Number(e.target.value),
                )
              }
              className="mt-2"
            />
          </div>
          {!subtitles && (
            <div>
              <Label htmlFor="review-concurrency">
                {tr("settings.reviewConcurrency")}
              </Label>
              <Input
                id="review-concurrency"
                type="number"
                min={1}
                value={Number(
                  section(config, "pipeline").review_concurrency ?? 4,
                )}
                onChange={(e) =>
                  onField(
                    "pipeline",
                    "review_concurrency",
                    Number(e.target.value),
                  )
                }
                className="mt-2"
              />
            </div>
          )}
          {pdf && (
            <div>
              <Label htmlFor="pdf-backend">{tr("settings.pdfParser")}</Label>
              <Select
                id="pdf-backend"
                value={String(
                  section(config, "pipeline").pdf_backend || "mineru",
                )}
                onChange={(e) =>
                  onField("pipeline", "pdf_backend", e.target.value)
                }
                className="mt-2"
              >
                {["mineru", "babeldoc"].map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </Select>
            </div>
          )}
        </div>
      </Disclosure>
    </fieldset>
  );
}
