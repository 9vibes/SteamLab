import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, ReactNode } from "react";
import {
  api,
  ApiError,
  bytes,
  duration,
  message,
  scopeRequest,
  timestamp,
} from "./api";
import type {
  Face,
  Recording,
  Request,
  Session,
  Settings,
  Status,
  Stream,
  StreamList,
} from "./api";
import LivePlayer from "./LivePlayer";
import { Brand, Empty, Icon, Modal } from "./ui";

const PAGE_SIZE = 12;
const tabs = ["faces", "recordings", "settings"] as const;
type Tab = (typeof tabs)[number];
type Catalog = {
  key: string;
  faces: { items: Face[]; total: number };
  sessions: Session[];
  recordings: Recording[];
  settings: Settings;
};
type Confirmation = {
  title: string;
  body: string;
  label: string;
  action: () => Promise<void>;
};

// Each loop schedules only after its request settles; slow servers never accumulate polls.
function useSerialPoll(
  task: (signal: AbortSignal) => Promise<void>,
  interval: number,
) {
  const latest = useRef(task);
  useEffect(() => {
    latest.current = task;
  }, [task]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const run = async () => {
      try {
        await latest.current(controller.signal);
      } catch {
        /* The task owns user-facing errors. */
      }
      if (!controller.signal.aborted)
        timer = setTimeout(() => {
          void run();
        }, interval);
    };
    void run();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [interval]);
}

