import { useQuery } from "@tanstack/react-query";
import { api, type CandidateComparison as Comparison } from "@/lib/api";
import { useI18n } from "@/i18n";
import { ErrorNotice } from "@/components/ui/data";

export function CandidateComparison({
  pid,
  chapterIndex,
  segmentIndex,
  currentTarget,
}: {
  pid: string;
  chapterIndex: number;
  segmentIndex: number;
  currentTarget: string | null | undefined;
}) {
  const { t } = useI18n();
  const query = useQuery({
    queryKey: ["segmentCandidates", pid, chapterIndex, segmentIndex],
    queryFn: () => api.segmentCandidates(pid, chapterIndex, segmentIndex),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data && data.status !== "published" && data.status !== "selected"
        ? 3000
        : false;
    },
  });
  if (query.isPending) return <p>{t("progress.loading")}</p>;
  if (query.error) return <ErrorNotice error={query.error} />;
  const data = query.data;
  if (!data)
    return (
      <p className="text-sm text-muted-foreground">{t("candidates.none")}</p>
    );
  const position = data.segment_indices.indexOf(segmentIndex);
  const selected = data.candidates.find(
    (candidate) => candidate.id === data.decision?.choice,
  );
  const changed =
    data.status === "published" &&
    selected?.targets &&
    selected.targets[position] !== currentTarget;
  const status: Record<Comparison["status"], string> = {
    generating: t("candidates.generating"),
    polishing: t("candidates.polishing"),
    judging: t("candidates.judging"),
    selected: t("candidates.selected"),
    published: t("candidates.published"),
  };
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        {t("candidates.batchHelp", {
          first: data.segment_indices[0] + 1,
          last: data.segment_indices[data.segment_indices.length - 1] + 1,
        })}
      </p>
      <p className="text-sm">{status[data.status]}</p>
      {data.decision && (
        <div className="rounded border p-3 text-sm space-y-1">
          <p>{t("candidates.winner", { id: data.decision.choice })}</p>
          {data.decision.model && (
            <p>{t("candidates.judgedBy", { model: data.decision.model })}</p>
          )}
          {data.decision.fallback_used && <p>{t("candidates.fallback")}</p>}
          {data.decision.confidence != null && (
            <p>
              {t("candidates.confidence", {
                value: Math.round(data.decision.confidence * 100),
              })}
            </p>
          )}
        </div>
      )}
      {changed && (
        <div className="rounded border p-3 text-sm space-y-2">
          <p>{t("candidates.changed")}</p>
          <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
            {currentTarget ?? t("proofreading.waitingForTranslation")}
          </p>
        </div>
      )}
      {data.candidates.map((candidate) => (
        <section
          key={candidate.id}
          className={`space-y-3 rounded-lg border p-4 ${candidate.id === data.decision?.choice ? "border-foreground/50 bg-muted/30" : ""}`}
        >
          <h3 className="font-medium text-sm">
            {t("candidates.candidate", { id: candidate.id })}
          </h3>
          {candidate.polish_status === "pending" && candidate.raw_targets && (
            <p className="text-xs text-muted-foreground">
              {t("candidates.polishing")}
            </p>
          )}
          {candidate.duplicate_of && (
            <p className="text-xs text-muted-foreground">
              {t("candidates.duplicate", { id: candidate.duplicate_of })}
            </p>
          )}
          <p className="whitespace-pre-wrap text-sm leading-relaxed [overflow-wrap:anywhere]">
            {candidate.targets?.[position] ??
              candidate.raw_targets?.[position] ??
              t("candidates.pending")}
          </p>
          {candidate.polish_status === "complete" && candidate.raw_targets && (
            <details>
              <summary className="cursor-pointer text-xs">
                {t("candidates.beforePolish")}
              </summary>
              <p className="mt-2 whitespace-pre-wrap text-sm [overflow-wrap:anywhere]">
                {candidate.raw_targets[position]}
              </p>
            </details>
          )}
          <details>
            <summary className="cursor-pointer text-xs">
              {t("candidates.wholeBatch")}
            </summary>
            <ol className="mt-3 space-y-4">
              {data.sources.map((source, i) => (
                <li key={data.segment_indices[i]} className="space-y-2 text-sm">
                  <p className="whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">
                    {source}
                  </p>
                  <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
                    {candidate.targets?.[i] ??
                      candidate.raw_targets?.[i] ??
                      t("candidates.pending")}
                  </p>
                  {candidate.polish_status === "complete" &&
                    candidate.raw_targets && (
                      <details>
                        <summary className="cursor-pointer text-xs">
                          {t("candidates.beforePolish")}
                        </summary>
                        <p className="mt-2 whitespace-pre-wrap [overflow-wrap:anywhere]">
                          {candidate.raw_targets[i]}
                        </p>
                      </details>
                    )}
                </li>
              ))}
            </ol>
          </details>
        </section>
      ))}
    </div>
  );
}
