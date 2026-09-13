import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Face, Recording, Settings, Status } from "../src/api";

// Contract fixtures exist only in tests. The application has no demo-data path.
const initialStatus: Status = {
  online: false,
  media_available: true,
  session_id: null,
  started_at: null,
  bitrate_mbps: 0,
  bitrate_history: [],
  tracks: [],
  recording: null,
  disk_free_bytes: 50 * 1024 ** 3,
  min_free_bytes: 2 * 1024 ** 3,
  warning: null,
  analysis: {
    enabled: false,
    state: "idle",
    provider: "cpu",
    error: null,
    last_seen: null,
  },
  face_count: 0,
};
const settings: Settings = {
  rtmp_url: "rtmp://localhost:1935/live",
  stream_key: "stream?user=publisher&pass=test-secret",
  analysis_enabled: false,
  match_threshold: 0.5,
  detection_threshold: 0.85,
  face_retention_days: 7,
  max_faces: 2000,
  analysis_fps: 2,
};
const face: Face = {
  id: 1,
  label: "Face 1",
  first_seen: "2026-09-13T12:00:00Z",
  last_seen: "2026-09-13T12:10:00Z",
  sightings: 3,
  detection_confidence: 0.98,
  match_similarity: 0.625,
  thumbnail_url: "/api/faces/1/thumbnail",
};

async function server(
  page: Page,
  options: {
    authenticated?: boolean;
    faces?: Face[];
    online?: boolean;
    statusDelay?: number;
    catalogDelay?: number;
    recordings?: Recording[];
  } = {},
) {
  let authenticated = options.authenticated ?? true;
  let currentStatus = {
    ...initialStatus,
    analysis: { ...initialStatus.analysis },
    online: options.online ?? false,
    face_count: options.faces?.length ?? 0,
  };
  let currentSettings = { ...settings };
  let faces = options.faces ?? [];
  let recordings = options.recordings ?? [];
  const calls: {
    path: string;
    method: string;
    csrf?: string;
    body: unknown;
  }[] = [];
  let activeStatus = 0;
  let maxStatus = 0;
  let activeCatalog = 0;
  let maxCatalog = 0;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const body = request.postData() ? request.postDataJSON() : null;
    calls.push({
      path: path + url.search,
      method,
      csrf: request.headers()["x-csrf-token"],
      body,
    });
    const json = (data: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    if (path === "/api/auth/login") {
      if (body.password !== "test-password")
        return json({ detail: "Invalid password" }, 401);
      authenticated = true;
      return json({ csrf_token: "test-csrf" });
    }
    if (!authenticated) return json({ detail: "Not authenticated" }, 401);
    if (method !== "GET" && request.headers()["x-csrf-token"] !== "test-csrf")
      return json({ detail: "CSRF token missing" }, 403);
    if (path === "/api/auth/me") return json({ csrf_token: "test-csrf" });
    if (path === "/api/auth/logout") {
      authenticated = false;
      return route.fulfill({ status: 204 });
    }
    if (path === "/api/status") {
      activeStatus++;
      maxStatus = Math.max(maxStatus, activeStatus);
      if (options.statusDelay)
        await new Promise((resolve) =>
          setTimeout(resolve, options.statusDelay),
        );
      activeStatus--;
      return json(currentStatus);
    }
    if (path === "/api/analysis") {
      currentStatus.analysis.enabled = body.enabled;
      return json({ enabled: body.enabled });
    }
    if (path === "/api/settings") return json(currentSettings);
    if (path === "/api/stream/key") {
      currentSettings.stream_key = "stream?user=publisher&pass=regenerated";
      return json(currentSettings);
    }
    if (path === "/api/sessions")
      return json({
        items: [
          {
            id: "session-test",
            started_at: "2026-09-13T12:00:00Z",
            ended_at: "2026-09-13T12:30:00Z",
          },
        ],
      });
    if (path === "/api/faces" && method === "GET") {
      activeCatalog++;
      maxCatalog = Math.max(maxCatalog, activeCatalog);
      if (options.catalogDelay)
        await new Promise((resolve) =>
          setTimeout(resolve, options.catalogDelay),
        );
      activeCatalog--;
      const offset = Number(url.searchParams.get("offset"));
      const limit = Number(url.searchParams.get("limit"));
      return json({
        items: faces.slice(offset, offset + limit),
        total: faces.length,
      });
    }
    if (path.startsWith("/api/faces") && method === "DELETE") {
      faces =
        path === "/api/faces"
          ? []
          : faces.filter((item) => item.id !== Number(path.split("/")[3]));
      currentStatus.face_count = faces.length;
      return route.fulfill({ status: 204 });
    }
    if (path.endsWith("/thumbnail")) return route.fulfill({ status: 404 });
    if (path === "/api/recordings/start") {
      currentStatus.recording = {
        id: "recording-test",
        started_at: "2026-09-13T12:00:00Z",
      };
      return json(currentStatus.recording);
    }
    if (path === "/api/recordings/stop") {
      currentStatus.recording = null;
      return route.fulfill({ status: 204 });
    }
    if (path === "/api/recordings" && method === "GET")
      return json({ items: recordings });
    if (path.startsWith("/api/recordings/") && method === "DELETE") {
      recordings = recordings.filter((item) => item.id !== path.split("/")[3]);
      return route.fulfill({ status: 204 });
    }
    if (path.startsWith("/api/live/"))
      return route.fulfill({ status: 503, body: "No stream in browser tests" });
    return json({ detail: `Unexpected endpoint: ${path}` }, 404);
  });
  return {
    calls,
    expire: () => {
      authenticated = false;
    },
    maxStatus: () => maxStatus,
    maxCatalog: () => maxCatalog,
    status: (update: Partial<Status>) => {
      currentStatus = { ...currentStatus, ...update };
    },
  };
}

