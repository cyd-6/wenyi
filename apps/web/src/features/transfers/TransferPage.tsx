import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, Upload } from "lucide-react";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type TransferPreview, type TransferResult } from "@/lib/api";
import { useI18n } from "@/i18n";

export default function TransferPage() {
  const { t } = useI18n();
  const [params, setParams] = useSearchParams();
  const uploadId = params.get("upload");
  const [preview, setPreview] = useState<TransferPreview | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [results, setResults] = useState<TransferResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const queryClient = useQueryClient();

  function accept(value: TransferPreview) {
    setPreview(value);
    setSelected(
      value.projects
        .filter((project) => !project.already_imported)
        .map((project) => project.id),
    );
  }

  useEffect(() => {
    if (!uploadId || preview?.upload_id === uploadId) return;
    let stopped = false;
    setBusy(true);
    Promise.all([api.getTransfer(uploadId), api.transferResults(uploadId)])
      .then(([value, records]) => {
        if (!stopped) {
          accept(value);
          setResults(records);
        }
      })
      .catch((err) => {
        if (!stopped) setError(String(err));
      })
      .finally(() => {
        if (!stopped) setBusy(false);
      });
    return () => {
      stopped = true;
    };
  }, [uploadId]);

  async function upload(file: File) {
    setParams({});
    setBusy(true);
    setError("");
    setPreview(null);
    setResults([]);
    try {
      const value = await api.previewTransfer(file);
      accept(value);
      setParams({ upload: value.upload_id });
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    if (!preview) return;
    setBusy(true);
    setError("");
    try {
      accept(await api.getTransfer(preview.upload_id));
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  async function importSelected() {
    if (!preview) return;
    setBusy(true);
    setError("");
    try {
      setResults(
        await api.importTransfer(
          preview.upload_id,
          selected,
          preview.registry_revision,
        ),
      );
      accept(await api.getTransfer(preview.upload_id));
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      await queryClient.invalidateQueries({ queryKey: ["globalConfig"] });
    } catch (err) {
      setError(String(err));
      try {
        setResults(await api.transferResults(preview.upload_id));
      } catch {
        /* Keep the actionable import error. */
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title={t("transfer.title")}
        subtitle={t("transfer.description")}
      />
      <PageContainer className="max-w-5xl space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>{t("transfer.archive")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm text-muted-foreground">
              {t("transfer.instructions")}
            </p>
            <label className="flex max-w-xl cursor-pointer items-center gap-3 rounded-md border border-dashed p-4">
              <Upload className="h-5 w-5 shrink-0" aria-hidden="true" />
              <span className="text-sm">{t("transfer.chooseFile")}</span>
              <input
                type="file"
                accept=".zip"
                aria-label={t("transfer.chooseFile")}
                disabled={busy}
                className="min-w-0 text-sm"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) void upload(file);
                  event.target.value = "";
                }}
              />
            </label>
            {busy && (
              <p role="status" className="flex items-center gap-2 text-sm">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t("transfer.working")}
              </p>
            )}
            {error && (
              <p role="alert" className="break-words text-sm text-destructive">
                {error}
              </p>
            )}
          </CardContent>
        </Card>
        {preview && (
          <Card>
            <CardHeader>
              <CardTitle>{t("transfer.selectProjects")}</CardTitle>
              <p className="text-sm text-muted-foreground">
                {t("transfer.sourceVersion", {
                  version: preview.source_version,
                })}
              </p>
            </CardHeader>
            <CardContent className="space-y-4">
              {preview.projects.map((project) => (
                <div
                  key={project.id}
                  className="space-y-2 rounded-md border p-4"
                >
                  <label className="flex items-center gap-3 font-medium">
                    <input
                      type="checkbox"
                      checked={selected.includes(project.id)}
                      disabled={busy || project.already_imported}
                      onChange={(event) =>
                        setSelected((old) =>
                          event.target.checked
                            ? [...old, project.id]
                            : old.filter((id) => id !== project.id),
                        )
                      }
                    />
                    {project.name}
                    {project.already_imported && (
                      <span className="text-xs font-normal text-muted-foreground">
                        {t("transfer.imported")}
                      </span>
                    )}
                  </label>
                  <p className="text-sm text-muted-foreground">
                    {t("transfer.counts", {
                      segments: project.counts.segments || 0,
                      terms: project.counts.glossary || 0,
                      files: project.file_count,
                    })}
                  </p>
                  {project.name_conflict && (
                    <p className="text-sm">{t("transfer.nameConflict")}</p>
                  )}
                  {project.conflicts.map((conflict, index) => (
                    <p key={index} className="break-all text-sm">
                      {conflict.source} → {conflict.target}
                    </p>
                  ))}
                  {project.missing_credentials.length > 0 && (
                    <p className="break-words text-sm text-amber-700 dark:text-amber-400">
                      {t("transfer.credentials")}{" "}
                      {project.missing_credentials.join(", ")}
                    </p>
                  )}
                  {project.warnings.map((warning) => (
                    <p
                      key={warning}
                      className="text-sm text-amber-700 dark:text-amber-400"
                    >
                      {warning}
                    </p>
                  ))}
                </div>
              ))}
              <p className="text-sm text-muted-foreground">
                {t("transfer.preserve")}
              </p>
              <div className="flex flex-wrap gap-3">
                <Button
                  disabled={busy || selected.length === 0}
                  onClick={() => void importSelected()}
                >
                  {t("transfer.importSelected", { count: selected.length })}
                </Button>
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() => void refresh()}
                >
                  {t("transfer.refresh")}
                </Button>
              </div>
            </CardContent>
          </Card>
        )}
        {results.length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle>{t("transfer.results")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {results.map((result) => (
                <div
                  key={result.source_id}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3"
                >
                  <span>
                    {preview?.projects.find(
                      (project) => project.id === result.source_id,
                    )?.name || result.source_id}
                  </span>
                  {result.status === "done" ? (
                    <Link
                      className="text-sm text-primary underline"
                      to={`/projects/${result.project_id}`}
                    >
                      {t("transfer.openProject")}
                    </Link>
                  ) : (
                    <span role="alert" className="text-sm text-destructive">
                      {result.error || result.status}
                    </span>
                  )}
                </div>
              ))}
            </CardContent>
          </Card>
        )}
      </PageContainer>
    </>
  );
}
