import { useId, useState } from "react";
import type { Stream, StreamList } from "./api";

export type DirectoryProps = {
  streams: StreamList | null;
  error: string;
  busy: boolean;
  multiView: boolean;
  onSelect: (id: string) => void;
  onOpen: (kind: "add" | "rename" | "archive", target?: Stream) => void;
};

export default function SignalDirectory({
  streamId,
  streams,
  error,
  busy,
  multiView,
  onSelect,
  onOpen,
}: DirectoryProps & { streamId: string }) {
  const [selectedId, setSelectedId] = useState(streamId);
  const archivedId = useId();
  const selected = streams?.items.find((item) => item.id === selectedId);
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
  function select(id: string) {
    setSelectedId(id);
    onSelect(id);
  }
  return (
    <section
      className="stream-manager signal-directory"
      aria-label="Stream management"
    >
      <div className="stream-toolbar">
        <h2>
          Streams{" "}
          <span className="mono subdued">
            {streams
              ? `${streams.active_count} / ${limit} active`
              : "Connecting"}
          </span>
        </h2>
        <div className="stream-actions">
          <button
            className="button small primary"
            disabled={atLimit || !!error || busy}
            onClick={() => onOpen("add")}
          >
            Add stream
          </button>
          <button
            className="button small"
            disabled={!selected || busy}
            onClick={() => onOpen("rename", selected)}
          >
            Rename stream
          </button>
          <button
            className="button small"
            disabled={archiveBlocked || busy}
            onClick={() => onOpen("archive", selected)}
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
        <div className="stream-grid" role="group" aria-label="Active streams">
          {streams.items
            .filter((item) => !item.archived_at)
            .map((item) => (
              <button
                key={item.id}
                className={`stream-card ${item.id === selectedId ? "selected" : ""}`}
                aria-pressed={item.id === selectedId}
                onClick={() => select(item.id)}
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
          <label htmlFor={archivedId}>Archived history</label>
          <select
            id={archivedId}
            value={selected?.archived_at ? selectedId : ""}
            onChange={(event) => {
              if (event.target.value) select(event.target.value);
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
          All {limit} active slots are in use. Archive an offline stream to add
          another.
        </p>
      )}
      {selected?.is_default && (
        <p className="input-help">
          The original default stream can be renamed, but cannot be archived.
        </p>
      )}
      {selected &&
        !selected.is_default &&
        !selected.archived_at &&
        archiveBlocked && (
          <p className="input-help">
            Archiving requires an offline stream, no recording, and an available
            media service.
          </p>
        )}
      {error && (
        <div className="notice danger" role="alert">
          Stream directory unavailable. Retrying automatically. {error}
        </div>
      )}
    </section>
  );
}
