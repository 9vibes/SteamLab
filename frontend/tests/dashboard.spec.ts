import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Face, Recording, Settings, Status, Stream } from "../src/api";

// Contract fixtures exist only in tests. The application has no demo-data path.
const initialStatus: Status = {
  stream_id: "stream",
  stream_name: "Stream 1",
  archived: false,
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
  stream_id: "stream",
  stream_name: "Stream 1",
  archived: false,
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
  thumbnail_url: "/api/faces/1/thumbnail?stream_id=stream",
};

const defaultStream: Stream = {
  id: "stream",
  name: "Stream 1",
  created_at: "2026-09-13T12:00:00Z",
  archived_at: null,
  is_default: true,
  online: false,
  media_available: true,
  recording: null,
  bitrate_mbps: 0,
  analysis_enabled: false,
};
const secondStream: Stream = {
  ...defaultStream,
  id: `stream-${"a".repeat(32)}`,
  name: "Studio B",
  is_default: false,
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
    streams?: Stream[];
    historyStreamId?: string;
  } = {},
) {
  let authenticated = options.authenticated ?? true;
  const initial = {
    ...initialStatus,
    analysis: { ...initialStatus.analysis },
    online: options.online ?? false,
    face_count: options.faces?.length ?? 0,
  };
  const streams = (
    options.streams ?? [{ ...defaultStream, online: initial.online }]
  ).map((stream) => ({ ...stream }));
  const data = new Map(
    streams.map((stream) => [
      stream.id,
      {
        status: {
          ...initial,
          face_count:
            stream.id === (options.historyStreamId ?? "stream")
              ? initial.face_count
              : 0,
          stream_id: stream.id,
          stream_name: stream.name,
          archived: !!stream.archived_at,
          online: stream.online,
          recording: stream.recording,
          analysis: { ...initial.analysis },
        },
        settings: {
          ...settings,
          stream_id: stream.id,
          stream_name: stream.name,
          archived: !!stream.archived_at,
          stream_key: stream.archived_at
            ? ""
            : stream.is_default
              ? settings.stream_key
              : `${stream.id}?user=publisher&pass=second-secret`,
        },
        faces:
          stream.id === (options.historyStreamId ?? "stream")
            ? (options.faces ?? [])
            : [],
        recordings:
          stream.id === (options.historyStreamId ?? "stream")
            ? (options.recordings ?? [])
            : [],
      },
    ]),
  );
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
    if (path === "/api/streams" && method === "GET")
      return json({
        items: streams.map((stream) => ({
          ...stream,
          online: data.get(stream.id)!.status.online,
          recording: data.get(stream.id)!.status.recording,
          media_available: data.get(stream.id)!.status.media_available,
        })),
        max_streams: 4,
        active_count: streams.filter((stream) => !stream.archived_at).length,
      });
    if (path === "/api/streams" && method === "POST") {
      if (streams.filter((stream) => !stream.archived_at).length >= 4)
        return json(
          { detail: "At most four active streams are allowed." },
          409,
        );
      const stream = {
        ...defaultStream,
        id: `stream-${String(streams.length).padStart(32, "0")}`,
        name: body.name,
        is_default: false,
      };
      streams.push(stream);
      data.set(stream.id, {
        status: {
          ...initialStatus,
          stream_id: stream.id,
          stream_name: stream.name,
        },
        settings: {
          ...settings,
          stream_id: stream.id,
          stream_name: stream.name,
          stream_key: `${stream.id}?user=publisher&pass=new-secret`,
        },
        faces: [],
        recordings: [],
      });
      return json(stream, 201);
    }
    if (/^\/api\/streams\/[^/]+$/.test(path)) {
      const stream = streams.find((item) => item.id === path.split("/")[3])!;
      const owned = data.get(stream.id)!;
      if (method === "PATCH") {
        stream.name = body.name;
        owned.status.stream_name = body.name;
        owned.settings.stream_name = body.name;
        return json(stream);
      }
      if (method === "DELETE") {
        if (
          stream.is_default ||
          owned.status.online ||
          owned.status.recording ||
          !owned.status.media_available
        )
          return json(
            {
              detail:
                "Stream cannot be archived while publishing or recording.",
            },
            409,
          );
        stream.archived_at = "2026-09-13T13:00:00Z";
        owned.status.archived = true;
        owned.settings.archived = true;
        owned.settings.stream_key = "";
        return route.fulfill({ status: 204 });
      }
    }
    if (/^\/api\/streams\/[^/]+\/live\//.test(path))
      return route.fulfill({ status: 503, body: "No stream in browser tests" });
    if (!url.searchParams.has("stream_id"))
      return json({ detail: "Missing stream scope in browser request" }, 400);
    const owned = data.get(url.searchParams.get("stream_id")!)!;
    const currentStatus = owned.status;
    const currentSettings = owned.settings;
    let faces = owned.faces;
    let recordings = owned.recordings;
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
      owned.faces = faces;
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
      owned.recordings = recordings;
      return route.fulfill({ status: 204 });
    }
    return json({ detail: `Unexpected endpoint: ${path}` }, 404);
  });
  return {
    calls,
    expire: () => {
      authenticated = false;
    },
    maxStatus: () => maxStatus,
    maxCatalog: () => maxCatalog,
    status: (update: Partial<Status>, streamId = "stream") => {
      const owned = data.get(streamId)!;
      owned.status = { ...owned.status, ...update };
    },
  };
}

