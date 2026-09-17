import { useEffect, useState } from "react";

import { asApiError } from "../api/http";
import { Field } from "../components/Field";
import { ThreePartError } from "../components/ThreePartError";
import { lifetimeWords, signIn as copy, type ThreePart } from "../copy/en";
import type { Session } from "./store";

// Sign in (docs/ui/sign-in.md): the front door. The heading is the installation name from the
// public endpoint, or the product name until it arrives. Errors stay under the form until the
// next attempt; after 5 s of waiting the page says the server is slow.

interface Props {
  session: Session;
  /** A dev-only line under the form naming the fake accounts; never set in production. */
  hint?: string;
}

const SLOW_AFTER_MS = 5000;

export function SignInPage({ session, hint }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [waiting, setWaiting] = useState(false);
  const [slow, setSlow] = useState(false);
  const [problem, setProblem] = useState<ThreePart | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    if (!waiting) {
      setSlow(false);
      return undefined;
    }
    const timer = window.setTimeout(() => setSlow(true), SLOW_AFTER_MS);
    return () => window.clearTimeout(timer);
  }, [waiting]);

  const notice =
    session.state.status === "anonymous"
      ? session.state.notice === "signed-out"
        ? copy.signedOut
        : session.state.notice === "expired"
          ? copy.sessionEnded(lifetimeWords(session.installation?.session_lifetime_hours ?? 8))
          : null
      : null;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setProblem(null);
    session.clearNotice();
    setWaiting(true);
    try {
      await session.signIn(email.trim(), password);
      setPassword("");
    } catch (error: unknown) {
      const apiError = asApiError(error);
      setUnreachable(apiError.unreachable);
      setProblem(apiError.describe(copy.serverNotAnswering));
    } finally {
      setWaiting(false);
    }
  }

  const heading = session.installation?.installation_name ?? copy.fallbackHeading;
  const button = waiting ? copy.buttonWaiting : unreachable && problem !== null ? copy.buttonRetry : copy.button;

  return (
    <main className="auth">
      <form className="panel auth-card stack" onSubmit={(event) => void submit(event)} aria-labelledby="sign-in-heading">
        <div>
          <h1 id="sign-in-heading">{heading}</h1>
          <p className="lede">{copy.lede}</p>
        </div>
        {notice !== null && (
          <p role="status" className="sentence">
            {notice}
          </p>
        )}
        <Field label={copy.email}>
          {(control) => (
            <input
              {...control}
              className="input"
              type="email"
              name="email"
              autoComplete="username"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          )}
        </Field>
        <Field label={copy.password}>
          {(control) => (
            <input
              {...control}
              className="input"
              type="password"
              name="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          )}
        </Field>
        <div className="row">
          <button type="submit" className="btn primary" disabled={waiting}>
            {button}
          </button>
          {waiting && slow && (
            <span role="status" className="muted">
              {copy.stillChecking}
            </span>
          )}
        </div>
        {problem !== null && <ThreePartError parts={problem} />}
        {hint !== undefined && <p className="faint">{hint}</p>}
      </form>
    </main>
  );
}
