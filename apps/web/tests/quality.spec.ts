import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

const rid = "review-quality";
const run = {
  id: rid,
  review_id: rid,
  status: "completed",
  created_at: null,
  issues: [],
  changes: [],
  autofix: {},
  summary: {},
  result: {},
  items: [],
  quality: { mode: "optimize", status: "completed" },
};
const unit = {
  unit_id: "ch0:text0:seg0",
  chapter_index: 0,
  members: [
    { text_index: 0, segment_index: 0 },
    { text_index: 1, segment_index: 7 },
  ],
  kind: "text",
  text_scope: "formal",
  status: "scored",
  dimensions: {
    adequacy: {
      score: 2,
      normalized: 50,
      confidence: 0.8,
      status: "scored",
      probabilities: { "0": 0, "1": 0, "2": 1, "3": 0, "4": 0 },
    },
  },
  decision: { action: "rejected" },
  reason_codes: [],
  publication_status: "not_published",
  stale: true,
  calibration_status: "uncalibrated",
  minimum_score: 50,
};

async function qualityApi(
  page: Parameters<typeof fakeApi>[0],
  judge: Record<string, unknown> = {},
) {
  await fakeApi(page, {
    [`/projects/${pid}/review/runs`]: [run],
    [`/projects/${pid}/review/runs/${rid}`]: run,
  });
  await page.route(
    `**/api/projects/${pid}/review/runs/${rid}/quality**`,
    (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("/quality"))
        return route.fulfill({
          json: {
            items: [
              {
                ...unit,
                judge,
                text_scope: url.searchParams.get("view") || "formal",
              },
            ],
            total: 1,
            limit: 20,
            offset: 0,
            text_scope: url.searchParams.get("view") || "formal",
            summary: {},
          },
        });
      return route.fulfill({
        json: {
          ...unit,
          judge,
          source_parts: ["Source", "continuation"],
          current_target_parts: ["Current translation", "continuation"],
          original_target_parts: ["Original translation", "continuation"],
          candidates: [
            {
              candidate_id: "candidate-a",
              target_parts: ["Proposed translation", "continuation"],
              operation: "review.quality_retranslate",
              provider: "default",
              model: "test-model",
            },
          ],
        },
      });
    },
  );
}

test("quality review distinguishes saved and shadow scores, stale status and candidate differences", async ({
  page,
}) => {
  await qualityApi(page);
  await page.goto(`/projects/${pid}/review`);
  const panel = page.getByRole("region", {
    name: "Paragraph quality (experimental)",
  });
  await expect(
    panel.getByRole("article").getByText("Stale score", { exact: true }),
  ).toBeVisible();
  await expect(panel.getByText("One unit across 2 fragments")).toBeVisible();
  await expect(panel.getByText("Not published", { exact: true })).toBeVisible();
  await expect(panel.getByText("50.0", { exact: true })).toBeVisible();
  await panel.getByLabel("Filter quality units").selectOption("low");
  await panel.getByLabel("Scored translation").selectOption("shadow");
  await expect(
    panel.getByText(
      "Shadow scores describe proposed text. Accepted candidates are not necessarily published.",
    ),
  ).toBeVisible();
  await panel
    .getByRole("button", { name: "Scores and candidate differences" })
    .click();
  await expect(panel.getByText("Candidate 1", { exact: true })).toBeVisible();
  await expect(panel.locator("ins")).toContainText("Proposed");
  await expect(panel.locator("del")).toContainText("Original");
  await expect(
    panel.getByRole("link", { name: "Chapter 1 · segment 0, 7" }),
  ).toHaveAttribute("href", `/projects/${pid}/proofreading/0?segment=0`);
});

test("quality settings expose modes, budgets and uncalibrated thresholds", async ({
  page,
}) => {
  await fakeApi(page);
  await page.goto(`/projects/${pid}/settings`);
  await page
    .locator("summary")
    .filter({ hasText: "Paragraph quality (experimental)" })
    .click();
  await page
    .getByLabel("Quality mode", { exact: true })
    .selectOption("optimize");
  await expect(page.getByLabel("Generation requests per run")).toHaveValue(
    "300",
  );
  await page.getByText("Experimental thresholds", { exact: true }).click();
  await expect(
    page.getByText(/Thresholds are uncalibrated experimental values/),
  ).toBeVisible();
  await expect(page.getByLabel("Meaning", { exact: true })).toHaveValue("75");
});

test("review submission keeps quality optimization and formal publication separate", async ({
  page,
}) => {
  await fakeApi(page);
  let body: unknown;
  await page.route(`**/api/projects/${pid}/review/run`, (route) => {
    body = route.request().postDataJSON();
    return route.fulfill({
      json: { job_id: "quality-job", project_id: pid, kind: "review" },
    });
  });
  await page.goto(`/projects/${pid}/review`);
  await page
    .getByLabel("Quality mode", { exact: true })
    .selectOption("optimize");
  await page.getByLabel("Publication", { exact: true }).selectOption("no");
  await page
    .getByRole("button", { name: "Run whole-book review", exact: true })
    .click();
  await expect
    .poll(() => body)
    .toEqual({ quality_mode: "optimize", autofix: false });
});

