export interface Status {
  online: boolean;
  media_available: boolean;
  session_id: string | null;
  started_at: string | null;
  bitrate_mbps: number;
  bitrate_history: number[];
  tracks: string[];
  recording: { id: string; started_at: string } | null;
  disk_free_bytes: number;
  min_free_bytes: number;
  warning: string | null;
  analysis: {
    enabled: boolean;
    state: string;
    provider: string | null;
    error: string | null;
    last_seen: string | null;
  };
  face_count: number;
}

export interface Settings {
  rtmp_url: string;
  stream_key: string;
  analysis_enabled: boolean;
  match_threshold: number;
  detection_threshold: number;
  face_retention_days: number;
  max_faces: number;
  analysis_fps: number;
}

export interface Face {
  id: number;
  label: string;
  first_seen: string;
  last_seen: string;
  sightings: number;
  detection_confidence: number;
  match_similarity: number | null;
  thumbnail_url: string;
}

export interface Session {
  id: string;
  started_at: string;
  ended_at: string | null;
}

export interface Recording {
  id: string;
  started_at: string;
  ended_at: string | null;
  status: "recording" | "ready" | "interrupted" | "error";
  size_bytes: number;
  duration_seconds: number | null;
  download_url: string | null;
  playback_url: string | null;
  error: string | null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export type Request = <T>(path: string, init?: RequestInit) => Promise<T>;

export async function api<T>(
  path: string,
  init: RequestInit = {},
  csrf?: string,
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (csrf && init.method && init.method !== "GET")
    headers.set("X-CSRF-Token", csrf);
  const timeout = AbortSignal.timeout(15000);
  const response = await fetch(path, {
    ...init,
    headers,
    signal: init.signal ? AbortSignal.any([init.signal, timeout]) : timeout,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      response.status,
      typeof body?.detail === "string"
        ? body.detail
        : `Request failed (${response.status}).`,
    );
  }
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
}

export function message(error: unknown) {
  if (error instanceof Error && error.name === "TimeoutError")
    return "The server took too long to respond. Check the connection and try again.";
  return error instanceof Error
    ? error.message
    : "Something went wrong. Please try again.";
}

export function timestamp(value: string | null) {
  if (!value) return "Not available";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

export function bytes(value: number) {
  if (!Number.isFinite(value)) return "Unavailable";
  if (value < 1024) return `${value} B`;
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** unit).toFixed(1)} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}

export function duration(seconds: number | null) {
  if (seconds === null) return "Pending";
  const total = Math.max(0, Math.floor(seconds));
  return `${Math.floor(total / 3600)
    .toString()
    .padStart(2, "0")}:${Math.floor((total / 60) % 60)
    .toString()
    .padStart(2, "0")}:${(total % 60).toString().padStart(2, "0")}`;
}
