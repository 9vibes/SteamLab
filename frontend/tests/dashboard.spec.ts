import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Face, Recording, Settings, Status, Stream } from "../src/api";

// Contract fixtures exist only in tests. The application has no demo-data path.
const initialStatus: Status = {
  can_stop_recording: false,
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
  auto_record: true,
  recording_state: "waiting",
  recording_error: null,
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
  auto_record: true,
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
    autoRecord?: boolean;
  } = {},
) {
  let authenticated = options.authenticated ?? true;
  const initial = {
    ...initialStatus,
    auto_record: options.autoRecord ?? true,
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
          recording_state: (stream.archived_at
            ? "archived"
            : stream.recording
              ? "recording"
              : options.autoRecord === false
                ? "manual"
                : "waiting") as Status["recording_state"],
          analysis: { ...initial.analysis },
        },
        settings: {
          ...settings,
          auto_record: options.autoRecord ?? true,
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
        owned.status.recording_state = "archived";
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
      currentStatus.recording ??= {
        id: "recording-test",
        started_at: "2026-09-13T12:00:00Z",
      };
      currentStatus.recording_state = "recording";
      currentStatus.recording_error = null;
      currentStatus.can_stop_recording = true;
      return json(currentStatus.recording);
    }
    if (path === "/api/recordings/stop") {
      currentStatus.recording = null;
      currentStatus.recording_state = "stopped";
      currentStatus.recording_error = null;
      currentStatus.can_stop_recording = false;
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

async function openDirectory(page: Page) {
  const dialog = page.getByRole("dialog", {
    name: "Signal Directory",
    exact: true,
  });
  if (!(await dialog.isVisible()))
    await page
      .getByRole("button", { name: "Signal Directory", exact: true })
      .click();
  await expect(dialog).toBeVisible();
  return dialog.getByRole("region", { name: "Stream management", exact: true });
}

test("Signal Directory starts closed, replaces the top box, and restores keyboard focus to its associated rail", async ({
  page,
}) => {
  const backend = await server(page, {
    streams: [defaultStream, secondStream],
  });
  await page.goto("/");
  const rail = page.getByRole("button", {
    name: "Signal Directory",
    exact: true,
    includeHidden: true,
  });
  const sidebar = page.getByRole("complementary", {
    name: "Workspace sidebar",
  });
  await expect(
    sidebar.getByRole("button", { name: "Signal Directory", exact: true }),
  ).toHaveCount(1);
  await expect(rail).toHaveAttribute("aria-haspopup", "dialog");
  await expect(rail).toHaveAttribute("aria-expanded", "false");
  await expect(
    page.getByRole("heading", { name: "Broadcast workspace." }),
  ).toBeVisible();
  await expect(page.locator("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("region", {
      name: "Stream management",
      includeHidden: true,
    }),
  ).toHaveCount(0);
  await expect(page.getByRole("main").locator(".stream-shell")).toHaveCount(0);
  for (const name of ["Add stream", "Rename stream", "Archive stream"])
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(
      0,
    );

  await rail.focus();
  await page.keyboard.press("Enter");
  const directory = await openDirectory(page);
  const dialog = page.getByRole("dialog", {
    name: "Signal Directory",
    exact: true,
  });
  await expect(page.locator("dialog")).toHaveCount(1);
  expect(await dialog.evaluate((element) => element.matches(":modal"))).toBe(
    true,
  );
  await expect(rail).toHaveAttribute("aria-expanded", "true");
  const controls = await rail.getAttribute("aria-controls");
  expect(controls).toBeTruthy();
  await expect(dialog).toHaveAttribute("id", controls!);
  await uniqueIds(page);
  await expect(
    directory
      .getByRole("group", { name: "Active streams" })
      .getByRole("button"),
  ).toHaveCount(2);
  const close = dialog.getByRole("button", {
    name: "Close dialog",
    exact: true,
  });
  await expect(close).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  // Native dialogs may tab through browser chrome, but page controls remain inert.
  const background = page.getByRole("button", {
    name: "Multi-view",
    exact: true,
    includeHidden: true,
  });
  await background.evaluate((element: HTMLButtonElement) => element.focus());
  await expect(background).not.toBeFocused();
  await close.focus();
  await expect(close).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator("dialog")).toHaveCount(0);
  await expect(rail).toHaveAttribute("aria-expanded", "false");
  await expect(rail).toBeFocused();

  await page.keyboard.press("Space");
  await expect(dialog).toHaveAttribute("id", controls!);
  await close.click();
  await expect(page.locator("dialog")).toHaveCount(0);
  await expect(rail).toBeFocused();
  await (await openDirectory(page))
    .getByRole("button", { name: /^Stream 1 Offline/ })
    .click();
  await expect(page.locator("dialog")).toHaveCount(0);
  await expect(rail).toBeFocused();
  await expect(
    page.getByRole("region", { name: "Stream 1 workspace", exact: true }),
  ).toBeVisible();
  expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
});

