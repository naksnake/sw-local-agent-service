import { useId } from "react";

// A labelled control with optional help text: the label names the control and the help is
// tied to it with aria-describedby, so the label's own text stays exactly the copy.

interface Control {
  id: string;
  "aria-describedby"?: string;
}

interface Props {
  label: string;
  help?: string;
  children: (control: Control) => React.ReactNode;
}

export function Field({ label, help, children }: Props) {
  const id = useId();
  const helpId = `${id}-help`;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children(help === undefined ? { id } : { id, "aria-describedby": helpId })}
      {help !== undefined && (
        <small id={helpId} className="help">
          {help}
        </small>
      )}
    </div>
  );
}