test("quality progress has its own phase and keeps cumulative elapsed time", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-09-25T00:00:40Z") });
  await qualityApi(page);
  await page.route(`**/api/projects/${pid}/workflow`, (route) =>
    route.fulfill({
      json: {
        source: "snapshot",
        kind: "review",
        status: "running",
        run_id: "quality-run",
        review_id: rid,
        stages: [],
        progress: {
          project_id: pid,
          run_id: "quality-run",
          kind: "review",
          label: "quality_scoring",
          done: 2,
          total: 4,
          elapsed_seconds: 40,
          updated_at: "2026-09-25T00:00:40Z",
        },
      },
    }),
  );
  await page.goto(`/projects/${pid}/review`);
  await expect(
    page.getByRole("heading", { name: "Scoring logical paragraphs" }),
  ).toBeVisible();
  await expect(
    page.getByRole("progressbar", { name: "Current stage progress" }),
  ).toHaveAttribute("aria-valuenow", "2");
  await page.clock.runFor(3000);
  await expect(page.getByTestId("review-elapsed")).toContainText("43 s");
});

test("proofreading shows one score for a logical continuation unit", async ({
  page,
}) => {
  await qualityApi(page);
  await page.goto(`/projects/${pid}/proofreading/0`);
  await expect(
    page.getByRole("article").getByText("Stale score", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("One unit across 2 fragments", { exact: true }),
  ).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: "Scores and candidate differences" }),
  ).toHaveCount(1);
});

test("quality routes accept economy tiers and ordinary generation models without TypeSafe", async ({
  page,
}) => {
  const { capabilities, configuration } = await import("./fixtures");
  let saved: Record<string, unknown> | undefined;
  const registered = {
    cheap_editor: {
      provider: "deepseek",
      model: "deepseek-flash",
      capabilities: ["generation", "judgment"],
    },
    native_judge: {
      provider: "typesafe",
      model: "jev-1.13.0",
      capabilities: ["judgment"],
    },
  };
  await fakeApi(page, {
    "/capabilities": {
      ...capabilities,
      operations: [
        ...capabilities.operations,
        {
          id: "review.quality_score",
          capability: "judgment",
          tier: null,
          requires_explicit_route: true,
        },
        {
          id: "review.quality_compare",
          capability: "judgment",
          tier: null,
          inherits: "review.quality_score",
        },
      ],
    },
    [`/projects/${pid}/config`]: {
      ...configuration,
      registered_models: registered,
    },
  });
  await page.route(`**/api/projects/${pid}/config`, (route) => {
    if (route.request().method() === "PUT")
      saved = JSON.parse(route.request().postDataJSON().yaml);
    return route.fulfill({
      json: {
        ...configuration,
        registered_models: registered,
        ...(saved ? { effective: saved, yaml: JSON.stringify(saved) } : {}),
      },
    });
  });
  await page.goto(`/projects/${pid}/settings`);
  await page
    .locator("summary")
    .filter({ hasText: "Models by operation" })
    .click();
  const score = page.getByLabel("Scoring logical paragraphs", { exact: true });
  await expect(score.locator('option[value="cheap_editor"]')).toHaveCount(1);
  await expect(score.locator('option[value="tier:cheap"]')).toHaveCount(1);
  await score.selectOption("tier:cheap");
  await page
    .getByLabel("Comparing candidates", { exact: true })
    .selectOption("cheap_editor");
  await expect(
    page.getByLabel("Quality tier").locator('option[value="native_judge"]'),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect
    .poll(() => (saved?.llm as { routes?: unknown } | undefined)?.routes)
    .toEqual({
      "review.quality_score": { tier: "cheap", fallbacks: [] },
      "review.quality_compare": { model: "cheap_editor", fallbacks: [] },
    });
});

for (const source of ["native", "generated"] as const) {
  test(`quality displays ${source} judgment provenance honestly`, async ({
    page,
  }) => {
    const generated = source === "generated";
    await qualityApi(page, {
      model: generated ? "deepseek-flash" : "jev-1.13.0",
      provenance: {
        source,
        confidence: generated ? "self_reported" : "model_distribution",
        probabilities: generated ? "self_reported" : "model_distribution",
        model_identity: generated ? "requested" : "resolved",
      },
    });
    await page.goto(`/projects/${pid}/review`);
    const panel = page.getByRole("region", {
      name: "Paragraph quality (experimental)",
    });
    await expect(
      panel.getByText(
        generated
          ? "Generated model judgment · self-reported signals"
          : "Native judgment · distribution signals",
        { exact: true },
      ),
    ).toBeVisible();
    await panel
      .getByRole("button", { name: "Scores and candidate differences" })
      .click();
    await expect(
      panel.getByText(
        generated
          ? "Requested scoring model: deepseek-flash"
          : "Reported scoring model: jev-1.13.0",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(
      panel.getByText(
        generated
          ? "These scores, confidence values and probabilities were reported by a generation model. They are not Jev distribution signals or calibrated probabilities of correctness."
          : "These confidence values and probabilities come from the native judgment response. They remain uncalibrated for your translation material.",
        { exact: true },
      ),
    ).toBeVisible();
  });
}
