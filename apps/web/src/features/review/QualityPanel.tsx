import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useI18n, type MessageKey } from "@/i18n";
import { api, type QualityUnit } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";

const dimensions = [
  "adequacy",
  "coverage",
  "terminology",
  "reference",
  "fluency",
  "voice",
] as const;
const states: Record<string, MessageKey> = {
  changed: "quality.state.changed",
  pending: "quality.state.pending",
  needs_review: "quality.state.needs_review",
  scored: "quality.state.scored",
  complete: "quality.state.scored",
  ready: "quality.state.scored",
  failed: "quality.state.failed",
  not_applicable: "quality.state.not_applicable",
  accepted: "quality.state.accepted",
  accept: "quality.state.accepted",
  rejected: "quality.state.rejected",
  reject: "quality.state.rejected",
  keep: "quality.state.keep",
  retain: "quality.state.keep",
  published: "quality.state.published",
  not_published: "quality.state.not_published",
  partial: "quality.state.partial",
  revise: "quality.state.revise",
  retranslate: "quality.state.retranslate",
  verify: "quality.state.needs_review",
};

const reasonKeys: Record<string, MessageKey> = {
  adequacy_risk: "quality.reason.adequacy_risk",
  coverage_risk: "quality.reason.coverage_risk",
  terminology_risk: "quality.reason.terminology_risk",
  reference_risk: "quality.reason.reference_risk",
  fluency_risk: "quality.reason.fluency_risk",
  voice_risk: "quality.reason.voice_risk",
  incomplete_scores: "quality.reason.incomplete_scores",
  critical_regression: "quality.reason.critical_regression",
  uncertain_score: "quality.reason.uncertain_score",
  no_confirmed_improvement: "quality.reason.no_confirmed_improvement",
  incomplete_comparison: "quality.reason.incomplete_comparison",
  equivalent: "quality.reason.equivalent",
  insufficient_evidence: "quality.reason.insufficient_evidence",
  order_conflict: "quality.reason.order_conflict",
  original_preferred: "quality.reason.original_preferred",
  uncertain_comparison: "quality.reason.uncertain_comparison",
  insufficient_support: "quality.reason.insufficient_support",
  accepted_by_comparison: "quality.reason.accepted_by_comparison",
  context_overflow: "quality.reason.context_overflow",
  empty_target: "quality.reason.empty_target",
  non_language_unit: "quality.reason.non_language_unit",
  incomplete_translation: "quality.reason.incomplete_translation",
  quality_budget_exhausted: "quality.reason.quality_budget_exhausted",
  quality_request_failed: "quality.reason.quality_request_failed",
  quality_unavailable: "quality.reason.quality_unavailable",
  quality_degraded: "quality.reason.quality_degraded",
  combined_neighbor_regression: "quality.reason.combined_neighbor_regression",
  candidate_preference_conflict: "quality.reason.candidate_preference_conflict",
  alternative_not_selected: "quality.reason.alternative_not_selected",
  invalid_candidate: "quality.reason.invalid_candidate",
  no_confirmed_issue: "quality.reason.no_confirmed_issue",
};