export default function App() {
  const [csrf, setCsrf] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [authError, setAuthError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [expired, setExpired] = useState(false);
  const [unauthorized] = useState(() => () => {
    setCsrf(null);
    setExpired(true);
  });

  useEffect(() => {
    const controller = new AbortController();
    setChecking(true);
    setAuthError("");
    void api<{ csrf_token: string }>("/api/auth/me", {
      signal: controller.signal,
    })
      .then((data) => {
        if (!controller.signal.aborted) setCsrf(data.csrf_token);
      })
      .catch((error) => {
        if (
          !controller.signal.aborted &&
          !(error instanceof ApiError && error.status === 401)
        )
          setAuthError(message(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setChecking(false);
      });
    return () => controller.abort();
  }, [attempt]);

  if (checking)
    return (
      <div className="auth-shell">
        <Brand />
        <div className="auth-loading" role="status">
          <span className="spinner" />
          Checking your session
        </div>
      </div>
    );
  if (csrf)
    return (
      <StreamWorkspace
        key={csrf}
        csrf={csrf}
        onUnauthorized={unauthorized}
        onLogout={() => {
          setCsrf(null);
          setExpired(false);
        }}
      />
    );
  return (
    <Login
      expired={expired}
      error={authError}
      onRetry={() => setAttempt((value) => value + 1)}
      onLogin={(token) => {
        setCsrf(token);
        setExpired(false);
      }}
    />
  );
}

function Login({
  expired,
  error,
  onRetry,
  onLogin,
}: {
  expired: boolean;
  error: string;
  onRetry: () => void;
  onLogin: (token: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [loginError, setLoginError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setLoginError("");
    try {
      const result = await api<{ csrf_token: string }>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ password }),
      });
      setPassword("");
      onLogin(result.csrf_token);
    } catch (error) {
      setLoginError(message(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="auth-shell">
      <header className="auth-brand">
        <Brand />
        <span className="secure-label">
          <Icon name="lock" size={14} />
          PRIVATE WORKSPACE
        </span>
      </header>
      <div className="auth-layout">
        <section className="auth-intro">
          <span className="eyebrow amber">YOUR SIGNAL. IN FOCUS.</span>
          <h1>
            A clear view.
            <br />
            Complete <span>control.</span>
          </h1>
          <p>
            Your live feed, recording library, and face intelligence. One
            private broadcast workspace.
          </p>
          <div className="auth-features">
            <span>
              <Icon name="signal" />
              Live monitoring
            </span>
            <span>
              <Icon name="faces" />
              Face analysis
            </span>
            <span>
              <Icon name="video" />
              Local recordings
            </span>
          </div>
          <div className="signal-art" aria-hidden="true">
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
            <div />
          </div>
        </section>
        <section className="login-card">
          <span className="section-number">01 / ACCESS</span>
          <div className="login-icon">
            <Icon name="lock" size={24} />
          </div>
          <h2>Enter the control room</h2>
          <p>Sign in with your administrator password.</p>
          {expired && (
            <div className="notice">
              Your session has expired. Sign in to reconnect.
            </div>
          )}
          {error && (
            <div className="notice danger" role="alert">
              Cannot reach the console: {error}
              <button className="text-button" onClick={onRetry}>
                Check connection again
              </button>
            </div>
          )}
          <form
            onSubmit={(event) => {
              void submit(event);
            }}
          >
            <label htmlFor="password">Administrator password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              autoFocus
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="Enter your password"
              disabled={busy}
            />
            {loginError && (
              <p className="field-error" role="alert">
                {loginError}
              </p>
            )}
            <button
              className="button primary login-submit"
              type="submit"
              disabled={busy || !password}
            >
              {busy ? <span className="spinner" /> : <Icon name="arrow" />}
              {busy ? "Signing in..." : "Open console"}
            </button>
          </form>
          <div className="login-foot">
            <Icon name="lock" size={13} />
            <span>Private by design. Authenticated access only.</span>
          </div>
        </section>
      </div>
      <footer className="auth-footer">
        <span>KUNAS/Labs / BROADCAST OPERATIONS</span>
        <span>Live locally. Stay in control.</span>
      </footer>
    </main>
  );
}

function StreamWorkspace({
  csrf,
  onUnauthorized,
  onLogout,
}: {
  csrf: string;
  onUnauthorized: () => void;
  onLogout: () => void;
}) {
  const [request] = useState<Request>(
    () =>
      async <T,>(path: string, init?: RequestInit) => {
        try {
          return await api<T>(path, init, csrf);
        } catch (error) {
          if (error instanceof ApiError && error.status === 401)
            onUnauthorized();
          throw error;
        }
      },
  );
  const [streams, setStreams] = useState<StreamList | null>(null);
  const [selectedId, setSelectedId] = useState("stream");
  const [multiView, setMultiView] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState<{
    kind: "add" | "rename" | "archive";
    stream?: Stream;
  } | null>(null);
  const [name, setName] = useState("");
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const version = useRef(0);
  useSerialPoll(async (signal) => {
    const revision = version.current;
    try {
      const result = await request<StreamList>("/api/streams", { signal });
      if (!signal.aborted && revision === version.current && !busyRef.current) {
        setStreams(result);
        setError("");
      }
    } catch (error) {
      if (!signal.aborted && revision === version.current)
        setError(message(error));
    }
  }, 2000);

  const selected = streams?.items.find((item) => item.id === selectedId);
  const visibleStreams = multiView
    ? (streams?.items
        .filter((item) => !item.archived_at && item.online)
        .slice(0, 4) ?? [])
    : selected
      ? [selected]
      : [];
  const limit = Math.min(4, streams?.max_streams ?? 4);
  const atLimit = !streams || streams.active_count >= limit;
  const archiveBlocked =
    !selected ||
    selected.is_default ||
    !!selected.archived_at ||
    selected.online ||
    !!selected.recording ||
    !selected.media_available ||
    !!error;
  function open(kind: "add" | "rename" | "archive") {
    setForm({ kind, stream: kind === "add" ? undefined : selected });
    setName(kind === "rename" ? (selected?.name ?? "") : "");
    setFormError("");
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!form || busyRef.current) return;
    if (form.kind === "add" && atLimit) {
      setFormError(
        `At most ${limit} active streams are allowed. Archive an offline stream to free a slot.`,
      );
      return;
    }
    if (
      form.kind !== "archive" &&
      (!name.trim() ||
        name.trim().length > 64 ||
        /[\u0000-\u001f\u007f-\u009f]/.test(name))
    ) {
      setFormError("Use a name of 1-64 characters without control characters.");
      return;
    }
    busyRef.current = true;
    version.current++;
    setBusy(true);
    setFormError("");
    try {
      if (form.kind === "archive") {
        await request(`/api/streams/${encodeURIComponent(form.stream!.id)}`, {
          method: "DELETE",
        });
        setMultiView(false);
        setStreams(
          (current) =>
            current && {
              ...current,
              active_count: current.active_count - 1,
              items: current.items.map((item) =>
                item.id === form.stream!.id
                  ? {
                      ...item,
                      archived_at: new Date().toISOString(),
                      online: false,
                      recording: null,
                      analysis_enabled: false,
                    }
                  : item,
              ),
            },
        );
      } else {
        const result = await request<Stream>(
          form.kind === "add"
            ? "/api/streams"
            : `/api/streams/${encodeURIComponent(form.stream!.id)}`,
          {
            method: form.kind === "add" ? "POST" : "PATCH",
            body: JSON.stringify({ name: name.trim() }),
          },
        );
        setStreams(
          (current) =>
            current && {
              ...current,
              active_count:
                current.active_count + (form.kind === "add" ? 1 : 0),
              items:
                form.kind === "add"
                  ? [...current.items, result]
                  : current.items.map((item) =>
                      item.id === result.id ? result : item,
                    ),
            },
        );
        if (form.kind === "add") {
          setSelectedId(result.id);
          setMultiView(false);
        }
      }
      setForm(null);
    } catch (error) {
      setFormError(message(error));
    } finally {
      version.current++;
      busyRef.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="dashboard">
      <header className="app-header">
        <Brand />
        <div className="workspace-label">
          <span className="header-divider" />
          PRIVATE WORKSPACE<span className="workspace-slash">/</span>
          <span>Overview</span>
        </div>
        <div className="header-actions">
          <span className="secure-label">
            <Icon name="lock" size={13} />
            SECURE SESSION
          </span>
          <button
            className="icon-button"
            title="Sign out"
            aria-label="Sign out"
            disabled={busy}
            onClick={() => {
              if (busyRef.current) return;
              busyRef.current = true;
              setBusy(true);
              void request("/api/auth/logout", { method: "POST" })
                .then(onLogout)
                .catch((error) => setError(message(error)))
                .finally(() => {
                  busyRef.current = false;
                  setBusy(false);
                });
            }}
          >
            <Icon name="logout" />
          </button>
        </div>
      </header>
      <section
        className="main-content stream-shell"
        aria-label="Stream management"
      >
        <div className="panel stream-manager">
          <div className="stream-toolbar">
            <div>
              <span className="eyebrow amber">SIGNAL DIRECTORY</span>
              <h2>
                Streams{" "}
                <span className="mono subdued">
                  {streams
                    ? `${streams.active_count} / ${limit} active`
                    : "Connecting"}
                </span>
              </h2>
            </div>
            <div className="stream-actions">
              <button
                className="button small primary"
                disabled={atLimit || !!error || busy}
                onClick={() => open("add")}
              >
                Add stream
              </button>
              <button
                className="button small"
                disabled={!selected || busy}
                onClick={() => open("rename")}
              >
                Rename stream
              </button>
              <button
                className="button small"
                disabled={archiveBlocked || busy}
                onClick={() => open("archive")}
              >
                Archive stream
              </button>
            </div>
          </div>
          <p className="input-help">
            {multiView
              ? `Managing: ${selected?.name ?? "No stream selected"}. Directory selection targets Rename / Archive, not the live grid.`
              : `Selected stream: ${selected?.name ?? "Connecting"}. Selection only changes this view.`}{" "}
            All active streams keep running independently.
          </p>
          {streams && (
            <div
              className="stream-grid"
              role="group"
              aria-label="Active streams"
            >
              {streams.items
                .filter((item) => !item.archived_at)
                .map((item) => (
                  <button
                    key={item.id}
                    className={`stream-card ${item.id === selectedId ? "selected" : ""}`}
                    aria-pressed={item.id === selectedId}
                    onClick={() => setSelectedId(item.id)}
                  >
                    <strong>{item.name}</strong>
                    <span>
                      <span
                        className={`status-dot ${!error && item.online ? "green" : ""}`}
                      />
                      {error
                        ? "Status unavailable"
                        : !item.media_available
                          ? "Media unavailable"
                          : item.online
                            ? "Online"
                            : "Offline"}
                      {item.is_default ? " / Default" : ""}
                    </span>
                    <span className={item.recording ? "amber" : "subdued"}>
                      {error
                        ? "Reconnecting"
                        : item.recording
                          ? "Recording"
                          : "Not recording"}{" "}
                      / {item.bitrate_mbps.toFixed(2)} Mbps
                    </span>
                  </button>
                ))}
            </div>
          )}
          {streams?.items.some((item) => item.archived_at) && (
            <div className="archive-selector">
              <label htmlFor="archived-stream">Archived history</label>
              <select
                id="archived-stream"
                value={selected?.archived_at ? selectedId : ""}
                onChange={(event) => {
                  if (event.target.value) {
                    setSelectedId(event.target.value);
                    setMultiView(false);
                  }
                }}
              >
                <option value="">Choose an archived stream</option>
                {streams.items
                  .filter((item) => item.archived_at)
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name} / Preserved history
                    </option>
                  ))}
              </select>
            </div>
          )}
          {atLimit && streams && (
            <p className="input-help">
              All {limit} active slots are in use. Archive an offline stream to
              add another.
            </p>
          )}
          {selected?.is_default && (
            <p className="input-help">
              The original default stream can be renamed, but cannot be
              archived.
            </p>
          )}
          {selected &&
            !selected.is_default &&
            !selected.archived_at &&
            archiveBlocked && (
              <p className="input-help">
                Archiving requires an offline stream, no recording, and an
                available media service.
              </p>
            )}
          {error && (
            <div className="notice danger" role="alert">
              Stream directory unavailable. Retrying automatically. {error}
            </div>
          )}
        </div>
      </section>
      <main className="main-content workspace-content">
        <div className="view-toolbar">
          <div>
            <span className="eyebrow amber">OPERATIONS / MASTER CONTROL</span>
            <h1>
              Broadcast workspace<span className="heading-dot">.</span>
            </h1>
            <p className="selected-stream-name">
              {multiView
                ? `${visibleStreams.length} connected feeds / Independent stream controls`
                : `${selected?.name ?? "Connecting"} / ${selected?.archived_at ? "Preserved history" : "Your signal, sessions, and intelligence in one place."}`}
            </p>
          </div>
          <div
            className="view-options"
            role="group"
            aria-label="Workspace view"
          >
            <button
              className={`button small ${!multiView ? "primary" : ""}`}
              aria-pressed={!multiView}
              onClick={() => setMultiView(false)}
            >
              Single view
            </button>
            <button
              className={`button small ${multiView ? "primary" : ""}`}
              aria-pressed={multiView}
              onClick={() => setMultiView(true)}
            >
              Multi-view
            </button>
          </div>
        </div>
        <div
          className={
            multiView ? "workspace-feeds multi-view" : "workspace-feeds"
          }
        >
          {visibleStreams.map((item) => (
            <Dashboard
              key={item.id}
              stream={item}
              compact={multiView}
              request={request}
              onUnauthorized={onUnauthorized}
            />
          ))}
        </div>
        {!streams ? (
          <Loading text="Loading streams" />
        ) : multiView && !visibleStreams.length ? (
          <section className="multi-empty" aria-label="No connected streams">
            <Empty icon="signal" title="No connected streams">
              Connect an encoder using a stream's RTMP credentials in Single
              view / Settings. Only connected, non-archived streams appear here.
            </Empty>
            <button
              className="button primary"
              onClick={() => setMultiView(false)}
            >
              Switch to single view
            </button>
          </section>
        ) : null}
        <footer className="app-footer">
          <span>
            <span
              className={`status-dot ${streams && !error ? "green" : ""}`}
            />
            {error
              ? "RECONNECTING TO BACKEND"
              : streams
                ? "CONNECTED TO BACKEND"
                : "CONNECTING TO BACKEND"}
          </span>
          <span>
            STATUS 1s <span className="tiny-divider">/</span> CATALOG 4s{" "}
            <span className="tiny-divider">/</span> TIMES LOCAL
          </span>
          <span>
            KUNAS/Labs<span className="amber"> CONTROL ROOM</span>
          </span>
        </footer>
      </main>
      {form && (
        <Modal
          title={
            form.kind === "add"
              ? "Add stream"
              : form.kind === "rename"
                ? `Rename stream / ${form.stream?.name}`
                : `Archive stream? / ${form.stream?.name}`
          }
          onClose={() => {
            if (!busy) setForm(null);
          }}
        >
          <form
            onSubmit={(event) => {
              void submit(event);
            }}
          >
            {form.kind === "archive" ? (
              <p className="modal-body">
                Archive {form.stream?.name}? Faces, sessions, and recordings are
                preserved in Archived history. Live publishing, recording,
                analysis, and key rotation will be disabled. This cannot be
                undone. The server will check for pending publishers before
                archiving.
              </p>
            ) : (
              <div className="stream-name-field">
                <label htmlFor="stream-name">Stream name</label>
                <input
                  id="stream-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  required
                  maxLength={64}
                  autoFocus
                  disabled={busy}
                />
                <p className="input-help">
                  1-64 characters. Names do not change publishing credentials.
                </p>
              </div>
            )}
            {formError && (
              <div className="notice danger" role="alert">
                {formError}
              </div>
            )}
            <div className="modal-actions">
              <button
                type="button"
                className="button"
                disabled={busy}
                onClick={() => setForm(null)}
              >
                Cancel
              </button>
              <button
                type="submit"
                className={`button ${form.kind === "archive" ? "destructive" : "primary"}`}
                disabled={
                  busy ||
                  (form.kind !== "archive" && !name.trim()) ||
                  (form.kind === "add" && atLimit)
                }
              >
                {busy
                  ? "Working..."
                  : form.kind === "archive"
                    ? "Archive stream"
                    : form.kind === "add"
                      ? "Add stream"
                      : "Save name"}
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}

function Dashboard({
  stream,
  compact,
  request: globalRequest,
  onUnauthorized,
}: {
  stream: Stream;
  compact: boolean;
  request: Request;
  onUnauthorized: () => void;
}) {
  // The keyed dashboard keeps in-flight mutations bound to their original stream.
  const [request] = useState(() => scopeRequest(globalRequest, stream.id));
  const [status, setStatus] = useState<Status | null>(null);
  const [statusError, setStatusError] = useState("");
  const [lastSync, setLastSync] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [tab, setTab] = useState<Tab>("faces");
  const id = useId();
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [session, setSession] = useState("");
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const busyRef = useRef(false);
  const [feedback, setFeedback] = useState<{
    text: string;
    error: boolean;
  } | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [playback, setPlayback] = useState<Recording | null>(null);
  const [playbackError, setPlaybackError] = useState("");
  const queryKey = `${session}:${page}`;
  // Mutations invalidate in-flight poll results so an older response cannot undo an action.
  const version = useRef(0);

  useSerialPoll(async (signal) => {
    const revision = version.current;
    try {
      const result = await request<Status>("/api/status", { signal });
      if (!signal.aborted && revision === version.current) {
        setStatus(result);
        setStatusError("");
        setLastSync(new Date().toISOString());
      }
    } catch (error) {
      if (!signal.aborted) setStatusError(message(error));
    }
  }, 1000);

  useSerialPoll(async (signal) => {
    const revision = version.current;
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(page * PAGE_SIZE),
      });
      if (session) params.set("session_id", session);
      const faces = await request<{ items: Face[]; total: number }>(
        `/api/faces?${params}`,
        { signal },
      );
      const sessions = await request<{ items: Session[] }>("/api/sessions", {
        signal,
      });
      const recordings = await request<{ items: Recording[] }>(
        "/api/recordings",
        { signal },
      );
      const settings = await request<Settings>("/api/settings", { signal });
      if (!signal.aborted && revision === version.current) {
        setCatalog({
          key: queryKey,
          faces,
          sessions: sessions.items,
          recordings: recordings.items,
          settings,
        });
        setCatalogError("");
      }
    } catch (error) {
      if (!signal.aborted) setCatalogError(message(error));
    }
  }, 4000);

  const faces = catalog?.key === queryKey ? catalog.faces : null;
  useEffect(() => {
    if (faces && page > 0 && page * PAGE_SIZE >= faces.total)
      setPage(Math.max(0, Math.ceil(faces.total / PAGE_SIZE) - 1));
  }, [faces, page]);

  async function act(
    name: string,
    action: () => Promise<void>,
    success?: string,
  ) {
    if (busyRef.current) return;
    busyRef.current = true;
    version.current++;
    setBusy(name);
    setFeedback(null);
    try {
      await action();
      setConfirmation(null);
      if (success) setFeedback({ text: success, error: false });
    } catch (error) {
      setFeedback({ text: message(error), error: true });
    } finally {
      version.current++;
      busyRef.current = false;
      setBusy(null);
    }
  }

  const archived =
    !!stream.archived_at || !!status?.archived || !!catalog?.settings.archived;
  const available =
    !archived && !!status && !statusError && status.media_available;
  const live = !archived && !!status?.online;
  const recording = archived ? null : status?.recording;
  const autoRecord = status?.auto_record ?? catalog?.settings.auto_record;
  const recordingState = archived ? "archived" : status?.recording_state;
  const stateLabel = recordingState
    ? {
        recording: "Recording in progress",
        waiting: "Waiting for a connection",
        stopped: "Stopped for this connection",
        disk_paused: "Paused for low storage",
        error: "Recording failed",
        manual: "Manual-only policy",
        archived: "Archived history",
      }[recordingState]
    : "Checking recording state";
  const controlsDisabled =
    archived || !status || !!statusError || busy !== null;
  const analysisEnabled =
    !archived &&
    (status?.analysis.enabled ?? catalog?.settings.analysis_enabled ?? false);

  function changeRecording(stop: boolean) {
    void act(
      "recording",
      async () => {
        if (stop) {
          await request("/api/recordings/stop", { method: "POST" });
          setStatus((current) =>
            current
              ? {
                  ...current,
                  recording: null,
                  recording_state: "stopped",
                  recording_error: null,
                  can_stop_recording: false,
                }
              : current,
          );
        } else {
          const result = await request<NonNullable<Status["recording"]>>(
            "/api/recordings/start",
            { method: "POST" },
          );
          setStatus((current) =>
            current
              ? {
                  ...current,
                  recording: result,
                  recording_state: "recording",
                  recording_error: null,
                  can_stop_recording: true,
                }
              : current,
          );
        }
      },
      stop
        ? "Recording stopped for this connection. Any capture will appear in your library when finalized."
        : "Recording started.",
    );
  }

  function deleteFace(face: Face) {
    setConfirmation({
      title: `Delete ${face.label || `face ${face.id}`}?`,
      body: "This permanently removes this face, its thumbnail, embedding, and sightings. This cannot be undone.",
      label: "Delete face",
      action: async () => {
        await request(`/api/faces/${face.id}`, { method: "DELETE" });
        setCatalog((current) =>
          current
            ? {
                ...current,
                faces: {
                  items: current.faces.items.filter(
                    (item) => item.id !== face.id,
                  ),
                  total: Math.max(0, current.faces.total - 1),
                },
              }
            : current,
        );
        setStatus((current) =>
          current
            ? { ...current, face_count: Math.max(0, current.face_count - 1) }
            : current,
        );
      },
    });
  }

  function deleteRecording(item: Recording) {
    setConfirmation({
      title: "Delete recording?",
      body: `The recording from ${timestamp(item.started_at)} will be permanently removed from disk. Download a copy first if you need to keep it.`,
      label: "Delete recording",
      action: async () => {
        await request(`/api/recordings/${encodeURIComponent(item.id)}`, {
          method: "DELETE",
        });
        setCatalog((current) =>
          current
            ? {
                ...current,
                recordings: current.recordings.filter(
                  (recording) => recording.id !== item.id,
                ),
              }
            : current,
        );
      },
    });
  }

  function changeTab(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft")
      next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    setTab(tabs[next]);
    tabRefs.current[next]?.focus();
  }

  return (
    <section
      className={`stream-dashboard ${compact ? "compact" : ""}`}
      aria-label={`${stream.name} workspace`}
    >
      <div className="stream-content">
        <div className="page-heading">
          {compact && <h2 className="tile-name">{stream.name}</h2>}
          <div
            className={`connection-pill ${statusError ? "error" : live ? "live" : ""}`}
          >
            <span
              className={`status-dot ${live && !statusError ? "glow" : ""}`}
            />
            {archived
              ? "Archived stream"
              : statusError
                ? "Connection lost"
                : !status
                  ? "Connecting"
                  : live
                    ? "Stream online"
                    : "Stream offline"}
            <span className="pill-divider" />
            {archived ? "HISTORY" : live && !statusError ? "ON AIR" : "STANDBY"}
          </div>
        </div>
        {archived && (
          <div className="notice warning" role="status">
            Preserved history mode. Faces, sessions, and recordings remain
            available to view or delete. Live publishing, recording, analysis,
            and key rotation are disabled.
          </div>
        )}
        {statusError && (
          <div className="notice danger" role="alert">
            <Icon name="alert" />
            Status is unavailable. Controls are paused; reconnecting
            automatically. {statusError}
            {lastSync && <span>Last update: {timestamp(lastSync)}</span>}
          </div>
        )}
        {status?.warning && (
          <div className="notice warning" role="status">
            <Icon name="alert" />
            {status.warning}
          </div>
        )}
        {feedback && !confirmation && (
          <div
            className={`notice ${feedback.error ? "danger" : "success"}`}
            role={feedback.error ? "alert" : "status"}
          >
            <Icon name={feedback.error ? "alert" : "check"} />
            <span>{feedback.text}</span>
            <button
              className="icon-button"
              aria-label="Dismiss message"
              onClick={() => setFeedback(null)}
            >
              <Icon name="close" size={16} />
            </button>
          </div>
        )}
        <div className="console-grid">
          <section
            className="broadcast-column"
            aria-label={`${stream.name} live broadcast`}
          >
            <div className="panel preview-panel">
              <div className="panel-heading">
                <h2>
                  <Icon name="signal" />
                  <span>Live monitor</span>
                </h2>
                <span className="mono subdued">
                  {stream.name} <span className="tiny-divider">/</span> HLS
                </span>
              </div>
              {archived ? (
                <div className="live-player archive-preview">
                  <Empty icon="video" title="Archived stream">
                    Live preview is disabled. Browse preserved faces, sessions,
                    and recordings in the tools panel.
                  </Empty>
                </div>
              ) : (
                <LivePlayer
                  streamName={stream.name}
                  manifestUrl={`/api/streams/${encodeURIComponent(stream.id)}/live/index.m3u8`}
                  online={live}
                  available={status ? status.media_available : true}
                  session={status?.session_id ?? null}
                  onUnauthorized={onUnauthorized}
                />
              )}
              <div className="preview-footer">
                <div className="stream-description">
                  <span className={`status-dot ${live ? "green" : ""}`} />
                  <span>{live ? "Incoming broadcast" : "No active input"}</span>
                </div>
                <span className="mono subdued">
                  {status?.tracks.length
                    ? status.tracks.join(" / ")
                    : "AWAITING SIGNAL"}
                </span>
              </div>
            </div>
            <div className="telemetry-grid">
              <section className="panel bitrate-panel">
                <div className="metric-heading">
                  <span>Incoming bitrate</span>
                  <Icon name="signal" size={16} />
                </div>
                <div className="metric-value">
                  {status ? status.bitrate_mbps.toFixed(2) : "--"}
                  <span>Mbps</span>
                </div>
                <BitrateChart values={status?.bitrate_history ?? []} />
                <div className="chart-labels">
                  <span>RECENT HISTORY</span>
                  <span>NOW</span>
                </div>
              </section>
              <section
                className={`panel disk-panel ${status && status.disk_free_bytes <= status.min_free_bytes ? "disk-low" : ""}`}
              >
                <div className="metric-heading">
                  <span>Available storage</span>
                  <Icon name="disk" size={16} />
                </div>
                <div className="metric-value disk-value">
                  {status ? bytes(status.disk_free_bytes) : "--"}
                </div>
                <div className="disk-status">
                  <span
                    className={`status-dot ${status ? (status.disk_free_bytes > status.min_free_bytes ? "green" : "red") : ""}`}
                  />
                  {!status
                    ? "Checking storage"
                    : status.disk_free_bytes > status.min_free_bytes
                      ? "Storage healthy"
                      : "Low disk space"}
                </div>
                <div className="disk-reserve">
                  {status
                    ? `${bytes(status.min_free_bytes)} minimum free reserve`
                    : "Waiting for disk telemetry"}
                </div>
              </section>
            </div>
            <section
              className={`panel recording-control ${recording ? "is-recording" : ""}`}
              aria-label={`${stream.name} recording controls`}
            >
              <div className="record-control-icon">
                <span
                  className={recording ? "record-dot pulse" : "record-dot"}
                />
              </div>
              <div className="record-description">
                <h2>{recording ? "Recording in progress" : "Recording"}</h2>
                <p>
                  {recording
                    ? `Started ${timestamp(recording.started_at)}`
                    : stateLabel}
                </p>
              </div>
              <button
                className={`button ${recording ? "record-stop" : "primary"}`}
                disabled={
                  controlsDisabled ||
                  (!recording &&
                    (!live ||
                      !available ||
                      (!!status &&
                        status.disk_free_bytes <= status.min_free_bytes)))
                }
                onClick={() => changeRecording(!!recording)}
              >
                {busy === "recording" ? (
                  <span className="spinner" />
                ) : (
                  <span
                    className={recording ? "stop-square" : "button-record-dot"}
                  />
                )}
                {busy === "recording"
                  ? "Working..."
                  : recording
                    ? "Stop recording"
                    : "Start recording"}
              </button>
              {!recording && (recordingState === "disk_paused" ||
                (autoRecord && !available && status?.can_stop_recording)) && (
                <button
                  className="button record-stop"
                  disabled={controlsDisabled}
                  onClick={() => changeRecording(true)}
                >
                  Stop recording
                </button>
              )}
              <div className="recording-policy">
                <p>
                  {autoRecord === undefined
                    ? "Checking server recording policy."
                    : autoRecord
                      ? compact
                        ? "Auto-record on. Stop holds this connection; reconnecting starts again. Policy details are in Settings."
                        : "Automatic recording is on (server default). Connecting starts recording on the server, even with no viewers. Stop holds for this publisher connection; a new connection starts a new recording. Start retries or resumes."
                      : compact
                        ? "Manual recording. Start records this stream."
                        : "Automatic recording is off. Start records this stream; reconnecting requires Start again."}
                </p>
                {autoRecord && !compact && (
                  <p>
                    Low storage pauses recording. It resumes after free space
                    stays above the reserve plus headroom for 5 continuous
                    seconds, unless stopped. Recorder failures require Start or
                    a new connection; no automatic failure retries.
                  </p>
                )}
                {autoRecord && !available && status?.can_stop_recording && (
                  <p>Media server unavailable. Stop keeps this publisher stopped when the connection recovers.</p>
                )}
                {status?.recording_error && (
                  <p className="danger-text" role="alert">
                    {status.recording_error}
                  </p>
                )}
              </div>
            </section>
            <section className="session-strip">
              <Icon name="clock" size={16} />
              <div>
                <span>SESSION STARTED</span>
                <strong>
                  {status?.started_at
                    ? timestamp(status.started_at)
                    : "No active session"}
                </strong>
              </div>
              <div className="session-id">
                <span>SESSION ID</span>
                <strong title={status?.session_id ?? undefined}>
                  {status?.session_id ?? "Not connected"}
                </strong>
              </div>
            </section>
            <div className="workflow-note">
              <Icon name="lock" size={14} />
              <span>
                Private input. Local storage. Your broadcast stays in your
                workspace.
              </span>
            </div>
          </section>
          <section
            className="panel inspector"
            aria-label={`${stream.name} broadcast tools`}
          >
            <div
              className="tabs"
              role="tablist"
              aria-label={`${stream.name} broadcast tools`}
            >
              {tabs.map((name, index) => (
                <button
                  key={name}
                  id={`${id}-tab-${name}`}
                  ref={(element) => {
                    tabRefs.current[index] = element;
                  }}
                  type="button"
                  role="tab"
                  aria-selected={tab === name}
                  aria-controls={`${id}-panel-${name}`}
                  tabIndex={tab === name ? 0 : -1}
                  className={tab === name ? "active" : ""}
                  onClick={() => setTab(name)}
                  onKeyDown={(event) => changeTab(event, index)}
                >
                  <Icon name={name === "recordings" ? "video" : name} />
                  <span>{name[0].toUpperCase() + name.slice(1)}</span>
                  {name === "faces" && status && (
                    <span className="tab-count">{status.face_count}</span>
                  )}
                </button>
              ))}
            </div>
            {catalogError && (
              <div className="notice danger catalog-error" role="alert">
                <Icon name="alert" />
                <span>
                  Catalog update failed.{" "}
                  {catalog ? "Showing the last available data. " : ""}Retrying
                  automatically. {catalogError}
                </span>
              </div>
            )}
            <div
              id={`${id}-panel-${tab}`}
              role="tabpanel"
              aria-labelledby={`${id}-tab-${tab}`}
              tabIndex={0}
              className="tab-panel"
            >
              {tab === "faces" && (
                <>
                  <div className="inspector-heading">
                    <div>
                      <span className="section-number">01 / INTELLIGENCE</span>
                      <h2>Face catalog</h2>
                      <p>Recognize familiar faces across your stream.</p>
                    </div>
                  </div>
                  <div className="analysis-box">
                    <div className="analysis-icon">
                      <Icon name="faces" size={21} />
                    </div>
                    <div className="analysis-description">
                      <strong>Face analysis</strong>
                      <span>
                        {busy === "analysis"
                          ? "Updating..."
                          : !status
                            ? "Checking worker"
                            : `${analysisEnabled ? status.analysis.state.replaceAll("_", " ") : "Disabled"}${status.analysis.provider ? ` / ${status.analysis.provider}` : ""}`}
                      </span>
                    </div>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={analysisEnabled}
                      aria-label="Enable face analysis"
                      className={`switch ${analysisEnabled ? "on" : ""}`}
                      disabled={controlsDisabled}
                      onClick={() => {
                        void act(
                          "analysis",
                          async () => {
                            const result = await request<{ enabled: boolean }>(
                              "/api/analysis",
                              {
                                method: "POST",
                                body: JSON.stringify({
                                  enabled: !analysisEnabled,
                                }),
                              },
                            );
                            setStatus((current) =>
                              current
                                ? {
                                    ...current,
                                    analysis: {
                                      ...current.analysis,
                                      enabled: result.enabled,
                                    },
                                  }
                                : current,
                            );
                          },
                          analysisEnabled
                            ? "Face analysis disabled."
                            : "Face analysis enabled.",
                        );
                      }}
                    >
                      <span />
                    </button>
                  </div>
                  {status?.analysis.error && (
                    <div className="notice danger" role="alert">
                      {status.analysis.error}
                    </div>
                  )}
                  <div className="worker-foot">
                    <span
                      className={`status-dot ${analysisEnabled && status?.analysis.last_seen && !status?.analysis.error ? "green" : ""}`}
                    />
                    {status?.analysis.last_seen
                      ? `Worker last seen ${timestamp(status.analysis.last_seen)}`
                      : "Waiting for worker heartbeat"}
                  </div>
                  <div className="catalog-toolbar">
                    <div className="filter-field">
                      <label htmlFor={`${id}-session-filter`}>Session</label>
                      <select
                        id={`${id}-session-filter`}
                        value={session}
                        onChange={(event) => {
                          setSession(event.target.value);
                          setPage(0);
                        }}
                      >
                        <option value="">All sessions</option>
                        {catalog?.sessions.map((item) => (
                          <option key={item.id} value={item.id}>
                            {timestamp(item.started_at)}
                            {!item.ended_at ? " (live)" : ""}
                          </option>
                        ))}
                      </select>
                    </div>
                    <button
                      className="text-button danger-text"
                      disabled={busy !== null || !status?.face_count}
                      onClick={() =>
                        setConfirmation({
                          title: "Clear the entire face catalog?",
                          body: `This deletes all faces across every session of ${stream.name}, including thumbnails, embeddings, and sightings. Other streams are unaffected. The selected session filter does not limit this action. New faces may appear while analysis is enabled.`,
                          label: "Delete all faces",
                          action: async () => {
                            await request("/api/faces", { method: "DELETE" });
                            setCatalog((current) =>
                              current
                                ? { ...current, faces: { items: [], total: 0 } }
                                : current,
                            );
                            setStatus((current) =>
                              current ? { ...current, face_count: 0 } : current,
                            );
                            setPage(0);
                          },
                        })
                      }
                    >
                      <Icon name="trash" size={15} />
                      Clear all
                    </button>
                  </div>
                  <div className="catalog-summary">
                    <span>
                      {faces
                        ? `${faces.total} ${faces.total === 1 ? "unique face" : "unique faces"}`
                        : "Loading faces..."}
                    </span>
                    <span>DETECTED / GROUPED</span>
                  </div>
                  {!faces ? (
                    <Loading text="Loading face catalog" />
                  ) : !faces.items.length ? (
                    <Empty icon="faces" title="No faces yet">
                      {session
                        ? "No faces have been detected in this session. Try another session or view the full catalog."
                        : archived
                          ? "There are no preserved faces for this stream."
                          : analysisEnabled
                            ? "Faces will appear here as the analysis worker detects them in your live broadcast."
                            : "Enable face analysis and start a stream to build your face catalog."}
                    </Empty>
                  ) : (
                    <div className="face-list">
                      {faces.items.map((face) => (
                        <FaceCard
                          key={face.id}
                          face={face}
                          disabled={busy !== null}
                          onDelete={() => deleteFace(face)}
                          onImageError={() => {
                            void request("/api/auth/me").catch(() => {});
                          }}
                        />
                      ))}
                    </div>
                  )}
                  <div className="pagination">
                    <span>
                      {faces?.total
                        ? `${page * PAGE_SIZE + 1}-${Math.min((page + 1) * PAGE_SIZE, faces.total)} of ${faces.total}`
                        : "0 faces"}
                    </span>
                    <div>
                      <button
                        className="icon-button previous"
                        aria-label="Previous page"
                        disabled={page === 0 || !faces}
                        onClick={() => setPage((value) => value - 1)}
                      >
                        <Icon name="chevron" size={16} />
                      </button>
                      <span>
                        Page {page + 1}
                        {faces
                          ? ` / ${Math.max(1, Math.ceil(faces.total / PAGE_SIZE))}`
                          : ""}
                      </span>
                      <button
                        className="icon-button"
                        aria-label="Next page"
                        disabled={
                          !faces || (page + 1) * PAGE_SIZE >= faces.total
                        }
                        onClick={() => setPage((value) => value + 1)}
                      >
                        <Icon name="chevron" size={16} />
                      </button>
                    </div>
                  </div>
                  <div className="info-note">
                    Detection confidence measures face detection. Match
                    similarity is raw cosine similarity (-1 to 1),{" "}
                    <strong>not a probability</strong>. A first observation has
                    no match score.
                  </div>
                </>
              )}
              {tab === "recordings" && (
                <>
                  <div className="inspector-heading">
                    <div>
                      <span className="section-number">02 / ARCHIVE</span>
                      <h2>Recording library</h2>
                      <p>Your broadcasts, captured and kept locally.</p>
                    </div>
                    <span className="count-badge">
                      {catalog?.recordings.length ?? "--"}
                    </span>
                  </div>
                  <div className="library-note">
                    <Icon name="disk" size={15} />
                    MP4 archive<span>LOCAL STORAGE</span>
                  </div>
                  {!catalog ? (
                    <Loading text="Loading recordings" />
                  ) : !catalog.recordings.length ? (
                    <Empty icon="video" title="Your archive starts here">
                      {archived
                        ? "There are no preserved recordings for this stream."
                        : "Completed captures appear here for playback and download. Recording follows the server policy; use Start to retry or resume an online stream."}
                    </Empty>
                  ) : (
                    <div className="recording-list">
                      {catalog.recordings.map((item) => (
                        <article className="recording-card" key={item.id}>
                          <div className="recording-card-top">
                            <div
                              className={`recording-art ${item.status === "recording" ? "active" : ""}`}
                            >
                              <Icon name="video" size={22} />
                            </div>
                            <div>
                              <h3>{timestamp(item.started_at)}</h3>
                              <span
                                className={`recording-status ${item.status}`}
                              >
                                {item.status === "recording" && (
                                  <span className="status-dot red" />
                                )}
                                {item.status}
                              </span>
                            </div>
                          </div>
                          <div className="recording-meta">
                            <span>
                              <Icon name="clock" size={13} />
                              {duration(item.duration_seconds)}
                            </span>
                            <span>{bytes(item.size_bytes)}</span>
                            <span>MP4</span>
                          </div>
                          {item.error && (
                            <p className="field-error">{item.error}</p>
                          )}
                          <div className="recording-card-actions">
                            <button
                              className="button small"
                              disabled={
                                !item.playback_url ||
                                item.status === "recording"
                              }
                              onClick={() => {
                                setPlayback(item);
                                setPlaybackError("");
                              }}
                            >
                              <Icon name="play" size={14} />
                              Play recording
                            </button>
                            {item.download_url &&
                            item.status !== "recording" ? (
                              <a
                                className="icon-button"
                                href={item.download_url}
                                download
                                aria-label={`Download recording from ${timestamp(item.started_at)}`}
                                title="Download MP4"
                              >
                                <Icon name="download" size={17} />
                              </a>
                            ) : (
                              <button
                                className="icon-button"
                                disabled
                                aria-label="Download unavailable"
                              >
                                <Icon name="download" size={17} />
                              </button>
                            )}
                            <button
                              className="icon-button danger-text"
                              disabled={
                                busy !== null || item.status === "recording"
                              }
                              aria-label={`Delete recording from ${timestamp(item.started_at)}`}
                              onClick={() => deleteRecording(item)}
                            >
                              <Icon name="trash" size={16} />
                            </button>
                          </div>
                        </article>
                      ))}
                    </div>
                  )}
                  <div className="info-note">
                    Recordings use available disk space. Download files to keep
                    a backup before deleting them. Interrupted captures may not
                    be playable.
                  </div>
                </>
              )}
              {tab === "settings" && (
                <>
                  <div className="inspector-heading">
                    <div>
                      <span className="section-number">03 / CONFIGURATION</span>
                      <h2>Stream settings</h2>
                      <p>Connect your encoder. Make the signal yours.</p>
                    </div>
                  </div>
                  {!catalog ? (
                    <Loading text="Loading stream settings" />
                  ) : (
                    <SettingsPanel
                      settings={
                        archived
                          ? {
                              ...catalog.settings,
                              archived: true,
                              stream_key: "",
                            }
                          : catalog.settings
                      }
                      busy={busy !== null}
                      canRegenerate={available && !live && busy === null}
                      onCopy={(error) =>
                        setFeedback({
                          text: error || "Copied to clipboard.",
                          error: !!error,
                        })
                      }
                      onRegenerate={() =>
                        setConfirmation({
                          title: "Regenerate stream key?",
                          body: "Your current stream key will stop working immediately. Update the key in every encoder before your next broadcast.",
                          label: "Regenerate key",
                          action: async () => {
                            const settings = await request<Settings>(
                              "/api/stream/key",
                              { method: "POST" },
                            );
                            setCatalog((current) =>
                              current ? { ...current, settings } : current,
                            );
                          },
                        })
                      }
                    />
                  )}
                </>
              )}
            </div>
          </section>
        </div>
      </div>
      {confirmation &&
        !(archived && confirmation.label === "Regenerate key") && (
          <Modal
            title={`${confirmation.title} / ${stream.name}`}
            onClose={() => {
              if (!busy) {
                setConfirmation(null);
                setFeedback(null);
              }
            }}
          >
            <p className="modal-body">{confirmation.body}</p>
            {feedback?.error && (
              <div className="notice danger" role="alert">
                {feedback.text}
              </div>
            )}
            <div className="modal-actions">
              <button
                className="button"
                disabled={busy !== null}
                onClick={() => {
                  setConfirmation(null);
                  setFeedback(null);
                }}
              >
                Cancel
              </button>
              <button
                className="button destructive"
                disabled={busy !== null}
                onClick={() => {
                  void act(
                    "confirm",
                    confirmation.action,
                    "Action completed successfully.",
                  );
                }}
              >
                {busy === "confirm" ? (
                  <span className="spinner" />
                ) : (
                  <Icon
                    name={
                      confirmation.label === "Regenerate key"
                        ? "refresh"
                        : "trash"
                    }
                    size={16}
                  />
                )}
                {busy === "confirm" ? "Working..." : confirmation.label}
              </button>
            </div>
          </Modal>
        )}
      {playback && (
        <Modal
          title={`Recording playback / ${stream.name}`}
          wide
          onClose={() => setPlayback(null)}
        >
          <video
            className="playback-video"
            src={playback.playback_url ?? undefined}
            controls
            autoPlay
            muted
            playsInline
            aria-label={`${stream.name} recording from ${timestamp(playback.started_at)}`}
            onError={() => {
              setPlaybackError(
                "This recording could not be played. It may be incomplete or use a codec unsupported by this browser. Try downloading the file.",
              );
              void request("/api/auth/me").catch(() => {});
            }}
          />
          {playbackError && (
            <div className="notice danger" role="alert">
              {playbackError}
            </div>
          )}
          <div className="playback-details">
            <div>
              <strong>{timestamp(playback.started_at)}</strong>
              <span>
                {duration(playback.duration_seconds)} /{" "}
                {bytes(playback.size_bytes)}
              </span>
            </div>
            {playback.download_url && (
              <a className="button small" href={playback.download_url} download>
                <Icon name="download" size={16} />
                Download MP4
              </a>
            )}
          </div>
        </Modal>
      )}
    </section>
  );
}

function Loading({ text }: { text: string }) {
  return (
    <div className="loading-state" role="status">
      <span className="spinner" />
      {text}
    </div>
  );
}

function BitrateChart({ values }: { values: number[] }) {
  const gradientId = useId();
  const samples = values.filter(Number.isFinite).slice(-90);
  if (!samples.length)
    return <div className="chart-empty">Waiting for bitrate samples</div>;
  const max = Math.max(...samples, 1);
  const points = samples
    .map(
      (value, index) =>
        `${samples.length === 1 ? 300 : (index / (samples.length - 1)) * 300},${44 - (Math.max(0, value) / max) * 38}`,
    )
    .join(" ");
  return (
    <svg
      className="bitrate-chart"
      viewBox="0 0 300 48"
      preserveAspectRatio="none"
      role="img"
      aria-label={`Recent incoming bitrate, ${samples.length} samples, peak ${Math.max(...samples).toFixed(2)} megabits per second`}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor="#edb557" stopOpacity="0.22" />
          <stop offset="100%" stopColor="#edb557" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path
        d="M0 14H300M0 30H300M0 46H300"
        stroke="currentColor"
        strokeDasharray="2 5"
      />
      <polygon points={`0,48 ${points} 300,48`} fill={`url(#${gradientId})`} />
      <polyline
        points={points}
        fill="none"
        stroke="#edb557"
        strokeWidth="1.75"
        vectorEffect="non-scaling-stroke"
      />
      {samples.length === 1 && (
        <circle
          cx="300"
          cy={44 - (Math.max(0, samples[0]) / max) * 38}
          r="2"
          fill="#edb557"
        />
      )}
    </svg>
  );
}

function FaceCard({
  face,
  disabled,
  onDelete,
  onImageError,
}: {
  face: Face;
  disabled: boolean;
  onDelete: () => void;
  onImageError: () => void;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <article className="face-card">
      <div className="face-thumbnail">
        {failed ? (
          <Icon name="faces" size={30} />
        ) : (
          <img
            src={face.thumbnail_url}
            alt={`Detected face: ${face.label || face.id}`}
            loading="lazy"
            onError={() => {
              setFailed(true);
              onImageError();
            }}
          />
        )}
        <span>#{String(face.id).padStart(3, "0")}</span>
      </div>
      <div className="face-details">
        <div className="face-title">
          <h3>{face.label || `Face ${face.id}`}</h3>
          <button
            className="icon-button danger-text"
            disabled={disabled}
            aria-label={`Delete ${face.label || `face ${face.id}`}`}
            onClick={onDelete}
          >
            <Icon name="trash" size={14} />
          </button>
        </div>
        <div className="face-scores">
          <span title="Model confidence that the detected region contains a face">
            <span className="score-label">Detection</span>
            <strong>
              {(face.detection_confidence * 100).toFixed(1)}
              <small>%</small>
            </strong>
          </span>
          <span title="Raw cosine similarity from -1 to 1. Not a probability.">
            <span className="score-label">Cosine match</span>
            <strong className="cosine">
              {face.match_similarity === null
                ? "No match yet"
                : face.match_similarity.toFixed(3)}
            </strong>
          </span>
          <span>
            <span className="score-label">Sightings</span>
            <strong>{face.sightings.toLocaleString()}</strong>
          </span>
        </div>
        <div className="face-timestamps">
          <span>
            <b>First</b>
            <time dateTime={face.first_seen}>{timestamp(face.first_seen)}</time>
          </span>
          <span>
            <b>Last</b>
            <time dateTime={face.last_seen}>{timestamp(face.last_seen)}</time>
          </span>
        </div>
      </div>
    </article>
  );
}

function SettingsPanel({
  settings,
  busy,
  canRegenerate,
  onCopy,
  onRegenerate,
}: {
  settings: Settings;
  busy: boolean;
  canRegenerate: boolean;
  onCopy: (error?: string) => void;
  onRegenerate: () => void;
}) {
  const [revealed, setRevealed] = useState(false);
  const id = useId();
  useEffect(() => {
    setRevealed(false);
  }, [settings.stream_key]);
  useEffect(() => {
    if (!revealed) return;
    const timeout = setTimeout(() => setRevealed(false), 30000);
    const hide = () => {
      if (document.hidden) setRevealed(false);
    };
    document.addEventListener("visibilitychange", hide);
    return () => {
      clearTimeout(timeout);
      document.removeEventListener("visibilitychange", hide);
    };
  }, [revealed]);
  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      onCopy();
    } catch {
      onCopy(
        "Clipboard access is unavailable. Use HTTPS or localhost, or reveal and select the value to copy manually.",
      );
    }
  }
  return (
    <>
      {settings.archived ? (
        <div className="settings-section">
          <h3>Encoder connection disabled</h3>
          <p>
            Archived streams have no publishing credentials. Preserved history
            remains accessible.
          </p>
          <button className="button small" disabled>
            Regenerate stream key
          </button>
        </div>
      ) : (
        <div className="settings-section">
          <h3>
            <span className="step-label">01</span>Encoder connection
          </h3>
          <p>
            In OBS or your encoder, choose a custom RTMP service and enter these
            credentials.
          </p>
          <label htmlFor={`${id}-rtmp-url`}>Server URL</label>
          <div className="credential-field">
            <input
              id={`${id}-rtmp-url`}
              readOnly
              value={settings.rtmp_url}
              spellCheck={false}
            />
            <button
              className="icon-button"
              aria-label="Copy RTMP server URL"
              onClick={() => {
                void copy(settings.rtmp_url);
              }}
            >
              <Icon name="copy" size={16} />
            </button>
          </div>
          <label htmlFor={`${id}-stream-key`}>
            Stream key{" "}
            <span className="label-tag">
              <Icon name="lock" size={11} />
              SECRET
            </span>
          </label>
          <div className="credential-field">
            <input
              id={`${id}-stream-key`}
              type={revealed ? "text" : "password"}
              readOnly
              value={settings.stream_key}
              autoComplete="off"
              spellCheck={false}
            />
            <button
              className="icon-button"
              aria-label={
                revealed
                  ? "Hide stream key"
                  : "Reveal stream key for 30 seconds"
              }
              aria-pressed={revealed}
              onClick={() => setRevealed((value) => !value)}
            >
              <Icon name="eye" size={16} />
            </button>
            <button
              className="icon-button"
              aria-label="Copy entire stream key"
              onClick={() => {
                void copy(settings.stream_key);
              }}
            >
              <Icon name="copy" size={16} />
            </button>
          </div>
          <p className="input-help">
            Copy the entire key, including its query parameters. Keep it
            private. Revealed keys hide automatically after 30 seconds.
          </p>
          <button
            className="button small"
            disabled={busy || !canRegenerate}
            onClick={onRegenerate}
          >
            <Icon name="refresh" size={15} />
            Regenerate stream key
          </button>
          <p className="input-help">
            Available only while the stream is offline and the media service is
            reachable.
          </p>
        </div>
      )}
      <div className="settings-section">
        <h3>Recording policy</h3>
        <p>
          {settings.auto_record
            ? "Automatic recording is on (AUTO_RECORD=true, the server default). The server records connected streams without an open browser."
            : "Automatic recording is off (AUTO_RECORD=false). Use Start for each recording."}{" "}
          This policy is read-only. Face analysis remains opt-in.
        </p>
      </div>
      <div className="settings-section">
        <h3>
          <span className="step-label">02</span>Analysis configuration
        </h3>
        <p>Managed by the server environment. These values are read-only.</p>
        <dl className="settings-values">
          <Setting label="Detection threshold">
            {(settings.detection_threshold * 100).toFixed(1)}%
          </Setting>
          <Setting label="Match threshold (raw cosine)">
            {settings.match_threshold.toFixed(3)}
          </Setting>
          <Setting label="Analysis sampling">
            {settings.analysis_fps} fps
          </Setting>
          <Setting label="Face retention">
            {settings.face_retention_days} days
          </Setting>
          <Setting label="Maximum stored faces (all streams)">
            {settings.max_faces.toLocaleString()}
          </Setting>
        </dl>
        <div className="info-note">
          The match threshold uses raw cosine similarity (-1 to 1). It is{" "}
          <strong>not a probability or a percentage</strong>.
        </div>
      </div>
    </>
  );
}

function Setting({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}