test("closed directory keeps polling, updates the rail count, and exposes directory errors globally", async ({
  page,
}) => {
  const backend = await server(page, {
    streams: [defaultStream, secondStream],
  });
  let activeCount = 2;
  let unavailable = false;
  let polls = 0;
  await page.route("**/api/streams", (route) => {
    polls++;
    return route.fulfill({
      status: unavailable ? 503 : 200,
      contentType: "application/json",
      body: JSON.stringify(
        unavailable
          ? { detail: "Directory fixture unavailable" }
          : {
              items: [
                defaultStream,
                {
                  ...secondStream,
                  archived_at:
                    activeCount === 1 ? "2026-09-13T13:00:00Z" : null,
                },
              ],
              max_streams: 4,
              active_count: activeCount,
            },
      ),
    });
  });
  await page.goto("/");
  const rail = page.getByRole("button", {
    name: "Signal Directory",
    exact: true,
  });
  await expect(rail).toContainText("2/4");
  const before = polls;
  activeCount = 1;
  await expect(rail).toContainText("1/4", { timeout: 7000 });
  expect(polls).toBeGreaterThan(before);
  await expect(rail).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("dialog")).toHaveCount(0);

  unavailable = true;
  const alert = page.getByRole("main").getByRole("alert");
  await expect(alert).toContainText(
    "Stream directory unavailable. Retrying automatically. Directory fixture unavailable",
    { timeout: 7000 },
  );
  await expect(alert).toBeVisible();
  await expect(rail).toContainText("!");
  await expect(rail).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "No faces yet" }),
  ).toBeVisible();

  const directory = await openDirectory(page);
  await expect(directory.getByRole("alert")).toContainText(
    "Directory fixture unavailable",
  );
  await expect(
    directory.getByRole("button", { name: "Add stream", exact: true }),
  ).toBeDisabled();
  await expect(
    directory.getByRole("button", { name: /^Stream 1 Status unavailable/ }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(alert).toBeVisible();
  unavailable = false;
  activeCount = 2;
  await expect(rail).toContainText("2/4", { timeout: 7000 });
  await expect(alert).toHaveCount(0);
  await expect(page.locator("dialog")).toHaveCount(0);
  expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
});

