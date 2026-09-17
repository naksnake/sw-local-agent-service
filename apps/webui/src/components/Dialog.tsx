import { useEffect, useId, useRef } from "react";

// One dialog per action (ADR-0009): its content advances from confirmation to result inside
// the same box; dialogs never stack. Escape and the backdrop close it unless `onClose` is
// withheld (the one-time password panel closes only through Done).

interface Props {
  title: string;
  onClose?: () => void;
  children: React.ReactNode;
}

export function Dialog({ title, onClose, children }: Props) {
  const titleId = useId();
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const first = box.current?.querySelector<HTMLElement>("input, select, textarea, button");
    first?.focus();
    if (onClose === undefined) {
      return undefined;
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id={titleId}>{title}</h2>
        {children}
      </div>
    </div>
  );
}