test("password login, CSRF mutation, keyboard tabs, and logout", async ({
  page,
}) => {
  const backend = await server(page, { authenticated: false });
  await page.goto("/");
  await expect(page).toHaveTitle("KUNAS/Labs | Broadcast Console");
  await expect(page.locator(".brand")).toHaveText(
    "KUNAS/LabsBROADCAST CONSOLE",
  );
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
  await expect(page.locator(".app-header .brand")).toHaveText(
    "KUNAS/LabsBROADCAST CONSOLE",
  );
  await expect(
    page.getByRole("heading", { name: "No faces yet" }),
  ).toBeVisible();
  await page.getByRole("switch", { name: "Enable face analysis" }).click();
  await expect(page.getByRole("switch")).toBeChecked();
  expect(
    backend.calls.find(
      (call) => call.path === "/api/analysis?stream_id=stream",
    ),
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
    backend.calls.filter(
      (call) => call.path === "/api/stream/key?stream_id=stream",
    ),
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
      (call) =>
        call.path === "/api/faces?stream_id=stream" && call.method === "DELETE",
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
      () =>
        backend.calls.filter(
          (call) => call.path === "/api/status?stream_id=stream",
        ).length,
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
    backend.calls.find(
      (call) => call.path === "/api/recordings/start?stream_id=stream",
    )?.csrf,
  ).toBe("test-csrf");
  expect(
    backend.calls.find(
      (call) => call.path === "/api/recordings/stop?stream_id=stream",
    )?.csrf,
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
    playback_url: "/api/recordings/archive-test/file?stream_id=stream",
    download_url:
      "/api/recordings/archive-test/file?download=1&stream_id=stream",
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
    path: "/api/recordings/archive-test?stream_id=stream",
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
    /\/api\/(faces\?|sessions|recordings\?|settings)/.test(call.path),
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
    backend.calls.filter(
      (call) => call.path === "/api/streams/stream/live/index.m3u8",
    ).length;
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
  await page.route("**/api/streams/*/live/**", (route) =>
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

test("add streams up to four, select the new stream, and enforce the UI limit", async ({
  page,
}) => {
  const backend = await server(page);
  await page.goto("/");
  for (const name of ["Studio B", "Studio C", "Studio D"]) {
    await page.getByRole("button", { name: "Add stream", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Add stream" });
    await expect(dialog.getByLabel("Stream name")).toBeFocused();
    await dialog.getByLabel("Stream name").fill(`  ${name}  `);
    await dialog
      .getByRole("button", { name: "Add stream", exact: true })
      .click();
    await expect(dialog).not.toBeVisible();
    await expect(
      page.getByRole("button", { name: new RegExp(`^${name} Offline`) }),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".selected-stream-name")).toContainText(name);
  }
  await expect(page.getByText("4 / 4 active", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Add stream", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("All 4 active slots are in use.", { exact: false }),
  ).toBeVisible();
  const additions = backend.calls.filter(
    (call) => call.method === "POST" && call.path === "/api/streams",
  );
  expect(additions.map((call) => call.body)).toEqual([
    { name: "Studio B" },
    { name: "Studio C" },
    { name: "Studio D" },
  ]);
  expect(additions.every((call) => call.csrf === "test-csrf")).toBeTruthy();
  expect(
    backend.calls.some((call) => call.path.startsWith("/api/streams?")),
  ).toBeFalsy();
});

test("add shows server limit errors and keeps the accessible form open", async ({
  page,
}) => {
  await server(page);
  await page.route("**/api/streams", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    return route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        detail: "At most four active streams are allowed.",
      }),
    });
  });
  await page.goto("/");
  const add = page.getByRole("button", { name: "Add stream", exact: true });
  await add.click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Stream name").fill("Another studio");
  await dialog.getByRole("button", { name: "Add stream", exact: true }).click();
  await expect(dialog.getByRole("alert")).toHaveText(
    "At most four active streams are allowed.",
  );
  await expect(dialog.getByLabel("Stream name")).toHaveValue("Another studio");
  await page.keyboard.press("Escape");
  await expect(add).toBeFocused();
});

test("rename preserves the default stream identity, credentials, and archive guard", async ({
  page,
}) => {
  const backend = await server(page);
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Archive stream", exact: true }),
  ).toBeDisabled();
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.locator("#stream-key")).toHaveValue(settings.stream_key);
  await page.getByRole("button", { name: "Rename stream" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("Stream name")).toHaveValue("Stream 1");
  await expect(dialog.getByLabel("Stream name")).toHaveAttribute(
    "maxlength",
    "64",
  );
  await dialog.getByLabel("Stream name").fill("Main stage");
  await dialog.getByRole("button", { name: "Save name" }).click();
  await expect(page.locator(".preview-panel .panel-heading")).toContainText(
    "Main stage",
  );
  await expect(page.locator("#stream-key")).toHaveValue(settings.stream_key);
  await expect(page.getByRole("tab", { name: "Settings" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    page.getByRole("button", { name: "Archive stream", exact: true }),
  ).toBeDisabled();
  expect(backend.calls.find((call) => call.method === "PATCH")).toMatchObject({
    path: "/api/streams/stream",
    csrf: "test-csrf",
    body: { name: "Main stage" },
  });
  expect(backend.calls.filter((call) => call.method === "POST")).toHaveLength(
    0,
  );
});

test("archive confirmation preserves histories, frees a slot, and disables live operations", async ({
  page,
}) => {
  const recording: Recording = {
    id: "preserved",
    started_at: "2026-09-13T12:00:00Z",
    ended_at: "2026-09-13T12:01:00Z",
    status: "ready",
    size_bytes: 1024,
    duration_seconds: 60,
    playback_url: `/api/recordings/preserved/file?stream_id=${secondStream.id}`,
    download_url: `/api/recordings/preserved/file?stream_id=${secondStream.id}&download=1`,
    error: null,
  };
  const backend = await server(page, {
    streams: [defaultStream, secondStream],
    historyStreamId: secondStream.id,
    faces: [
      {
        ...face,
        thumbnail_url: `/api/faces/1/thumbnail?stream_id=${secondStream.id}`,
      },
    ],
    recordings: [recording],
  });
  await page.goto("/");
  await page.getByRole("button", { name: /^Studio B Offline/ }).click();
  await page
    .getByRole("button", { name: "Archive stream", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Archive stream?",
    exact: true,
  });
  await expect(dialog).toContainText(
    "Faces, sessions, and recordings are preserved",
  );
  await expect(dialog).toContainText("This cannot be undone");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(backend.calls.filter((call) => call.method === "DELETE")).toHaveLength(
    0,
  );
  await page
    .getByRole("button", { name: "Archive stream", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Archive stream", exact: true })
    .click();
  await expect(
    page.getByText("Preserved history mode.", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("1 / 4 active", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Archived history")).toHaveValue(
    secondStream.id,
  );
  await expect(
    page.getByRole("button", { name: "Start recording", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("switch", { name: "Enable face analysis" }),
  ).toBeDisabled();
  await expect(page.locator(".live-player video")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Face 1", exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByLabel("Session", { exact: true })
      .locator("option[value='session-test']"),
  ).toHaveCount(1);
  await page.getByRole("tab", { name: "Recordings" }).click();
  await page.getByRole("button", { name: "Play recording" }).click();
  await expect(page.locator(".playback-video")).toHaveAttribute(
    "src",
    recording.playback_url!,
  );
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.locator("#stream-key")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Regenerate stream key", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Rename stream" }).click();
  await page.getByLabel("Stream name").fill("Former studio");
  await page.getByRole("button", { name: "Save name" }).click();
  await expect(page.locator(".selected-stream-name")).toContainText(
    "Former studio",
  );
  await page.getByRole("button", { name: /^Stream 1 Offline/ }).click();
  await page.getByLabel("Archived history").selectOption(secondStream.id);
  await expect(
    page.getByRole("heading", { name: "Face 1", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Delete Face 1", exact: true })
    .click();
  await page.getByRole("button", { name: "Delete face", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "No faces yet" }),
  ).toBeVisible();
  expect(
    backend.calls.find(
      (call) => call.method === "DELETE" && call.path.startsWith("/api/faces/"),
    ),
  ).toMatchObject({
    path: `/api/faces/1?stream_id=${secondStream.id}`,
    csrf: "test-csrf",
  });
  expect(
    backend.calls.find(
      (call) =>
        call.method === "DELETE" && call.path.startsWith("/api/streams/"),
    ),
  ).toMatchObject({
    path: `/api/streams/${secondStream.id}`,
    csrf: "test-csrf",
  });
  expect(backend.calls.filter((call) => call.method === "POST")).toHaveLength(
    0,
  );
});

test("archive guards live, recording, unavailable media, and pending publisher server errors", async ({
  page,
}) => {
  const backend = await server(page, {
    streams: [defaultStream, { ...secondStream, online: true }],
  });
  await page.goto("/");
  await page.getByRole("button", { name: /^Studio B Online/ }).click();
  const archive = page.getByRole("button", {
    name: "Archive stream",
    exact: true,
  });
  await expect(archive).toBeDisabled();
  backend.status(
    {
      online: false,
      recording: { id: "recording-b", started_at: "2026-09-13T12:00:00Z" },
    },
    secondStream.id,
  );
  await expect(
    page.getByRole("button", { name: /^Studio B Offline.*Recording/ }),
  ).toBeVisible();
  await expect(archive).toBeDisabled();
  backend.status({ recording: null, media_available: false }, secondStream.id);
  await expect(
    page.getByRole("button", { name: /^Studio B Media unavailable/ }),
  ).toBeVisible();
  await expect(archive).toBeDisabled();
  backend.status({ media_available: true }, secondStream.id);
  await expect(archive).toBeEnabled();
  await page.route(`**/api/streams/${secondStream.id}`, (route) =>
    route.request().method() === "DELETE"
      ? route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "A pending publisher prevents archiving.",
          }),
        })
      : route.fallback(),
  );
  await archive.click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Archive stream", exact: true })
    .click();
  await expect(page.getByRole("dialog").getByRole("alert")).toHaveText(
    "A pending publisher prevents archiving.",
  );
  await expect(page.getByText("2 / 4 active", { exact: true })).toBeVisible();
});

test("scope switches clear revealed keys and recording state without stopping other streams", async ({
  page,
}) => {
  const backend = await server(page, {
    faces: [face],
    streams: [
      { ...defaultStream, online: true },
      { ...secondStream, online: true },
    ],
  });
  await page.goto("/");
  await page
    .getByRole("button", { name: "Start recording", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Stop recording", exact: true }),
  ).toBeEnabled();
  await page.getByRole("tab", { name: "Settings" }).click();
  await page
    .getByRole("button", { name: "Reveal stream key for 30 seconds" })
    .click();
  await expect(page.locator("#stream-key")).toHaveAttribute("type", "text");
  await page.getByRole("button", { name: /^Studio B Online/ }).click();
  await expect(page.locator("#stream-key")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "No faces yet" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start recording", exact: true }),
  ).toBeEnabled();
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.locator("#stream-key")).toHaveValue(
    `${secondStream.id}?user=publisher&pass=second-secret`,
  );
  await expect(page.locator("#stream-key")).toHaveAttribute("type", "password");
  await page
    .getByRole("button", { name: "Start recording", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Stop recording", exact: true }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: /^Stream 1 Online.*Recording/ }),
  ).toBeVisible();
  await page.getByRole("button", { name: /^Stream 1 Online/ }).click();
  await expect(
    page.getByRole("heading", { name: "Face 1", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Stop recording", exact: true }),
  ).toBeEnabled();
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.locator("#stream-key")).toHaveValue(settings.stream_key);
  await expect(page.locator("#stream-key")).toHaveAttribute("type", "password");
  expect(
    backend.calls.filter((call) =>
      call.path.startsWith("/api/recordings/stop"),
    ),
  ).toHaveLength(0);
  expect(
    backend.calls
      .filter((call) => call.path.startsWith("/api/recordings/start"))
      .map((call) => call.path),
  ).toEqual([
    "/api/recordings/start?stream_id=stream",
    `/api/recordings/start?stream_id=${secondStream.id}`,
  ]);
});

test("slow old status, settings, and mutation responses cannot populate a newly selected dashboard", async ({
  page,
}) => {
  const backend = await server(page, {
    streams: [{ ...defaultStream, online: true }, secondStream],
  });
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const waiting: string[] = [];
  let delay = false;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (
      delay &&
      url.searchParams.get("stream_id") === "stream" &&
      ["/api/status", "/api/settings", "/api/recordings/start"].includes(
        url.pathname,
      )
    ) {
      waiting.push(url.pathname);
      await held;
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(
          url.pathname === "/api/settings"
            ? { ...settings, stream_key: "OLD-SECRET-MUST-NOT-APPEAR" }
            : url.pathname === "/api/status"
              ? {
                  ...initialStatus,
                  online: true,
                  recording: {
                    id: "old-recording",
                    started_at: "2026-09-13T12:00:00Z",
                  },
                }
              : { id: "old-recording", started_at: "2026-09-13T12:00:00Z" },
        ),
      });
    }
    return route.fallback();
  });
  await page.goto("/");
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.locator("#stream-key")).toHaveValue(settings.stream_key);
  delay = true;
  await expect
    .poll(
      () =>
        waiting.includes("/api/status") && waiting.includes("/api/settings"),
      { timeout: 7000 },
    )
    .toBeTruthy();
  await page
    .getByRole("button", { name: "Start recording", exact: true })
    .click();
  await expect
    .poll(() => waiting.includes("/api/recordings/start"))
    .toBeTruthy();
  await page.getByRole("button", { name: /^Studio B Offline/ }).click();
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.locator("#stream-key")).toHaveValue(
    `${secondStream.id}?user=publisher&pass=second-secret`,
  );
  release();
  await page.waitForTimeout(1200);
  await expect(page.locator("#stream-key")).toHaveValue(
    `${secondStream.id}?user=publisher&pass=second-secret`,
  );
  await expect(page.locator("#stream-key")).toHaveAttribute("type", "password");
  await expect(
    page.getByRole("heading", { name: "Manual recording", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start recording", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("Recording started.", { exact: true }),
  ).toHaveCount(0);
  expect(
    backend.calls.filter((call) =>
      call.path.startsWith("/api/recordings/stop"),
    ),
  ).toHaveLength(0);
});

test("switching streams changes the HLS manifest and disposes old reconnects", async ({
  page,
}) => {
  const backend = await server(page, {
    streams: [
      { ...defaultStream, online: true },
      { ...secondStream, online: true },
    ],
  });
  const manifests = (id: string) =>
    backend.calls.filter(
      (call) => call.path === `/api/streams/${id}/live/index.m3u8`,
    ).length;
  await page.goto("/");
  await expect.poll(() => manifests("stream")).toBeGreaterThan(0);
  await page.getByRole("button", { name: /^Studio B Online/ }).click();
  await expect.poll(() => manifests(secondStream.id)).toBeGreaterThan(0);
  const before = manifests("stream");
  await page.waitForTimeout(5500);
  expect(manifests("stream")).toBe(before);
  expect(
    backend.calls.some((call) => call.path.startsWith("/api/live/")),
  ).toBeFalsy();
});

test("native HLS and its authenticated error probe use the selected manifest URL", async ({
  page,
}) => {
  await page.addInitScript(() => {
    MediaSource.isTypeSupported = () => false;
    HTMLMediaElement.prototype.canPlayType = (type: string) =>
      type === "application/vnd.apple.mpegurl" ? "probably" : "";
  });
  const backend = await server(page, {
    streams: [defaultStream, { ...secondStream, online: true }],
  });
  await page.goto("/");
  await page.getByRole("button", { name: /^Studio B Online/ }).click();
  const manifest = `/api/streams/${secondStream.id}/live/index.m3u8`;
  await expect(page.locator(".live-player video")).toHaveAttribute(
    "src",
    manifest,
  );
  const count = () =>
    backend.calls.filter((call) => call.path === manifest).length;
  const before = count();
  await page.locator(".live-player video").dispatchEvent("error");
  await expect.poll(count).toBeGreaterThan(before);
  await page.getByRole("button", { name: /^Stream 1 Offline/ }).click();
  await expect(page.locator(".live-player video")).not.toHaveAttribute("src");
});

test("four-stream directory stays responsive with long names and polls serially", async ({
  page,
}) => {
  await server(page, {
    streams: [
      defaultStream,
      secondStream,
      { ...secondStream, id: `stream-${"b".repeat(32)}`, name: "C".repeat(64) },
      { ...secondStream, id: `stream-${"c".repeat(32)}`, name: "D".repeat(64) },
    ],
  });
  let active = 0;
  let max = 0;
  let calls = 0;
  await page.route("**/api/streams", async (route) => {
    active++;
    calls++;
    max = Math.max(max, active);
    await new Promise((resolve) => setTimeout(resolve, 2200));
    await route.fallback();
    active--;
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: /^C{64}/ })).toBeVisible();
  await page.getByRole("button", { name: /^C{64}/ }).click();
  for (const width of [1440, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
  }
  await expect.poll(() => calls, { timeout: 10000 }).toBeGreaterThanOrEqual(2);
  expect(max).toBe(1);
});
