import { test, expect } from "@playwright/test";
import { fakeApi } from "./fixtures";

const preview = {
  upload_id: "upload-one",
  package_id: "package-one",
  source_version: "1.0.0",
  registry_revision: 2,
  projects: [
    {
      id: "source-a",
      name: "Docker book",
      format: "text",
      status: "paused",
      name_conflict: true,
      already_imported: false,
      counts: { segments: 12, glossary: 3 },
      file_count: 4,
      bytes: 128,
      conflicts: [
        { kind: "models", source: "default", target: "default_import_123" },
      ],
      missing_credentials: ["MODEL_KEY_IMPORT_123"],
      warnings: [],
    },
    {
      id: "source-b",
      name: "Another book",
      format: "srt",
      status: "done",
      name_conflict: false,
      already_imported: false,
      counts: {},
      file_count: 1,
      bytes: 128,
      conflicts: [],
      missing_credentials: [],
      warnings: [],
    },
  ],
};

test("preview selects projects and reports successful merge after reload", async ({
  page,
}) => {
  await fakeApi(page);
  let imported = false;
  let submitted: unknown;
  const results = [
    {
      source_id: "source-a",
      project_id: "new-book",
      status: "done",
      error: null,
    },
  ];
  await page.route("**/api/transfers/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/import")) {
      submitted = route.request().postDataJSON();
      imported = true;
      await route.fulfill({ json: results });
    } else if (path.endsWith("/results"))
      await route.fulfill({ json: imported ? results : [] });
    else
      await route.fulfill({
        json: {
          ...preview,
          projects: preview.projects.map((p) => ({
            ...p,
            already_imported: imported && p.id === "source-a",
          })),
        },
      });
  });
  await page.goto("/transfers");
  await page
    .getByLabel("Choose .wenyi.zip archive")
    .setInputFiles({
      name: "book.wenyi.zip",
      mimeType: "application/zip",
      buffer: Buffer.from("fixture"),
    });
  await expect(
    page.getByText("MODEL_KEY_IMPORT_123", { exact: false }),
  ).toBeVisible();
  await page.getByRole("checkbox", { name: "Another book" }).uncheck();
  await page
    .getByRole("button", { name: "Import 1 selected projects" })
    .click();
  expect(submitted).toEqual({
    project_ids: ["source-a"],
    registry_revision: 2,
  });
  await expect(
    page.getByRole("link", { name: "Open imported project" }),
  ).toHaveAttribute("href", "/projects/new-book");
  await page.reload();
  await expect(
    page.getByRole("link", { name: "Open imported project" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Refresh preview" }),
  ).toBeEnabled();
  await expect(
    page.getByRole("checkbox", { name: "Docker book", exact: false }),
  ).toBeDisabled();
});

test("desktop token is removed from URL and used for HTTP and WebSocket auth", async ({
  page,
}) => {
  await fakeApi(page);
  let token: unknown;
  await page.routeWebSocket("**/ws/**", (socket) =>
    socket.onMessage((data) => {
      token = JSON.parse(String(data)).token;
    }),
  );
  await page.goto("/projects/book-1#wenyi-token=desktop-session");
  await expect(page).toHaveURL(/\/projects\/book-1$/);
  await expect.poll(() => token).toBe("desktop-session");
  const request = page.waitForRequest(
    (req) =>
      req.url().includes("/api/projects/book-1") &&
      req.headers().authorization === "Bearer desktop-session",
  );
  await page.reload();
  await request;
});

test("invalid migration archive shows actionable validation error", async ({
  page,
}) => {
  await fakeApi(page);
  await page.route("**/api/transfers/preview", (route) =>
    route.fulfill({
      status: 422,
      json: { detail: "Transfer checksum mismatch" },
    }),
  );
  await page.goto("/transfers");
  await page
    .getByLabel("Choose .wenyi.zip archive")
    .setInputFiles({
      name: "broken.wenyi.zip",
      mimeType: "application/zip",
      buffer: Buffer.from("bad"),
    });
  await expect(page.getByRole("alert")).toContainText(
    "Transfer checksum mismatch",
  );
  await expect(page.getByLabel("Choose .wenyi.zip archive")).toBeEnabled();
});
