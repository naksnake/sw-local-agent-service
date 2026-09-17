import type { ThreePart } from "../copy/en";

// The one error component (ADR-0009): what happened, likely cause and what to do, rendered
// inline and left in place until the next attempt replaces it. Nothing in this codebase
// shows an error any other way, and nothing pops up and disappears on its own.

interface Props {
  parts: ThreePart;
  /** `fail` for an error, `warn` for a notice that still needs a person (the .env mirror). */
  tone?: "fail" | "warn";
  /** Overrides the default: errors are alerts, warnings are status. */
  role?: "alert" | "status";
  /** An action that belongs to the error, such as Try again. */
  action?: React.ReactNode;
}

export function ThreePartError({ parts, tone = "fail", role, action }: Props) {
  return (
    <section role={role ?? (tone === "fail" ? "alert" : "status")} className={tone === "warn" ? "notice warn" : "notice"}>
      <strong>{parts.whatHappened}</strong>
      <dl>
        {parts.likelyCause !== "" && (
          <>
            <dt>Likely cause</dt>
            <dd>{parts.likelyCause}</dd>
          </>
        )}
        <dt>What to do</dt>
        <dd>{parts.whatToDo}</dd>
      </dl>
      {action !== undefined && <div className="row" style={{ marginTop: 10 }}>{action}</div>}
    </section>
  );
}