export function QualityPanel({ pid, rid }: { pid: string; rid: string }) {
  const { t } = useI18n();
  const [view, setView] = useState<"formal" | "shadow">("formal");
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["quality", pid, rid, view, filter, offset],
    queryFn: () =>
      api.getQuality(pid, rid, {
        view,
        offset,
        limit: 20,
        ...(filter === "low" ? { max_score: 75 } : { status: filter }),
      }),
    refetchInterval: 5000,
  });
  return (
    <section
      className="space-y-4 rounded-lg border p-4"
      aria-label={t("quality.title")}
    >
      <h2 className="font-medium">{t("quality.title")}</h2>
      <p className="text-sm text-muted-foreground">
        {t("quality.uncalibrated")}
      </p>
      {!!query.data?.summary.status && (
        <p role="status" className="text-sm">
          {t(
            query.data.summary.status === "completed"
              ? "quality.completed"
              : query.data.summary.status === "degraded"
                ? "quality.degraded"
                : "quality.incomplete",
          )}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <Select
          aria-label={t("quality.scope")}
          value={view}
          onChange={(event) => {
            setView(event.target.value as typeof view);
            setOffset(0);
          }}
        >
          <option value="formal">{t("quality.formal")}</option>
          <option value="shadow">{t("quality.shadow")}</option>
        </Select>
        <Select
          aria-label={t("quality.filter")}
          value={filter}
          onChange={(event) => {
            setFilter(event.target.value);
            setOffset(0);
          }}
        >
          <option value="">{t("quality.all")}</option>
          <option value="low">{t("quality.low")}</option>
          {["needs_review", "rejected", "published"].map((value) => (
            <option key={value} value={value}>
              {t(states[value])}
            </option>
          ))}
          <option value="stale">{t("quality.stale")}</option>
        </Select>
      </div>
      <p className="text-xs text-muted-foreground">
        {t(view === "formal" ? "quality.formalHelp" : "quality.shadowHelp")}
      </p>
      <ErrorNotice error={query.error} />
      {query.data?.items.map((unit) => (
        <QualityUnitCard
          key={`${view}-${unit.unit_id}`}
          pid={pid}
          rid={rid}
          unit={unit as QualityUnit}
          view={view}
        />
      ))}
      {query.data && (
        <div className="flex items-center gap-3 text-sm">
          <Button
            variant="outline"
            disabled={!offset}
            onClick={() => setOffset(Math.max(0, offset - 20))}
          >
            {t("quality.previous")}
          </Button>
          <span>
            {t("quality.page", {
              count: query.data.items.length,
              total: query.data.total,
            })}
          </span>
          <Button
            variant="outline"
            disabled={offset + 20 >= query.data.total}
            onClick={() => setOffset(offset + 20)}
          >
            {t("quality.next")}
          </Button>
        </div>
      )}
    </section>
  );
}

export function QualityUnitCard({
  pid,
  rid,
  unit,
  view = "formal",
  compact = false,
}: {
  pid: string;
  rid: string;
  unit: QualityUnit;
  view?: "formal" | "shadow";
  compact?: boolean;
}) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const detail = useQuery({
    queryKey: ["quality-detail", pid, rid, unit.unit_id, view],
    queryFn: () => api.getQualityUnit(pid, rid, unit.unit_id, view),
    enabled: expanded,
  });
  const label = (value: string) => (states[value] ? t(states[value]) : value);
  const action =
    typeof unit.decision.action === "string" ? unit.decision.action : "";
  const judgeSource = unit.judge?.provenance?.source;
  const judgeModel =
    judgeSource === "generated"
      ? unit.judge?.model || unit.judge?.requested_model
      : unit.judge?.resolved_model ||
        unit.judge?.model ||
        unit.judge?.requested_model;
  const reasons = Array.isArray(unit.decision.reason_codes)
    ? unit.decision.reason_codes
    : unit.reason_codes;
  return (
    <article
      className={
        compact
          ? "mt-3 space-y-2 border-t pt-3"
          : "space-y-3 rounded-md border p-4"
      }
    >
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {!compact && (
          <Link
            className="underline"
            to={`/projects/${pid}/proofreading/${unit.chapter_index}?segment=${unit.members[0]?.segment_index}`}
          >
            {t("quality.location", {
              chapter: unit.chapter_index + 1,
              segments: unit.members
                .map((member) => member.segment_index)
                .join(", "),
            })}
          </Link>
        )}
        <Badge variant={unit.stale ? "destructive" : "secondary"}>
          {unit.stale ? t("quality.stale") : label(unit.status)}
        </Badge>
        <Badge
          variant={
            unit.publication_status === "published" ? "success" : "secondary"
          }
        >
          {label(unit.publication_status)}
        </Badge>
        {action && (
          <span>
            {t("quality.decision")}: {label(action)}
          </span>
        )}
        {unit.members.length > 1 && (
          <span>{t("quality.grouped", { count: unit.members.length })}</span>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {t(
          judgeSource === "generated"
            ? "quality.judgeGenerated"
            : judgeSource === "native"
              ? "quality.judgeNative"
              : "quality.judgeUnknown",
        )}
      </p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
        {dimensions.map((dimension) => (
          <div key={dimension} className="flex justify-between gap-2">
            <dt>{t(`quality.dimension.${dimension}`)}</dt>
            <dd>
              {unit.dimensions[dimension]?.normalized == null
                ? "—"
                : Number(unit.dimensions[dimension].normalized).toFixed(1)}
            </dd>
          </div>
        ))}
      </dl>
      {!!reasons.length && (
        <p className="text-xs text-muted-foreground">
          {t("quality.ruleReasons")}:{" "}
          {reasons
            .map((code) =>
              reasonKeys[String(code)]
                ? t(reasonKeys[String(code)])
                : String(code),
            )
            .join(" · ")}
        </p>
      )}
      <Button
        size="sm"
        variant="outline"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        {t("quality.details")}
      </Button>
      {expanded && (
        <div className="space-y-3 text-sm">
          <ErrorNotice error={detail.error} />
          <p className="text-xs text-muted-foreground">
            {t("quality.signalHelp")}
          </p>
          {judgeSource && (
            <p className="text-xs text-muted-foreground">
              {t(
                judgeSource === "generated"
                  ? "quality.generatedSignalHelp"
                  : "quality.nativeSignalHelp",
              )}
            </p>
          )}
          {judgeModel && (
            <p className="text-xs text-muted-foreground">
              {t(
                unit.judge?.provenance?.model_identity === "resolved" ||
                  unit.judge?.resolved_model
                  ? "quality.resolvedJudgeModel"
                  : "quality.requestedJudgeModel",
                { model: judgeModel },
              )}
            </p>
          )}
          {detail.data && (
            <>
              <div className="grid gap-3 md:grid-cols-2">
                <div>
                  <h4 className="font-medium">{t("quality.original")}</h4>
                  <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
                    {detail.data.original_target_parts.join("\n") ||
                      t("review.emptyTranslation")}
                  </p>
                </div>
                <div>
                  <h4 className="font-medium">{t("quality.current")}</h4>
                  <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
                    {detail.data.current_target_parts.join("\n") ||
                      t("review.emptyTranslation")}
                  </p>
                </div>
              </div>
              {detail.data.candidates.map((candidate, index) => {
                const parts = Array.isArray(candidate.target_parts)
                  ? candidate.target_parts.filter(
                      (part): part is string => typeof part === "string",
                    )
                  : [];
                return (
                  <section
                    key={String(candidate.candidate_id || index)}
                    className="space-y-2 border-t pt-3"
                  >
                    <h4 className="font-medium">
                      {t("quality.candidate", { index: index + 1 })}
                    </h4>
                    <p className="text-xs text-muted-foreground">
                      {t("quality.generationSource")}:{" "}
                      {String(candidate.operation || "—")}
                    </p>
                    {candidate.model_identity_kind === "configured_route" && (
                      <p className="text-xs text-muted-foreground">
                        {t("quality.configuredModel")}:{" "}
                        {[candidate.provider, candidate.model]
                          .filter(Boolean)
                          .map(String)
                          .join(" · ")}
                      </p>
                    )}
                    <TextDifference
                      before={detail.data!.original_target_parts.join("\n")}
                      after={parts.join("\n")}
                    />
                    {Array.isArray(candidate.comparisons) && (
                      <details className="text-xs">
                        <summary>{t("quality.comparisonSignals")}</summary>
                        <ol className="my-2 space-y-1">
                          {candidate.comparisons.map((value, index) => {
                            const comparison =
                              value && typeof value === "object"
                                ? (value as Record<string, unknown>)
                                : {};
                            const provenance =
                              comparison.provenance &&
                              typeof comparison.provenance === "object"
                                ? (comparison.provenance as Record<
                                    string,
                                    unknown
                                  >)
                                : {};
                            return (
                              <li key={index}>
                                {index + 1} ·{" "}
                                {t(
                                  provenance.source === "generated"
                                    ? "quality.judgeGenerated"
                                    : provenance.source === "native"
                                      ? "quality.judgeNative"
                                      : "quality.judgeUnknown",
                                )}
                              </li>
                            );
                          })}
                        </ol>
                        <pre className="max-h-60 overflow-auto whitespace-pre-wrap">
                          {JSON.stringify(candidate.comparisons, null, 2)}
                        </pre>
                      </details>
                    )}
                  </section>
                );
              })}
              <details className="text-xs">
                <summary>{t("quality.signals")}</summary>
                <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap">
                  {JSON.stringify(
                    {
                      dimensions: unit.dimensions,
                      decision: unit.decision,
                      reason_codes: unit.reason_codes,
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
            </>
          )}
        </div>
      )}
    </article>
  );
}

function TextDifference({ before, after }: { before: string; after: string }) {
  let prefix = 0;
  while (
    prefix < Math.min(before.length, after.length) &&
    before[prefix] === after[prefix]
  )
    prefix++;
  let suffix = 0;
  while (
    suffix < Math.min(before.length, after.length) - prefix &&
    before[before.length - suffix - 1] === after[after.length - suffix - 1]
  )
    suffix++;
  return (
    <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
      {after.slice(0, prefix)}
      <del className="bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-100">
        {before.slice(prefix, before.length - suffix)}
      </del>
      <ins className="bg-green-100 text-green-900 dark:bg-green-950 dark:text-green-100">
        {after.slice(prefix, after.length - suffix)}
      </ins>
      {suffix ? after.slice(-suffix) : ""}
    </p>
  );
}
