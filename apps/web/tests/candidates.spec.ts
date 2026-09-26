import { expect, test } from "@playwright/test";
import { capabilities, configuration, fakeApi, pid } from "./fixtures";

const candidatePath = `/projects/${pid}/chapters/0/segments/0/candidates`;
const comparison = {
  batch_id: "a".repeat(64),
  chapter: 0,
  segment_indices: [0, 4],
  sources: ["First source", "Second source"],
  status: "published",
  extra_generations: 2,
  candidates: [
    {
      id: "A",
      raw_targets: ["Draft A", "Later A"],
      targets: ["Other translation", "Other ending"],
      polish_status: "complete",
    },
    {
      id: "B",
      raw_targets: ["Draft B", "Later B"],
      targets: ["Chosen by judge", "Chosen ending"],
      polish_status: "complete",
    },
    {
      id: "C",
      raw_targets: ["Draft C", "Later C"],
      targets: ["Other translation", "Other ending"],
      polish_status: "complete",
      duplicate_of: "A",
    },
  ],
  decision: {
    choice: "B",
    model: "fallback-editor",
    confidence: 0.8,
    fallback_used: true,
  },
};

test("candidate comparison loads on demand and separates history from the current translation", async ({
  page,
}) => {
  await fakeApi(page, { [candidatePath]: comparison });
  let requests = 0;
  let writes = 0;
  page.on("request", (request) => {
    if (request.url().endsWith(candidatePath)) requests++;
    if (request.method() === "PUT") writes++;
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page
    .getByRole("button", { name: "Paragraph actions", exact: true })
    .click();
  await page
    .getByRole("menuitem", { name: "Edit translation", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("Edit translation", { exact: true })
    .fill("Unsaved revision");
  expect(requests).toBe(0);
  await dialog.getByRole("tab", { name: "Candidate comparison" }).click();
  await expect(
    dialog.getByText("Selected for this batch: B", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("Judge: fallback-editor", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("A fallback judge made this selection."),
  ).toBeVisible();
  await expect(
    dialog.getByText("Judge confidence: 80%", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("Identical to candidate A", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText(
      /The current translation has changed since this selection/,
    ),
  ).toBeVisible();
  await expect(
    dialog.getByText("Original translation", { exact: true }),
  ).toBeVisible();
  await expect(dialog.getByRole("textbox")).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Save translation" }),
  ).toHaveCount(0);
  const winner = dialog
    .getByRole("heading", { name: "Candidate B", exact: true })
    .locator("..");
  await winner
    .locator("summary")
    .filter({ hasText: "Compare the complete batch" })
    .click();
  await expect(
    winner.getByText("Chosen ending", { exact: true }),
  ).toBeVisible();
  await winner
    .locator("summary")
    .filter({ hasText: "Translation before polishing" })
    .last()
    .click();
  await expect(winner.getByText("Later B", { exact: true })).toBeVisible();
  await dialog.getByRole("tab", { name: "Translation", exact: true }).click();
  await expect(
    dialog.getByLabel("Edit translation", { exact: true }),
  ).toHaveValue("Unsaved revision");
  expect(requests).toBe(1);
  expect(writes).toBe(0);
});

test("old projects show an empty candidate state in Chinese", async ({
  page,
}) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page, { [candidatePath]: null });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page.getByRole("button", { name: "段落操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "编辑译文", exact: true }).click();
  await page.getByRole("tab", { name: "候选对照", exact: true }).click();
  await expect(
    page.getByText("此段落没有候选记录。", { exact: true }),
  ).toBeVisible();
});

test("workflow explicitly selects a replaceable judge and excludes choice-only models from text tiers", async ({
  page,
}) => {
  const models = {
    ...configuration.registered_models,
    jev: { provider: "typesafe", model: "jev-1.13.0", supports_text: false },
    editor: {
      provider: "generic",
      model: "another-judge",
      supports_text: true,
    },
  };
  let saved: typeof configuration.effective & {
    pipeline: { best_of_three?: boolean };
    llm: { routes: Record<string, { model?: string; fallbacks?: string[] }> };
  } = structuredClone(configuration.effective);
  const response = () => ({
    ...configuration,
    effective: saved,
    yaml: JSON.stringify(saved),
    registered_models: models,
  });
  await fakeApi(page, {
    [`/projects/${pid}/config`]: response(),
    "/capabilities": {
      ...capabilities,
      operations: [
        ...capabilities.operations,
        { id: "translation.judge", tier: "strong", request_kind: "choice" },
      ],
    },
  });
  await page.route(`**/api/projects/${pid}/config`, async (route) => {
    if (route.request().method() === "PUT") {
      const input = JSON.parse(route.request().postDataJSON().yaml);
      if (
        input.pipeline.best_of_three &&
        !input.llm.routes["translation.judge"]
      ) {
        await route.fulfill({
          status: 422,
          json: { detail: "Choose a translation judge" },
        });
        return;
      }
      saved = input;
    }
    await route.fulfill({ json: response() });
  });
  await page.goto(`/projects/${pid}/settings`);
  await expect(
    page.getByLabel("Best of three", { exact: true }),
  ).not.toBeChecked();
  await page.getByLabel("Best of three", { exact: true }).check();
  const judge = page.getByRole("combobox", {
    name: "Translation judge",
    exact: true,
  });
  await expect(judge).toHaveValue("");
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect(
    page.getByText("422: Choose a translation judge", { exact: true }).first(),
  ).toBeVisible();
  await judge.selectOption("jev");
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect(
    page.getByText("Project settings saved", { exact: true }),
  ).toBeVisible();
  expect(saved.pipeline.best_of_three).toBe(true);
  expect(saved.llm.routes["translation.judge"]).toEqual({
    model: "jev",
    fallbacks: [],
  });
  await expect(
    page.getByLabel("Quality tier").locator("option[value='jev']"),
  ).toHaveCount(0);
  await page.reload();
  await expect(judge).toHaveValue("jev");
  await judge.selectOption("editor");
  await page
    .getByRole("button", { name: "Save configuration", exact: true })
    .click();
  await expect(
    page.getByText("Project settings saved", { exact: true }),
  ).toBeVisible();
  expect(saved.llm.routes["translation.judge"].model).toBe("editor");
});
