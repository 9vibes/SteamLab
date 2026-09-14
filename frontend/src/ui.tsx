import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

const paths = {
  signal: "M4 17v3m5-8v8m5-13v13m5-18v18",
  faces:
    "M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3M3 16v3a2 2 0 0 0 2 2h3m8 0h3a2 2 0 0 0 2-2v-3M9 9h.01M15 9h.01M8 14c2 3 6 3 8 0",
  video:
    "M15 8l6-3v14l-6-3M5 5h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z",
  settings: "M4 7h16M4 17h16M8 4v6m8 4v6",
  lock: "M7 10V7a5 5 0 0 1 10 0v3M5 10h14v11H5zM12 14v3",
  arrow: "M5 12h14m-5-5 5 5-5 5",
  logout: "M9 4H4v16h5m-1-8h13m-4-4 4 4-4 4",
  close: "m6 6 12 12M6 18 18 6",
  trash: "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7",
  download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  play: "m8 4 12 8-12 8Z",
  copy: "M9 9h12v12H9zM15 5V3H3v12h2",
  eye: "M2 12s3-7 10-7 10 7 10 7-3 7-10 7S2 12 2 12Zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z",
  refresh: "M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 3M5 15a8 8 0 0 0 13 3",
  disk: "M5 3h14l3 13v5H2v-5L5 3ZM2 16h20M6 19h.01M10 19h.01",
  alert: "m12 3 10 18H2L12 3Zm0 6v5m0 3h.01",
  check: "m5 12 4 4L19 6",
  chevron: "m9 5 7 7-7 7",
  clock: "M12 8v5l3 2m7-3a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z",
} as const;

export function Icon({
  name,
  size = 18,
}: {
  name: keyof typeof paths;
  size?: number;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}

export function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 40 40">
          <path
            d="M10 10h5v8l9-8h7L20 20l11 10h-7l-9-8v8h-5z"
            fill="currentColor"
          />
        </svg>
      </span>
      <span>
        KUNAS<span className="brand-light">/Labs</span>
        <small>BROADCAST CONSOLE</small>
      </span>
    </div>
  );
}

export function Empty({
  icon,
  title,
  children,
}: {
  icon: keyof typeof paths;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon name={icon} size={26} />
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current!;
    const previous = document.activeElement as HTMLElement | null;
    dialog.showModal();
    dialog.querySelector<HTMLInputElement>("input:not(:disabled)")?.focus();
    return () => {
      dialog.close();
      previous?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal ${wide ? "modal-wide" : ""}`}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          const box = event.currentTarget.getBoundingClientRect();
          if (
            event.clientX < box.left ||
            event.clientX > box.right ||
            event.clientY < box.top ||
            event.clientY > box.bottom
          )
            onClose();
        }
      }}
    >
      <div className="modal-header">
        <h2 id={titleId}>{title}</h2>
        <button
          className="icon-button"
          aria-label="Close dialog"
          onClick={onClose}
        >
          <Icon name="close" />
        </button>
      </div>
      {children}
    </dialog>
  );
}