test("password login, CSRF mutation, keyboard tabs, and logout", async ({
  page,
}) => {
  const backend = await server(page, { authenticated: false });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Enter the control room" }),
  ).toBeVisible();
  await page.getByLabel("Administrator password").fill("incorrect");
  await page.getByRole("button", { name: "Open console" }).click();
  await expect(page.getByRole("alert")).toHaveText("Invalid password");
  await page.getByLabel("Administrator password").fill("test-password");
  await page.getByRole("button", { name: "Open console" }).click();
  await expect(
    page.getByRole("heading", { name: "Broadcast workspace." }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "No faces yet" }),
  ).toBeVisible();
  await page.getByRole("switch", { name: "Enable face analysis" }).click();
  await expect(page.getByRole("switch")).toBeChecked();
  expect(
    backend.calls.find((call) => call.path === "/api/analysis"),
  ).toMatchObject({
    method: "POST",
    csrf: "test-csrf",
    body: { enabled: true },
  });
  await page.getByRole("tab", { name: /^Faces/ }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Recordings" })).toBeFocused();
  await expect(page.getByRole("tabpanel")).toHaveAttribute(
    "aria-labelledby",
    "tab-recordings",
  );
  await page.keyboard.press("End");
  await expect(page.getByRole("tab", { name: "Settings" })).toBeFocused();
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(
    page.getByRole("heading", { name: "Enter the control room" }),
  ).toBeVisible();
  expect(
    backend.calls.find((call) => call.path === "/api/auth/logout"),
  ).toMatchObject({ method: "POST", csrf: "test-csrf" });
});

