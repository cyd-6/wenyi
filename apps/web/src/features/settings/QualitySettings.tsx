import { useI18n, type MessageKey } from "@/i18n";
import { Disclosure } from "@/components/ui/disclosure";
import { Input, Label, Select } from "@/components/ui/form";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;
const dimensions = [
  "adequacy",
  "coverage",
  "terminology",
  "reference",
  "fluency",
  "voice",
] as const;

export function QualitySettings({
  value,
  onChange,
}: {
  value: unknown;
  onChange: (value: Document) => void;
}) {
  const { t } = useI18n();
  const quality = object(value);
  const thresholds = object(quality.thresholds);
  const mode = String(quality.mode || "off");
  const update = (key: string, value: unknown) =>
    onChange({ ...quality, [key]: value });
  const budgets: [string, MessageKey, number, number?][] = [
    ["max_candidates_per_unit", "quality.maxCandidates", 2, 2],
    ["max_units_to_optimize_per_run", "quality.maxUnits", 100],
    ["max_generation_requests_per_run", "quality.generationBudget", 300],
    ["max_judge_requests_per_run", "quality.judgeBudget", 10000],
  ];
  return (
    <Disclosure
      title={t("quality.title")}
      summary={t(`quality.mode.${mode}` as MessageKey)}
    >
      <p className="text-sm text-muted-foreground">
        {t("quality.experimental")}
      </p>
      <div>
        <Label htmlFor="quality-mode">{t("quality.mode")}</Label>
        <Select
          id="quality-mode"
          value={mode}
          onChange={(event) => update("mode", event.target.value)}
        >
          {["off", "observe", "optimize"].map((value) => (
            <option key={value} value={value}>
              {t(`quality.mode.${value}` as MessageKey)}
            </option>
          ))}
        </Select>
      </div>
      <p className="text-sm text-muted-foreground">
        {t("quality.publicationHelp")}
      </p>
      <p className="text-sm text-muted-foreground">{t("quality.modelHelp")}</p>
      {mode !== "off" && (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            {budgets.map(([key, label, defaultValue, max]) => (
              <div key={key}>
                <Label htmlFor={`quality-${key}`}>{t(label)}</Label>
                <Input
                  id={`quality-${key}`}
                  type="number"
                  min={0}
                  max={max}
                  value={Number(quality[key] ?? defaultValue)}
                  onChange={(event) => update(key, Number(event.target.value))}
                />
              </div>
            ))}
          </div>
          <Disclosure title={t("quality.thresholds")}>
            <p className="text-sm text-muted-foreground">
              {t("quality.uncalibrated")}
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              {dimensions.map((dimension, i) => (
                <div key={dimension}>
                  <Label htmlFor={`quality-${dimension}`}>
                    {t(`quality.dimension.${dimension}`)}
                  </Label>
                  <Input
                    id={`quality-${dimension}`}
                    type="number"
                    min={0}
                    max={100}
                    value={Number(
                      thresholds[`${dimension}_min`] ??
                        (i < 4 ? 75 : i === 4 ? 70 : 65),
                    )}
                    onChange={(event) =>
                      update("thresholds", {
                        ...thresholds,
                        [`${dimension}_min`]: Number(event.target.value),
                      })
                    }
                  />
                </div>
              ))}
              {(["min_confidence_to_act", "min_pairwise_support"] as const).map(
                (key, i) => (
                  <div key={key}>
                    <Label htmlFor={`quality-${key}`}>
                      {t(
                        i === 0
                          ? "quality.confidenceThreshold"
                          : "quality.supportThreshold",
                      )}
                    </Label>
                    <Input
                      id={`quality-${key}`}
                      type="number"
                      min={0}
                      max={1}
                      step={0.01}
                      value={Number(thresholds[key] ?? (i === 0 ? 0.7 : 0.75))}
                      onChange={(event) =>
                        update("thresholds", {
                          ...thresholds,
                          [key]: Number(event.target.value),
                        })
                      }
                    />
                  </div>
                ),
              )}
            </div>
          </Disclosure>
        </>
      )}
    </Disclosure>
  );
}