test("management forms replace the drawer, focus their controls, and cancel back to the unchanged workspace and rail", async ({
  page,
}) => {
  const backend = await server(page, {
    streams: [defaultStream, secondStream],
  });
  await page.goto("/");
  await (await openDirectory(page))
    .getByRole("button", { name: /^Studio B Offline/ })
    .click();
  await page.getByRole("tab", { name: "Settings" }).click();
  await page
    .getByRole("button", { name: "Reveal stream key for 30 seconds" })
    .click();
  const key = page.getByLabel("Stream key SECRET", { exact: true });
  await expect(key).toHaveAttribute("type", "text");
  const input = await key.elementHandle();
  for (const [action, title] of [
    ["Add stream", "Add stream"],
    ["Rename stream", "Rename stream / Studio B"],
    ["Archive stream", "Archive stream? / Studio B"],
  ]) {
    await (await openDirectory(page))
      .getByRole("button", { name: action, exact: true })
      .click();
    const form = page.getByRole("dialog", { name: title, exact: true });
    await expect(form).toBeVisible();
    await expect(page.locator("dialog")).toHaveCount(1);
    await expect(
      page.getByRole("dialog", { name: "Signal Directory", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", {
        name: "Signal Directory",
        exact: true,
        includeHidden: true,
      }),
    ).toHaveAttribute("aria-expanded", "false");
    if (action === "Archive stream") {
      await expect(
        form.getByRole("button", { name: "Close dialog", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(
        form.getByRole("button", { name: "Cancel", exact: true }),
      ).toBeFocused();
    } else {
      await expect(form.getByLabel("Stream name")).toBeFocused();
      await expect(form.getByLabel("Stream name")).toHaveValue(
        action === "Add stream" ? "" : "Studio B",
      );
      await form.getByLabel("Stream name").fill("Unsaved name");
    }
    await form.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(page.locator("dialog")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Signal Directory", exact: true }),
    ).toBeFocused();
    await expect(page.getByRole("tab", { name: "Settings" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(key).toHaveAttribute("type", "text");
    await expect(key).toHaveValue(
      `${secondStream.id}?user=publisher&pass=second-secret`,
    );
    expect(await input!.evaluate((element) => element.isConnected)).toBe(true);
    await expect(page.locator(".selected-stream-name")).toContainText(
      "Studio B",
    );
  }
  expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
});

for (const width of [320, 390, 801, 1440]) {
  test(`Signal Directory fits ${width}px with scrollable cards, accessible controls, and a compact mobile rail`, async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const backend = await server(page, {
      streams: [
        defaultStream,
        secondStream,
        { ...secondStream, id: "long-c", name: "C".repeat(64) },
        { ...secondStream, id: "long-d", name: "D".repeat(64) },
        {
          ...secondStream,
          id: "archived",
          name: "Former studio",
          archived_at: "2026-09-13T13:00:00Z",
        },
      ],
    });
    await page.setViewportSize({ width, height: 640 });
    await page.goto("/");
    const rail = page.getByRole("button", {
      name: "Signal Directory",
      exact: true,
    });
    await expect(rail).toContainText("4/4");
    const sidebar = (await page
      .getByRole("complementary", { name: "Workspace sidebar" })
      .boundingBox())!;
    const main = (await page.getByRole("main").boundingBox())!;
    if (width <= 800) {
      expect(sidebar.height).toBeLessThanOrEqual(80);
      expect(sidebar.y + sidebar.height).toBeLessThanOrEqual(main.y);
      expect(
        await rail.evaluate(
          (element) => getComputedStyle(element).flexDirection,
        ),
      ).toBe("row");
    } else {
      expect(sidebar.x + sidebar.width).toBeLessThanOrEqual(main.x);
    }
    const directory = await openDirectory(page);
    const dialog = page.getByRole("dialog", {
      name: "Signal Directory",
      exact: true,
    });
    const box = (await dialog.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(box.y + box.height).toBeLessThanOrEqual(640);
    expect(
      await dialog.evaluate(
        (element) => element.scrollWidth <= element.clientWidth,
      ),
    ).toBe(true);
    expect(
      await dialog.evaluate(
        (element) => element.scrollHeight > element.clientHeight,
      ),
    ).toBe(true);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await expect(
      directory
        .getByRole("group", { name: "Active streams" })
        .getByRole("button"),
    ).toHaveCount(4);
    if (width === 1440 || width === 390)
      await page.screenshot({
        path: test.info().outputPath(`signal-directory-${width}.png`),
        fullPage: true,
      });

    for (const control of await dialog
      .getByRole("button")
      .or(dialog.getByRole("combobox"))
      .all()) {
      await control.scrollIntoViewIfNeeded();
      await expect(control).toBeInViewport();
      const bounds = (await control.boundingBox())!;
      expect(bounds.x).toBeGreaterThanOrEqual(box.x);
      expect(bounds.x + bounds.width).toBeLessThanOrEqual(box.x + box.width);
      if (await control.isEnabled()) {
        await control.focus();
        await expect(control).toBeFocused();
        await control.click({ trial: true });
      }
    }
    expect(
      await dialog.evaluate((element) => element.scrollTop),
    ).toBeGreaterThan(0);
    await dialog
      .getByRole("button", { name: "Close dialog", exact: true })
      .click();
    await expect(page.locator("dialog")).toHaveCount(0);
    await expect(rail).toBeFocused();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
    expect(errors).toEqual([]);
  });
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
    (await page.getByRole("tab", { name: "Recordings" }).getAttribute("id"))!,
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
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "password");
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.getByRole("button", { name: "Copy entire stream key" }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    settings.stream_key,
  );
  await page
    .getByRole("button", { name: "Reveal stream key for 30 seconds" })
    .click();
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "text");
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue(settings.stream_key);
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
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue("stream?user=publisher&pass=regenerated");
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "password");
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

test("recording starts and stops, then disables when stream goes offline", async ({
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
    page.getByRole("heading", { name: "Recording", exact: true }),
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
    page.getByRole("dialog", { name: "Recording playback / Stream 1" }),
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
    await (await openDirectory(page))
      .getByRole("button", { name: "Add stream", exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: "Add stream" });
    await expect(dialog.getByLabel("Stream name")).toBeFocused();
    await dialog.getByLabel("Stream name").fill(`  ${name}  `);
    await dialog
      .getByRole("button", { name: "Add stream", exact: true })
      .click();
    await expect(dialog).not.toBeVisible();
    await expect(
      page.getByRole("button", { name: "Signal Directory", exact: true }),
    ).toBeFocused();
    await expect(page.locator(".selected-stream-name")).toContainText(name);
    await expect(
      (await openDirectory(page)).getByRole("button", {
        name: new RegExp(`^${name} Offline`),
      }),
    ).toHaveAttribute("aria-pressed", "true");
  }
  const directory = await openDirectory(page);
  await expect(
    directory.getByText("4 / 4 active", { exact: true }),
  ).toBeVisible();
  await expect(
    directory.getByRole("button", { name: "Add stream", exact: true }),
  ).toBeDisabled();
  await expect(
    directory.getByText("All 4 active slots are in use.", { exact: false }),
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
  const add = (await openDirectory(page)).getByRole("button", {
    name: "Add stream",
    exact: true,
  });
  await add.click();
  const dialog = page.getByRole("dialog", { name: "Add stream", exact: true });
  await dialog.getByLabel("Stream name").fill("Another studio");
  await dialog.getByRole("button", { name: "Add stream", exact: true }).click();
  await expect(dialog.getByRole("alert")).toHaveText(
    "At most four active streams are allowed.",
  );
  await expect(dialog.getByLabel("Stream name")).toHaveValue("Another studio");
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Signal Directory", exact: true }),
  ).toBeFocused();
});

test("rename preserves the default stream identity, credentials, and archive guard", async ({
  page,
}) => {
  const backend = await server(page);
  await page.goto("/");
  await expect(
    (await openDirectory(page)).getByRole("button", {
      name: "Archive stream",
      exact: true,
    }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue(settings.stream_key);
  await (await openDirectory(page))
    .getByRole("button", { name: "Rename stream" })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Rename stream / Stream 1",
    exact: true,
  });
  await expect(dialog.getByLabel("Stream name")).toHaveValue("Stream 1");
  await expect(dialog.getByLabel("Stream name")).toHaveAttribute(
    "maxlength",
    "64",
  );
  await dialog.getByLabel("Stream name").fill("Main stage");
  await dialog.getByRole("button", { name: "Save name" }).click();
  await expect(
    page.getByRole("button", { name: "Signal Directory", exact: true }),
  ).toBeFocused();
  await expect(page.locator(".preview-panel .panel-heading")).toContainText(
    "Main stage",
  );
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue(settings.stream_key);
  await expect(page.getByRole("tab", { name: "Settings" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    (await openDirectory(page)).getByRole("button", {
      name: "Archive stream",
      exact: true,
    }),
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
  await (await openDirectory(page))
    .getByRole("button", { name: /^Studio B Offline/ })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await (await openDirectory(page))
    .getByRole("button", { name: "Archive stream", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Archive stream? / Studio B",
    exact: true,
  });
  await expect(dialog).toContainText(
    "Faces, sessions, and recordings are preserved",
  );
  await expect(dialog).toContainText("This cannot be undone");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Signal Directory", exact: true }),
  ).toBeFocused();
  expect(backend.calls.filter((call) => call.method === "DELETE")).toHaveLength(
    0,
  );
  await (await openDirectory(page))
    .getByRole("button", { name: "Archive stream", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "Archive stream", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Signal Directory", exact: true }),
  ).toBeFocused();
  await expect(
    page.getByText("Preserved history mode.", { exact: false }),
  ).toBeVisible();
  const directory = await openDirectory(page);
  await expect(
    directory.getByText("1 / 4 active", { exact: true }),
  ).toBeVisible();
  await expect(
    directory.getByRole("combobox", { name: "Archived history" }),
  ).toHaveValue(secondStream.id);
  await page.keyboard.press("Escape");
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
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Regenerate stream key", exact: true }),
  ).toBeDisabled();
  await (await openDirectory(page))
    .getByRole("button", { name: "Rename stream" })
    .click();
  const rename = page.getByRole("dialog", {
    name: "Rename stream / Studio B",
    exact: true,
  });
  await rename.getByLabel("Stream name").fill("Former studio");
  await rename.getByRole("button", { name: "Save name" }).click();
  await expect(page.locator(".selected-stream-name")).toContainText(
    "Former studio",
  );
  await (await openDirectory(page))
    .getByRole("button", { name: /^Stream 1 Offline/ })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await (await openDirectory(page))
    .getByRole("combobox", { name: "Archived history" })
    .selectOption(secondStream.id);
  await expect(page.getByRole("dialog")).toHaveCount(0);
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
  await (await openDirectory(page))
    .getByRole("button", { name: /^Studio B Online/ })
    .click();
  const directory = await openDirectory(page);
  const archive = directory.getByRole("button", {
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
    directory.getByRole("button", { name: /^Studio B Offline.*Recording/ }),
  ).toBeVisible();
  await expect(archive).toBeDisabled();
  backend.status({ recording: null, media_available: false }, secondStream.id);
  await expect(
    directory.getByRole("button", { name: /^Studio B Media unavailable/ }),
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
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Cancel", exact: true })
    .click();
  await expect(
    (await openDirectory(page)).getByText("2 / 4 active", { exact: true }),
  ).toBeVisible();
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
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "text");
  await (await openDirectory(page))
    .getByRole("button", { name: /^Studio B Online/ })
    .click();
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "No faces yet" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start recording", exact: true }),
  ).toBeEnabled();
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue(`${secondStream.id}?user=publisher&pass=second-secret`);
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "password");
  await page
    .getByRole("button", { name: "Start recording", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Stop recording", exact: true }),
  ).toBeEnabled();
  await expect(
    (await openDirectory(page)).getByRole("button", {
      name: /^Stream 1 Online.*Recording/,
    }),
  ).toBeVisible();
  await (await openDirectory(page))
    .getByRole("button", { name: /^Stream 1 Online/ })
    .click();
  await expect(
    page.getByRole("heading", { name: "Face 1", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Stop recording", exact: true }),
  ).toBeEnabled();
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue(settings.stream_key);
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "password");
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
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue(settings.stream_key);
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
  await (await openDirectory(page))
    .getByRole("button", { name: /^Studio B Offline/ })
    .click();
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue(`${secondStream.id}?user=publisher&pass=second-secret`);
  release();
  await page.waitForTimeout(1200);
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue(`${secondStream.id}?user=publisher&pass=second-secret`);
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "password");
  await expect(
    page.getByRole("heading", { name: "Recording", exact: true }),
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
  await (await openDirectory(page))
    .getByRole("button", { name: /^Studio B Online/ })
    .click();
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
  await (await openDirectory(page))
    .getByRole("button", { name: /^Studio B Online/ })
    .click();
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
  await (await openDirectory(page))
    .getByRole("button", { name: /^Stream 1 Offline/ })
    .click();
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
  const directory = await openDirectory(page);
  await expect(directory.getByRole("button", { name: /^C{64}/ })).toBeVisible();
  await directory.getByRole("button", { name: /^C{64}/ }).click();
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

const liveStreams: Stream[] = [
  defaultStream,
  secondStream,
  { ...secondStream, id: `stream-${"b".repeat(32)}`, name: "Studio C" },
  { ...secondStream, id: `stream-${"c".repeat(32)}`, name: "Studio D" },
].map((stream) => ({ ...stream, online: true }));

test("multi-view release gallery shows four independently recording feeds", async ({
  page,
}) => {
  const streams = liveStreams.map((stream, index) => ({
    ...stream,
    name: `Studio ${String.fromCharCode(65 + index)}`,
    recording: {
      id: `gallery-recording-${index}`,
      started_at: "2026-09-14T12:00:00Z",
    },
  }));
  const backend = await server(page, { streams });
  for (const [index, stream] of streams.entries()) {
    backend.status(
      {
        can_stop_recording: true,
        session_id: `gallery-session-${index}`,
        started_at: "2026-09-14T12:00:00Z",
        tracks: ["H264", "MPEG-4 Audio"],
        bitrate_mbps: 4 + index / 2,
        bitrate_history: [3.9, 4.1, 4, 4.2, 4.1, 4.0].map(
          (value) => value + index / 2,
        ),
      },
      stream.id,
    );
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/");
  await page.getByRole("button", { name: "Multi-view", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Recording in progress" }),
  ).toHaveCount(4);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    const filename =
      width === 1440
        ? "screenshot-multiview.png"
        : "screenshot-multiview-mobile.png";
    await page.screenshot({
      path: process.env.STEAMLAB_SCREENSHOT_DIR
        ? `${process.env.STEAMLAB_SCREENSHOT_DIR}/${filename}`
        : test.info().outputPath(filename),
      fullPage: true,
    });
  }
  expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
});

async function uniqueIds(page: Page) {
  expect(
    await page.locator("[id]").evaluateAll((elements) => {
      const ids = elements.map((element) => element.id);
      return ids.filter((id, index) => ids.indexOf(id) !== index);
    }),
  ).toEqual([]);
  expect(
    await page
      .locator("label[for]")
      .evaluateAll((labels) =>
        labels.every(
          (label) =>
            document.getElementById(label.getAttribute("for")!) !== null,
        ),
      ),
  ).toBe(true);
  for (const panel of await page.getByRole("tabpanel").all()) {
    const tabId = await panel.getAttribute("aria-labelledby");
    const selectedTab = page
      .getByRole("tab", { selected: true })
      .and(page.locator(`[id="${tabId}"]`));
    await expect(selectedTab).toHaveAttribute(
      "aria-controls",
      (await panel.getAttribute("id"))!,
    );
  }
}

for (const count of [2, 3, 4]) {
  test(`${count} live feeds occupy a two-column grid with each video above its own tools`, async ({
    page,
  }) => {
    const backend = await server(page, {
      streams: liveStreams.slice(0, count),
    });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.goto("/");
    await page.getByRole("button", { name: "Multi-view", exact: true }).click();
    await expect(page.locator(".stream-dashboard")).toHaveCount(count);
    await expect(page.locator(".app-header")).toHaveCount(1);
    await expect(page.locator(".app-footer")).toHaveCount(1);
    await expect(
      page.getByRole("heading", { name: "Broadcast workspace." }),
    ).toHaveCount(1);
    await expect(page.getByRole("main")).toHaveCount(1);
    await expect(
      page.getByRole("region", { name: "Stream management", exact: true }),
    ).toHaveCount(0);
    await expect(await openDirectory(page)).toHaveCount(1);
    await page.keyboard.press("Escape");
    const positions = [];
    for (const stream of liveStreams.slice(0, count)) {
      const tile = page.getByRole("region", {
        name: `${stream.name} workspace`,
        exact: true,
      });
      const video = tile.getByLabel(`${stream.name} live stream preview`, {
        exact: true,
      });
      await expect(video).toBeVisible();
      expect(
        await video.evaluate((element: HTMLVideoElement) => element.muted),
      ).toBe(true);
      const tools = tile.getByRole("region", {
        name: `${stream.name} broadcast tools`,
        exact: true,
      });
      await expect(
        tools.getByRole("tablist", { name: `${stream.name} broadcast tools` }),
      ).toBeVisible();
      const v = (await video.boundingBox())!;
      const t = (await tools.boundingBox())!;
      expect(t.y).toBeGreaterThanOrEqual(v.y + v.height);
      expect(Math.abs(t.x - v.x)).toBeLessThan(2);
      expect(Math.abs(t.width - v.width)).toBeLessThan(3);
      positions.push((await tile.boundingBox())!);
    }
    expect(positions[1].x).toBeGreaterThanOrEqual(
      positions[0].x + positions[0].width,
    );
    expect(positions[1].y).toBe(positions[0].y);
    if (count > 2) {
      expect(positions[2].x).toBe(positions[0].x);
      expect(positions[2].y).toBeGreaterThanOrEqual(
        Math.max(...positions.slice(0, 2).map((box) => box.y + box.height)),
      );
    }
    if (count === 4) {
      expect(positions[3].x).toBe(positions[1].x);
      expect(positions[3].y).toBe(positions[2].y);
    }
    expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
  });
}

test("four tiles keep independent tabs, keyboard focus, credentials, analysis and recording scopes", async ({
  page,
}) => {
  const backend = await server(page, { streams: liveStreams });
  await page.goto("/");
  await (await openDirectory(page))
    .getByRole("button", { name: /^Studio B Online/ })
    .click();
  const studioB = page.getByRole("region", {
    name: "Studio B workspace",
    exact: true,
  });
  await studioB.getByRole("tab", { name: "Settings" }).click();
  await expect(
    studioB.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveValue(`${secondStream.id}?user=publisher&pass=second-secret`);
  await studioB.locator("video").evaluate((video) => {
    video.dataset.retained = "yes";
  });
  await page.getByRole("button", { name: "Multi-view", exact: true }).click();
  await expect(page.locator(".stream-dashboard")).toHaveCount(4);
  await expect(studioB.locator("video")).toHaveAttribute(
    "data-retained",
    "yes",
  );
  await expect(studioB.getByRole("tab", { name: "Settings" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const tiles = liveStreams.map((stream) =>
    page.getByRole("region", { name: `${stream.name} workspace`, exact: true }),
  );
  await tiles[2].getByRole("tab", { name: "Recordings" }).click();
  await tiles[3].getByRole("tab", { name: /^Faces/ }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(tiles[3].getByRole("tab", { name: "Recordings" })).toBeFocused();
  await expect(tiles[0].getByRole("tab", { name: /^Faces/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(tiles[1].getByRole("tab", { name: "Settings" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    tiles[2].getByRole("tab", { name: "Recordings" }),
  ).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(tiles[3].getByRole("tab", { name: "Settings" })).toBeFocused();
  await page.keyboard.press("Home");
  await expect(tiles[3].getByRole("tab", { name: /^Faces/ })).toBeFocused();
  for (const tile of tiles)
    await tile.getByRole("tab", { name: "Settings" }).click();
  for (const [index, tile] of tiles.entries()) {
    await expect(
      tile.getByLabel("Stream key SECRET", { exact: true }),
    ).toHaveValue(
      index === 0
        ? settings.stream_key
        : `${liveStreams[index].id}?user=publisher&pass=second-secret`,
    );
    await expect(
      tile.getByLabel("Stream key SECRET", { exact: true }),
    ).toHaveAttribute("type", "password");
  }
  await tiles[2]
    .getByRole("button", { name: "Reveal stream key for 30 seconds" })
    .click();
  await expect(
    tiles[2].getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "text");
  await expect(
    tiles[1].getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "password");
  await (await openDirectory(page))
    .getByRole("button", { name: /^Studio D Online/ })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    (await openDirectory(page)).getByText("Managing: Studio D.", {
      exact: false,
    }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator(".stream-dashboard")).toHaveCount(4);
  await expect(studioB.locator("video")).toHaveAttribute(
    "data-retained",
    "yes",
  );
  await expect(
    tiles[2].getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveAttribute("type", "text");
  expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);

  for (const [index, tile] of tiles.entries()) {
    await tile.getByRole("tab", { name: /^Faces/ }).click();
    await tile.getByRole("switch", { name: "Enable face analysis" }).click();
    await expect(tile.getByRole("switch")).toBeChecked();
    for (const other of tiles.slice(index + 1)) {
      await other.getByRole("tab", { name: /^Faces/ }).click();
      await expect(other.getByRole("switch")).not.toBeChecked();
    }
    await tile
      .getByRole("button", { name: "Start recording", exact: true })
      .click();
    await expect(
      tile.getByRole("button", { name: "Stop recording", exact: true }),
    ).toBeEnabled();
    for (const other of tiles.slice(index + 1))
      await expect(
        other.getByRole("button", { name: "Start recording", exact: true }),
      ).toBeEnabled();
  }
  for (const tile of [...tiles].reverse()) {
    await tile
      .getByRole("button", { name: "Stop recording", exact: true })
      .click();
    await expect(
      tile.getByText("Stopped for this connection", { exact: true }),
    ).toBeVisible();
  }
  const mutations = backend.calls.filter((call) => call.method === "POST");
  expect(mutations).toHaveLength(12);
  expect(mutations.every((call) => call.csrf === "test-csrf")).toBe(true);
  for (const endpoint of ["analysis", "recordings/start", "recordings/stop"]) {
    expect(
      mutations
        .filter((call) => call.path.startsWith(`/api/${endpoint}?`))
        .map((call) =>
          new URL(call.path, "http://localhost").searchParams.get("stream_id"),
        )
        .sort(),
    ).toEqual(liveStreams.map((stream) => stream.id).sort());
  }
});

test("four dashboards have unique tab, select, credential, SVG and modal IDs", async ({
  page,
}) => {
  const backend = await server(page, { streams: liveStreams, faces: [face] });
  for (const stream of liveStreams)
    backend.status({ bitrate_history: [1, 2, 3] }, stream.id);
  await page.goto("/");
  await page.getByRole("button", { name: "Multi-view", exact: true }).click();
  await expect(page.getByLabel("Session", { exact: true })).toHaveCount(4);
  await expect(page.locator("linearGradient")).toHaveCount(4);
  await uniqueIds(page);
  expect(
    await page.locator(".bitrate-chart polygon").evaluateAll((polygons) =>
      polygons.every((polygon) => {
        const id = polygon.getAttribute("fill")!.slice(5, -1);
        return (
          polygon.closest("svg")!.querySelector("linearGradient")!.id === id
        );
      }),
    ),
  ).toBe(true);
  for (const tab of await page.getByRole("tab", { name: "Settings" }).all())
    await tab.click();
  await expect(
    page.getByLabel("Stream key SECRET", { exact: true }),
  ).toHaveCount(4);
  await uniqueIds(page);
  await openDirectory(page);
  await uniqueIds(page);
  await (await openDirectory(page))
    .getByRole("button", { name: "Rename stream", exact: true })
    .click();
  const rename = page.getByRole("dialog", {
    name: "Rename stream / Stream 1",
    exact: true,
  });
  await expect(rename).toBeVisible();
  await uniqueIds(page);
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Signal Directory", exact: true }),
  ).toBeFocused();
  for (const tab of await page.getByRole("tab", { name: "Recordings" }).all())
    await tab.click();
  await uniqueIds(page);
  for (const tab of await page.getByRole("tab", { name: /^Faces/ }).all())
    await tab.click();
  const clear = page
    .getByRole("region", { name: "Stream 1 workspace", exact: true })
    .getByRole("button", { name: "Clear all", exact: true });
  await clear.click();
  const dialog = page.getByRole("dialog", {
    name: "Clear the entire face catalog? / Stream 1",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await uniqueIds(page);
  await page.keyboard.press("Escape");
  await expect(clear).toBeFocused();
  expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
});

test("multi-view excludes offline and archived streams, caps at four and opens history in single view", async ({
  page,
}) => {
  const archived = {
    ...secondStream,
    id: "archived",
    name: "Former studio",
    archived_at: "2026-09-13T13:00:00Z",
    online: true,
  };
  await server(page, {
    streams: [
      ...liveStreams,
      { ...secondStream, id: "offline", name: "Offline studio" },
      archived,
      { ...secondStream, id: "extra", name: "Extra live", online: true },
    ],
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Multi-view", exact: true }).click();
  await expect(page.locator(".stream-dashboard")).toHaveCount(4);
  for (const name of ["Former studio", "Offline studio", "Extra live"])
    await expect(
      page.getByRole("region", { name: `${name} workspace`, exact: true }),
    ).toHaveCount(0);
  await (await openDirectory(page))
    .getByRole("combobox", { name: "Archived history", exact: true })
    .selectOption("archived");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Single view", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".stream-dashboard")).toHaveCount(1);
  await expect(
    page.getByRole("region", { name: "Former studio workspace", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".live-player video")).toHaveCount(0);
  await expect(
    page.getByText("Archived history", { exact: true }).last(),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start recording", exact: true }),
  ).toBeDisabled();
});

test("no connected streams offers connection guidance and a single-view action", async ({
  page,
}) => {
  const backend = await server(page, {
    streams: [defaultStream, secondStream],
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Multi-view", exact: true }).click();
  const empty = page.getByRole("region", {
    name: "No connected streams",
    exact: true,
  });
  await expect(empty).toContainText(
    "RTMP credentials in Single view / Settings",
  );
  await expect(page.locator(".stream-dashboard")).toHaveCount(0);
  await expect(page.locator("video")).toHaveCount(0);
  backend.status({ online: true }, secondStream.id);
  await expect(
    page.getByRole("region", { name: "Studio B workspace", exact: true }),
  ).toBeVisible();
  await expect(empty).toHaveCount(0);
  backend.status({ online: false }, secondStream.id);
  await expect(empty).toBeVisible();
  await empty
    .getByRole("button", { name: "Switch to single view", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Stream 1 workspace", exact: true }),
  ).toBeVisible();
  expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
});

for (const engine of ["native", "HLS.js"]) {
  test(`${engine} players are retained for shared feeds and cleaned up on mode and online changes`, async ({
    page,
  }) => {
    if (engine === "native")
      await page.addInitScript(() => {
        MediaSource.isTypeSupported = () => false;
        HTMLMediaElement.prototype.canPlayType = (type: string) =>
          type === "application/vnd.apple.mpegurl" ? "probably" : "";
      });
    const backend = await server(page, { streams: liveStreams });
    const manifests = (id: string) =>
      backend.calls.filter(
        (call) => call.path === `/api/streams/${id}/live/index.m3u8`,
      ).length;
    await page.goto("/");
    await page.getByRole("button", { name: "Multi-view", exact: true }).click();
    for (const stream of liveStreams)
      await expect.poll(() => manifests(stream.id)).toBeGreaterThan(0);
    const players = await page.locator(".live-player video").elementHandles();
    await players[0].evaluate((video) => {
      (video as HTMLVideoElement).dataset.retained = "yes";
    });
    for (const close of ["Escape", "Close dialog"]) {
      await openDirectory(page);
      for (const player of players)
        expect(await player.evaluate((video) => video.isConnected)).toBe(true);
      if (close === "Escape") await page.keyboard.press("Escape");
      else
        await page
          .getByRole("dialog", { name: "Signal Directory", exact: true })
          .getByRole("button", { name: close, exact: true })
          .click();
      await expect(page.locator("dialog")).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "Signal Directory", exact: true }),
      ).toBeFocused();
      await expect(page.locator(".live-player video")).toHaveCount(4);
      for (const [index, player] of players.entries())
        expect(
          await player.evaluate(
            (video, index) =>
              video === document.querySelectorAll(".live-player video")[index],
            index,
          ),
        ).toBe(true);
    }
    expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
    await page
      .getByRole("button", { name: "Single view", exact: true })
      .click();
    await expect(page.locator(".live-player video")).toHaveCount(1);
    await expect(page.locator(".live-player video")).toHaveAttribute(
      "data-retained",
      "yes",
    );
    const after = liveStreams.map((stream) => manifests(stream.id));
    for (const player of players.slice(1)) {
      expect(
        await player.evaluate((node) => {
          const video = node as HTMLVideoElement;
          return {
            connected: video.isConnected,
            src: video.getAttribute("src"),
            paused: video.paused,
          };
        }),
      ).toEqual({ connected: false, src: null, paused: true });
      await player.evaluate((video) => {
        video.dispatchEvent(new Event("error"));
        video.dispatchEvent(new Event("loadedmetadata"));
      });
    }
    await page.waitForTimeout(5500);
    for (const [index, stream] of liveStreams.entries())
      if (index) expect(manifests(stream.id)).toBe(after[index]);
    await page.getByRole("button", { name: "Multi-view", exact: true }).click();
    await expect(page.locator(".live-player video")).toHaveCount(4);
    await expect
      .poll(() => manifests(secondStream.id))
      .toBeGreaterThan(after[1]);
    const removed = await page
      .getByLabel("Studio B live stream preview", { exact: true })
      .elementHandle();
    backend.status({ online: false }, secondStream.id);
    await expect(
      page.getByRole("region", { name: "Studio B workspace", exact: true }),
    ).toHaveCount(0);
    expect(
      await removed!.evaluate((video: HTMLVideoElement) => ({
        connected: video.isConnected,
        src: video.getAttribute("src"),
        paused: video.paused,
      })),
    ).toEqual({ connected: false, src: null, paused: true });
    const offline = manifests(secondStream.id);
    await removed!.evaluate((video) => video.dispatchEvent(new Event("error")));
    await page.waitForTimeout(5500);
    expect(manifests(secondStream.id)).toBe(offline);
    await expect(
      page.getByLabel("Stream 1 live stream preview", { exact: true }),
    ).toHaveAttribute("data-retained", "yes");
    expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
  });
}

test("recording state and server policy explain storage recovery, stop latches and manual retries", async ({
  page,
}) => {
  const backend = await server(page, { online: true });
  await page.goto("/");
  const controls = page.getByRole("region", {
    name: "Stream 1 recording controls",
    exact: true,
  });
  await expect(controls).toContainText(
    "Automatic recording is on (server default)",
  );
  await expect(controls).toContainText("even with no viewers");
  await expect(controls).toContainText("5 continuous seconds");
  await expect(controls).toContainText(
    "Stop holds for this publisher connection",
  );
  await expect(controls).toContainText(
    "Recorder failures require Start or a new connection",
  );
  await expect(controls).toContainText("Waiting for a connection");
  backend.status({ recording_state: "disk_paused", disk_free_bytes: 1 });
  await expect(controls).toContainText("Paused for low storage");
  await expect(
    controls.getByRole("button", { name: "Start recording", exact: true }),
  ).toBeDisabled();
  await expect(
    controls.getByRole("button", { name: "Stop recording", exact: true }),
  ).toBeEnabled();
  expect(backend.calls.filter((call) => call.method !== "GET")).toEqual([]);
  await controls
    .getByRole("button", { name: "Stop recording", exact: true })
    .click();
  await expect(controls).toContainText("Stopped for this connection");
  backend.status({
    recording_state: "error",
    recording_error: "Recorder exited unexpectedly.",
    disk_free_bytes: initialStatus.disk_free_bytes,
  });
  await expect(controls).toContainText("Recording failed");
  await expect(controls.getByRole("alert")).toHaveText(
    "Recorder exited unexpectedly.",
  );
  await controls
    .getByRole("button", { name: "Start recording", exact: true })
    .click();
  await expect(
    controls.getByRole("heading", { name: "Recording in progress" }),
  ).toBeVisible();
  await expect(controls.getByRole("alert")).toHaveCount(0);
  expect(
    backend.calls
      .filter((call) => call.method === "POST")
      .map((call) => call.path),
  ).toEqual([
    "/api/recordings/stop?stream_id=stream",
    "/api/recordings/start?stream_id=stream",
  ]);
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(
    page.getByText("Automatic recording is on (AUTO_RECORD=true", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Face analysis remains opt-in.", { exact: false }),
  ).toBeVisible();
});

test("single view can suppress automatic recording during a media server outage", async ({
  page,
}) => {
  const backend = await server(page, { online: true });
  await page.goto("/");
  const controls = page.getByRole("region", {
    name: "Stream 1 recording controls",
    exact: true,
  });
  await expect(controls).toBeVisible();
  backend.status({
    online: false,
    media_available: false,
    recording: null,
    recording_state: "waiting",
    can_stop_recording: false,
  });
  await expect(
    controls.getByRole("button", { name: "Start recording", exact: true }),
  ).toBeDisabled();
  await expect(
    controls.getByRole("button", { name: "Stop recording", exact: true }),
  ).toHaveCount(0);
  backend.status({ can_stop_recording: true });
  await expect(controls).toContainText("Stop keeps this publisher stopped");
  const stop = controls.getByRole("button", {
    name: "Stop recording",
    exact: true,
  });
  await expect(stop).toBeEnabled();
  await stop.click();
  await expect(controls).toContainText("Stopped for this connection");
  await expect(stop).toHaveCount(0);
  backend.status({ online: true, media_available: true });
  await expect(
    controls.getByRole("button", { name: "Start recording", exact: true }),
  ).toBeEnabled();
  expect(
    backend.calls
      .filter((call) => call.method === "POST")
      .map((call) => call.path),
  ).toEqual(["/api/recordings/stop?stream_id=stream"]);
});

test("manual-only server policy is read-only and idempotent Start accepts an automatic race response", async ({
  page,
}) => {
  const backend = await server(page, { online: true, autoRecord: false });
  await page.goto("/");
  await expect(
    page.getByText("Manual-only policy", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Automatic recording is off. Start records this stream; reconnecting requires Start again.",
      { exact: true },
    ),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(
    page.getByText("Automatic recording is off (AUTO_RECORD=false)", {
      exact: false,
    }),
  ).toBeVisible();
  // The monitor wins after the last status response but before the user's Start.
  const existing = {
    id: "already-recording",
    started_at: "2026-09-13T12:00:00Z",
  };
  await page.route("**/api/status?stream_id=stream", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        ...initialStatus,
        online: true,
        auto_record: false,
        recording_state: "manual",
      }),
    }),
  );
  backend.status({
    recording: existing,
    recording_state: "recording",
    auto_record: true,
  });
  const response = page.waitForResponse((response) =>
    response.url().includes("/api/recordings/start?stream_id=stream"),
  );
  await page
    .getByRole("button", { name: "Start recording", exact: true })
    .click();
  expect((await response).status()).toBe(200);
  expect(await (await response).json()).toEqual(existing);
  await expect(
    page.getByRole("button", { name: "Stop recording", exact: true }),
  ).toBeEnabled();
  expect(backend.calls.filter((call) => call.method === "POST")).toHaveLength(
    1,
  );
});

test("four-feed layouts and all tools fit desktop and mobile without horizontal overflow", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const streams = liveStreams.map((stream, index) => ({
    ...stream,
    name: String.fromCharCode(65 + index).repeat(64),
  }));
  await server(page, { streams, faces: [face] });
  await page.goto("/");
  await page.getByRole("button", { name: "Multi-view", exact: true }).click();
  await expect(page.locator(".stream-dashboard")).toHaveCount(4);
  for (const width of [1440, 1024, 801, 800, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const name of [/^Faces/, /^Recordings$/, /^Settings$/]) {
      for (const tab of await page.getByRole("tab", { name }).all())
        await tab.click();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
        `page overflow at ${width}, ${name}`,
      ).toBe(true);
      expect(
        await page
          .locator(".tab-panel")
          .evaluateAll((panels) =>
            panels.every((panel) => panel.scrollWidth <= panel.clientWidth),
          ),
        `tools overflow at ${width}, ${name}`,
      ).toBe(true);
    }
    const positions = await page
      .locator(".stream-dashboard")
      .evaluateAll((tiles) =>
        tiles.map((tile) => {
          const { x, y, width, height } = tile.getBoundingClientRect();
          return { x, y, width, height };
        }),
      );
    if (width <= 800) {
      for (const [index, position] of positions.entries()) {
        expect(position.x).toBe(positions[0].x);
        if (index)
          expect(position.y).toBeGreaterThanOrEqual(
            positions[index - 1].y + positions[index - 1].height,
          );
      }
    } else {
      expect(positions[1].x).toBeGreaterThanOrEqual(
        positions[0].x + positions[0].width,
      );
      expect(positions[1].y).toBe(positions[0].y);
      expect(positions[2].x).toBe(positions[0].x);
      expect(positions[3].y).toBe(positions[2].y);
    }
    if (width === 1440 || width === 390)
      await page.screenshot({
        path: test.info().outputPath(`multi-view-${width}.png`),
        fullPage: true,
      });
  }
  expect(errors).toEqual([]);
});
