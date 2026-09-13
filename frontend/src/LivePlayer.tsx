import type Hls from "hls.js";
import { useEffect, useRef, useState } from "react";
import { Icon } from "./ui";

export default function LivePlayer({
  online,
  available,
  session,
  manifestUrl,
  onUnauthorized,
}: {
  online: boolean;
  available: boolean;
  session: string | null;
  manifestUrl: string;
  onUnauthorized: () => void;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<
    "loading" | "playing" | "error" | "blocked"
  >("loading");
  const [retry, setRetry] = useState(0);
  const [reason, setReason] = useState("");

  useEffect(() => {
    const element = video.current!;
    setState("loading");
    setReason("");
    if (!online || !available) return;
    let disposed = false;
    let hls: Hls | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    const fail = (
      text = "The live connection was interrupted. Reconnecting shortly.",
    ) => {
      if (disposed) return;
      setReason(text);
      setState("error");
      if (!retryTimer)
        retryTimer = setTimeout(() => {
          if (!disposed) setRetry((value) => value + 1);
        }, 5000);
    };
    const loading = () => {
      if (disposed) return;
      setState("loading");
      clearTimeout(watchdog);
      watchdog = setTimeout(
        () => fail("No playable media received. Retrying the live connection."),
        20000,
      );
    };
    const playing = () => {
      clearTimeout(watchdog);
      clearTimeout(retryTimer);
      retryTimer = undefined;
      if (!disposed) setState("playing");
    };
    const play = () => {
      void element.play().catch((error) => {
        if (!disposed && error.name === "NotAllowedError") {
          clearTimeout(watchdog);
          setState("blocked");
        }
      });
    };
    const nativeError = () => {
      // Native HLS does not expose HTTP status; probe the authenticated manifest.
      void fetch(manifestUrl, {
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      })
        .then((response) => {
          if (!disposed) {
            if (response.status === 401) onUnauthorized();
            else fail();
          }
        })
        .catch(() => {
          if (!disposed) fail();
        });
    };
    const controller = new AbortController();
    element.addEventListener("playing", playing);
    element.addEventListener("waiting", loading);
    element.addEventListener("stalled", loading);
    element.addEventListener("ended", nativeError);
    element.addEventListener("error", nativeError);
    loading();
    void import("hls.js")
      .then(({ default: Hls }) => {
        if (disposed) return;
        if (Hls.isSupported()) {
          hls = new Hls({ lowLatencyMode: true, backBufferLength: 30 });
          hls.on(Hls.Events.MANIFEST_PARSED, play);
          hls.on(Hls.Events.ERROR, (_event, data) => {
            if (disposed) return;
            if (data.response?.code === 401) {
              onUnauthorized();
              return;
            }
            if (data.fatal) {
              hls?.destroy();
              hls = null;
              fail();
            }
          });
          hls.loadSource(manifestUrl);
          hls.attachMedia(element);
        } else if (element.canPlayType("application/vnd.apple.mpegurl")) {
          element.src = manifestUrl;
          element.addEventListener("loadedmetadata", play);
        } else {
          clearTimeout(watchdog);
          setState("error");
          setReason(
            "This browser does not support HLS playback. Use a current version of Chrome, Firefox, Edge, or Safari.",
          );
        }
      })
      .catch(() =>
        fail("The playback engine could not be loaded. Retrying shortly."),
      );
    return () => {
      disposed = true;
      controller.abort();
      clearTimeout(retryTimer);
      clearTimeout(watchdog);
      hls?.destroy();
      element.removeEventListener("playing", playing);
      element.removeEventListener("waiting", loading);
      element.removeEventListener("stalled", loading);
      element.removeEventListener("ended", nativeError);
      element.removeEventListener("error", nativeError);
      element.removeEventListener("loadedmetadata", play);
      element.pause();
      element.removeAttribute("src");
      element.load();
    };
  }, [online, available, session, manifestUrl, retry, onUnauthorized]);

  const waiting = !online || !available;
  return (
    <div className={`live-player ${waiting ? "is-waiting" : ""}`}>
      <video
        ref={video}
        autoPlay
        muted
        playsInline
        controls={online && available}
        aria-label="Live stream preview"
      />
      {(waiting || state !== "playing") && (
        <div
          className={`player-overlay ${!waiting && state === "loading" ? "is-loading" : ""}`}
          role="status"
        >
          <div
            className={`player-symbol ${!waiting && state === "loading" ? "pulse" : ""}`}
          >
            <Icon
              name={waiting ? "signal" : state === "error" ? "alert" : "play"}
              size={30}
            />
          </div>
          <span className="eyebrow">
            {waiting
              ? "STANDING BY"
              : state === "error"
                ? "CONNECTION NOTICE"
                : state === "blocked"
                  ? "READY TO PLAY"
                  : "CONNECTING"}
          </span>
          <h2>
            {!available
              ? "Media service unavailable"
              : !online
                ? "Waiting for your broadcast"
                : state === "error"
                  ? "Live preview interrupted"
                  : state === "blocked"
                    ? "Start the live preview"
                    : "Tuning into your stream"}
          </h2>
          <p>
            {!available
              ? "The media service is not reachable. The console will keep checking."
              : !online
                ? "Connect your encoder with the RTMP credentials in Settings. Your preview will appear here automatically."
                : state === "error"
                  ? reason
                  : state === "blocked"
                    ? "Your browser needs permission to begin playback."
                    : "The stream is online. Waiting for the first playable segment."}
          </p>
          {!waiting && state === "error" && (
            <button
              className="button small"
              onClick={() => setRetry((value) => value + 1)}
            >
              <Icon name="refresh" />
              Retry now
            </button>
          )}
          {!waiting && state === "blocked" && (
            <button
              className="button primary"
              onClick={() => {
                void video.current?.play().catch(() => setState("blocked"));
              }}
            >
              <Icon name="play" />
              Play preview
            </button>
          )}
        </div>
      )}
      <div className="player-corner top-left" />
      <div className="player-corner top-right" />
      <div className="player-corner bottom-left" />
      <div className="player-corner bottom-right" />
    </div>
  );
}
