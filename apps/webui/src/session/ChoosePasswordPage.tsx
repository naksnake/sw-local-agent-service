import { useState } from "react";

import { asApiError } from "../api/http";
import { Field } from "../components/Field";
import { ThreePartError } from "../components/ThreePartError";
import { choosePassword as copy, type ThreePart } from "../copy/en";
import type { Person } from "./api";
import type { Session } from "./store";

// Choose a new password (docs/ui/sign-in.md): forced while `must_change_password` is true.
// The route guard keeps a reloaded page here until a password is saved. Mismatch, too short
// and same-as-email are checked here with the same sentences the api uses; whatever the api
// answers is rendered as received.

interface Props {
  session: Session;
  person: Person;
}

export function ChoosePasswordPage({ session, person }: Props) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [problem, setProblem] = useState<ThreePart | null>(null);

  function check(): ThreePart | null {
    if (next !== again) {
      return copy.mismatch;
    }
    if (next.length < copy.minLength) {
      return copy.tooShort(next.length);
    }
    if (next.toLowerCase() === person.email.toLowerCase()) {
      return copy.sameAsEmail;
    }
    return null;
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const local = check();
    if (local !== null) {
      setProblem(local);
      return;
    }
    setProblem(null);
    setWaiting(true);
    try {
      await session.changePassword(next, session.needsCurrentPassword ? current : undefined);
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(copy.serverNotAnswering));
    } finally {
      setWaiting(false);
    }
  }

  return (
    <main className="auth">
      <form className="panel auth-card stack" onSubmit={(event) => void submit(event)} aria-labelledby="choose-heading">
        <div>
          <h1 id="choose-heading">{copy.heading}</h1>
          <p className="lede">{copy.lede}</p>
        </div>
        {session.needsCurrentPassword && (
          <Field label={copy.current} help={copy.currentHelp}>
            {(control) => (
              <input
                {...control}
                className="input"
                type="password"
                autoComplete="current-password"
                required
                value={current}
                onChange={(event) => setCurrent(event.target.value)}
              />
            )}
          </Field>
        )}
        <Field label={copy.newPassword} help={copy.help}>
          {(control) => (
            <input
              {...control}
              className="input"
              type="password"
              autoComplete="new-password"
              required
              value={next}
              onChange={(event) => setNext(event.target.value)}
            />
          )}
        </Field>
        <Field label={copy.again}>
          {(control) => (
            <input
              {...control}
              className="input"
              type="password"
              autoComplete="new-password"
              required
              value={again}
              onChange={(event) => setAgain(event.target.value)}
            />
          )}
        </Field>
        <div className="row">
          <button type="submit" className="btn primary" disabled={waiting}>
            {waiting ? copy.buttonWaiting : copy.button}
          </button>
        </div>
        {problem !== null && <ThreePartError parts={problem} />}
      </form>
    </main>
  );
}