test("secrets stay masked; regeneration requires confirmation and offline media", async ({
  page,
}) => {
  const backend = await server(page);
  await page.goto("/");
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.locator("#stream-key")).toHaveAttribute("type", "password");
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.getByRole("button", { name: "Copy entire stream key" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    settings.stream_key,
  );
  await page
    .getByRole("button", { name: "Reveal stream key for 30 seconds" })
    .click();
  await expect(page.locator("#stream-key")).toHaveAttribute("type", "text");
  await expect(page.locator("#stream-key")).toHaveValue(settings.stream_key);
  await page
    .getByRole("button", { name: "Regenerate stream key", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  expect(
    backend.calls.filter((call) => call.path === "/api/stream/key"),
  ).toHaveLength(0);
  await page
    .getByRole("button", { name: "Regenerate stream key", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Regenerate key", exact: true })
    .click();
  await expect(page.locator("#stream-key")).toHaveValue(
    "stream?user=publisher&pass=regenerated",
  );
  await expect(page.locator("#stream-key")).toHaveAttribute("type", "password");
  backend.status({ media_available: false });
  await expect(
    page.getByRole("button", { name: "Regenerate stream key", exact: true }),
  ).toBeDisabled();
});

test("face scores, pagination, session filter, and confirmed deletions", async ({
  page,
}) => {
  const backend = await server(page, {
    faces: Array.from({ length: 13 }, (_, index) => ({
      ...face,
      id: index + 1,
      label: `Face ${index + 1}`,
      match_similarity: index === 0 ? null : 0.625,
    })),
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Face 1", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("No match yet")).toBeVisible();
  await expect(page.getByText("98.0", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("0.625", { exact: true }).first()).toBeVisible();
  await expect(
    page.getByText("not a probability", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Next page" }).click();
  await expect(
    page.getByRole("heading", { name: "Face 13", exact: true }),
  ).toBeVisible({ timeout: 7000 });
  expect(
    backend.calls.some((call) => call.path.includes("offset=12")),
  ).toBeTruthy();
  await page
    .getByRole("button", { name: "Delete Face 13", exact: true })
    .click();
  await page.getByRole("button", { name: "Delete face", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Face 1", exact: true }),
  ).toBeVisible({ timeout: 7000 });
  await page
    .getByLabel("Session", { exact: true })
    .selectOption("session-test");
  await expect
    .poll(
      () =>
        backend.calls.some((call) =>
          call.path.includes("session_id=session-test"),
        ),
      { timeout: 7000 },
    )
    .toBeTruthy();
  await page.getByRole("button", { name: "Clear all", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("across every session");
  await page
    .getByRole("button", { name: "Delete all faces", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "No faces yet" }),
  ).toBeVisible();
  expect(
    backend.calls.find(
      (call) => call.path === "/api/faces" && call.method === "DELETE",
    )?.csrf,
  ).toBe("test-csrf");
});

test("status polls are serial even when responses exceed the interval; 401 logs out", async ({
  page,
}) => {
  const backend = await server(page, { statusDelay: 1400 });
  await page.goto("/");
  await expect
    .poll(
      () => backend.calls.filter((call) => call.path === "/api/status").length,
      { timeout: 10000 },
    )
    .toBeGreaterThanOrEqual(3);
  expect(backend.maxStatus()).toBe(1);
  backend.expire();
  await expect(
    page.getByRole("heading", { name: "Enter the control room" }),
  ).toBeVisible({ timeout: 7000 });
  await expect(
    page.getByText("Your session has expired. Sign in to reconnect."),
  ).toBeVisible();
});

test("manual recording starts and stops, then disables when stream goes offline", async ({
  page,
}) => {
  const backend = await server(page, { online: true });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Start recording", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Recording in progress" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Stop recording", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Manual recording" }),
  ).toBeVisible();
  backend.status({ online: false });
  await expect(
    page.getByRole("button", { name: "Start recording", exact: true }),
  ).toBeDisabled();
  expect(
    backend.calls.find((call) => call.path === "/api/recordings/start")?.csrf,
  ).toBe("test-csrf");
  expect(
    backend.calls.find((call) => call.path === "/api/recordings/stop")?.csrf,
  ).toBe("test-csrf");
});

test("desktop and mobile have no horizontal overflow or browser errors", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await server(page, { faces: [face] });
  for (const width of [1440, 1024, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "Face 1", exact: true }),
    ).toBeVisible();
    if (width === 1440 || width === 390)
      await page.screenshot({
        path: process.env.STEAMLAB_SCREENSHOT_DIR
          ? `${process.env.STEAMLAB_SCREENSHOT_DIR}/screenshot-${width === 1440 ? "dashboard" : "mobile"}.png`
          : test.info().outputPath(`dashboard-${width}.png`),
        fullPage: true,
      });
    for (const name of [/^Faces/, /^Recordings$/, /^Settings$/]) {
      await page.getByRole("tab", { name }).click();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBeTruthy();
    }
  }
  expect(errors).toEqual([]);
});

test("recording playback dialog, download URL, and guarded deletion", async ({
  page,
}) => {
  const recording: Recording = {
    id: "archive-test",
    started_at: "2026-09-13T12:00:00Z",
    ended_at: "2026-09-13T12:01:00Z",
    status: "ready",
    size_bytes: 1024 ** 2,
    duration_seconds: 60,
    playback_url: "/api/recordings/archive-test/file",
    download_url: "/api/recordings/archive-test/file?download=1",
    error: null,
  };
  const backend = await server(page, { recordings: [recording] });
  await page.goto("/");
  await page.getByRole("tab", { name: "Recordings" }).click();
  await expect(
    page.getByRole("link", { name: /^Download recording from/ }),
  ).toHaveAttribute("href", recording.download_url!);
  const play = page.getByRole("button", { name: "Play recording" });
  await play.click();
  await expect(
    page.getByRole("dialog", { name: "Recording playback" }),
  ).toBeVisible();
  await expect(page.locator(".playback-video")).toHaveAttribute(
    "src",
    recording.playback_url!,
  );
  await page.keyboard.press("Escape");
  await expect(play).toBeFocused();
  await expect(page.locator(".playback-video")).toHaveCount(0);
  await page.getByRole("button", { name: /^Delete recording from/ }).click();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(backend.calls.filter((call) => call.method === "DELETE")).toHaveLength(
    0,
  );
  await page.getByRole("button", { name: /^Delete recording from/ }).click();
  await page
    .getByRole("button", { name: "Delete recording", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Your archive starts here" }),
  ).toBeVisible();
  expect(backend.calls.find((call) => call.method === "DELETE")).toMatchObject({
    path: "/api/recordings/archive-test",
    csrf: "test-csrf",
  });
});

test("slow catalog requests remain serial and preserve endpoint order", async ({
  page,
}) => {
  const backend = await server(page, { catalogDelay: 4500 });
  await page.goto("/");
  await expect
    .poll(
      () =>
        backend.calls.filter((call) => call.path.startsWith("/api/faces?"))
          .length,
      { timeout: 15000 },
    )
    .toBeGreaterThanOrEqual(2);
  expect(backend.maxCatalog()).toBe(1);
  const catalogCalls = backend.calls.filter((call) =>
    /\/api\/(faces\?|sessions|recordings$|settings)/.test(call.path),
  );
  expect(
    catalogCalls.slice(0, 5).map((call) => call.path.split("?")[0]),
  ).toEqual([
    "/api/faces",
    "/api/sessions",
    "/api/recordings",
    "/api/settings",
    "/api/faces",
  ]);
});

test("HLS reconnects for a new session and disposes when the stream goes offline", async ({
  page,
}) => {
  const backend = await server(page, { online: true });
  const manifests = () =>
    backend.calls.filter((call) => call.path === "/api/live/index.m3u8").length;
  await page.goto("/");
  await expect.poll(manifests).toBeGreaterThan(0);
  const before = manifests();
  backend.status({ session_id: "next-session" });
  await expect.poll(manifests).toBeGreaterThan(before);
  backend.status({ online: false, session_id: null });
  await expect(
    page.getByRole("heading", { name: "Waiting for your broadcast" }),
  ).toBeVisible();
  const after = manifests();
  await page.waitForTimeout(2500);
  expect(manifests()).toBe(after);
  await expect(page.locator(".live-player video")).not.toHaveAttribute("src");
});

test("an HLS 401 returns to login even if status requests still succeed", async ({
  page,
}) => {
  await server(page, { online: true });
  await page.route("**/api/live/**", (route) =>
    route.fulfill({ status: 401, body: "Not authenticated" }),
  );
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Enter the control room" }),
  ).toBeVisible();
  await expect(
    page.getByText("Your session has expired. Sign in to reconnect."),
  ).toBeVisible();
});
