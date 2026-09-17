// Actual bundled host checks; invoked by scripts/smoke_windows_webui.py while it is running.
import { chromium, expect } from "@playwright/test";
import fs from "node:fs/promises";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const { url, archive, screenshot, result } = JSON.parse(input);
const browser = await chromium.launch();
try {
  const page = await browser.newPage({
    locale: "en-US",
    viewport: { width: 1360, height: 960 },
  });
  await page.goto(url);
  await page.getByRole("link", { name: "Import Docker projects" }).click();
  await page.getByLabel("Choose .wenyi.zip archive").setInputFiles(archive);
  await expect(
    page.getByRole("checkbox", { name: "Portable 中文" }),
  ).toBeChecked({ timeout: 30000 });
  await page
    .getByRole("button", { name: "Import 1 selected projects" })
    .click();
  const imported = page.getByRole("link", { name: "Open imported project" });
  await expect(imported).toBeVisible({ timeout: 30000 });
  const pid = (await imported.getAttribute("href")).split("/").pop();
  await page.reload();
  await expect(imported).toBeVisible();
  await page.screenshot({ path: screenshot, fullPage: true });
  await imported.click();
  await page.reload();
  await expect(
    page.getByText("Portable 中文", { exact: false }).first(),
  ).toBeVisible();
  const snapshot = await page.evaluate(
    (projectId) =>
      new Promise((resolve, reject) => {
        const socket = new WebSocket(
          `ws://${location.host}/ws/projects/${projectId}/progress`,
        );
        const timer = setTimeout(() => {
          socket.close();
          reject(new Error("Progress socket timed out"));
        }, 10000);
        socket.onopen = () =>
          socket.send(
            JSON.stringify({ token: localStorage.getItem("wenyi_token") }),
          );
        socket.onmessage = (event) => {
          clearTimeout(timer);
          socket.close();
          resolve(JSON.parse(event.data));
        };
        socket.onerror = () => {
          clearTimeout(timer);
          reject(new Error("Progress socket failed"));
        };
      }),
    pid,
  );
  expect(snapshot.kind).toBe("snapshot");
  expect(snapshot.project.id).toBe(pid);
  await fs.writeFile(
    result,
    JSON.stringify({
      project_id: pid,
      websocket: "ok",
      spa: "ok",
      migration: "ok",
    }),
  );
} finally {
  await browser.close();
}
