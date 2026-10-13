import { useState } from "react";

import { asApiError } from "../api/http";
import { Field } from "../components/Field";
import { ThreePartError } from "../components/ThreePartError";
import { choosePassword, roleInfo, type ThreePart } from "../copy/en";
import { humanTime } from "../time";
import type { Person } from "./api";
import type { Session } from "./store";

// Settings → Your account (docs/ui/account.md): your name as commits and tickets show it, the
// sign-in name and role (read only: the name is your Coding/<name>/ folder and the role is an
// administrator's call), a new password, and "sign out everywhere else". One form per box,
// each with its own sentence after it saves.

interface Props {
  session: Session;
  person: Person;
}

const NOT_ANSWERING = (what: string): ThreePart => ({
  whatHappened: what,
  likelyCause: "The api service didn't answer.",
  whatToDo: "Try again; if it repeats, run `slas logs api` on the host.",
});

export function AccountPage({ session, person }: Props) {
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Your account</h1>
          <p className="lede">How you appear on commits and tickets, and how you sign in.</p>
        </div>
      </div>
      <div className="cols cols-2">
        <NameBox session={session} person={person} />
        <section className="panel" aria-labelledby="signin-heading">
          <h2 id="signin-heading">Sign-in</h2>
          <dl className="facts compact">
            <dt>Sign-in name</dt>
            <dd>
              <span className="mono">{person.username}</span>
              <div className="faint">
                It is also your folder, Coding/{person.username}/, so it stays the same. Ask an administrator for a
                new account if you need another name.
              </div>
            </dd>
            <dt>Role</dt>
            <dd>
              {person.role_label}
              <div className="faint">{roleInfo(person.role).sentence}</div>
            </dd>
            <dt>Last sign-in</dt>
            <dd>{humanTime(person.last_sign_in_at)}</dd>
          </dl>
        </section>
        <PasswordBox session={session} />
        <SessionsBox session={session} />
      </div>
    </div>
  );
}

function NameBox({ session, person }: Props) {
  const [name, setName] = useState(person.display_name);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [problem, setProblem] = useState<ThreePart | null>(null);
  const changed = name.trim() !== person.display_name;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setProblem(null);
    setSaved(null);
    setBusy(true);
    try {
      const updated = await session.renameMe(name);
      setName(updated.display_name);
      setSaved(`Saved. New commits and tickets show ${updated.display_name}.`);
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(NOT_ANSWERING("Your name wasn't saved.")));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel stack" aria-labelledby="name-heading" noValidate onSubmit={(event) => void submit(event)}>
      <h2 id="name-heading">Name</h2>
      <Field label="Your name" help="Shown on your commits, tickets and the top bar.">
        {(control) => (
          <input {...control} className="input" value={name} maxLength={120} onChange={(event) => setName(event.target.value)} />
        )}
      </Field>
      <div className="row">
        <button type="submit" className="btn primary" disabled={busy || !changed}>
          {busy ? "Saving…" : "Save name"}
        </button>
      </div>
      {saved !== null && (
        <p className="sentence" role="status">
          {saved}
        </p>
      )}
      {problem !== null && <ThreePartError parts={problem} />}
    </form>
  );
}

function PasswordBox({ session }: { session: Session }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [problem, setProblem] = useState<ThreePart | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setProblem(null);
    setSaved(null);
    if (next !== again) {
      setProblem(choosePassword.mismatch);
      return;
    }
    if (next.length < choosePassword.minLength) {
      setProblem(choosePassword.tooShort(next.length));
      return;
    }
    setBusy(true);
    try {
      await session.changePassword(next, current);
      setCurrent("");
      setNext("");
      setAgain("");
      setSaved("Your password was changed. Your other sign-ins have ended; this one stays.");
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(NOT_ANSWERING("Your password wasn't changed.")));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="panel stack" aria-labelledby="password-heading" noValidate onSubmit={(event) => void submit(event)}>
      <h2 id="password-heading">Password</h2>
      <Field label="Current password">
        {(control) => (
          <input
            {...control}
            className="input"
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(event) => setCurrent(event.target.value)}
          />
        )}
      </Field>
      <Field label="New password" help={choosePassword.help}>
        {(control) => (
          <input
            {...control}
            className="input"
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(event) => setNext(event.target.value)}
          />
        )}
      </Field>
      <Field label="Type it again">
        {(control) => (
          <input
            {...control}
            className="input"
            type="password"
            autoComplete="new-password"
            value={again}
            onChange={(event) => setAgain(event.target.value)}
          />
        )}
      </Field>
      <div className="row">
        <button type="submit" className="btn primary" disabled={busy || current === "" || next === ""}>
          {busy ? "Changing…" : "Change password"}
        </button>
      </div>
      {saved !== null && (
        <p className="sentence" role="status">
          {saved}
        </p>
      )}
      {problem !== null && <ThreePartError parts={problem} />}
    </form>
  );
}

function SessionsBox({ session }: { session: Session }) {
  const [busy, setBusy] = useState(false);
  const [sentence, setSentence] = useState<string | null>(null);
  const [problem, setProblem] = useState<ThreePart | null>(null);

  async function endOthers() {
    setProblem(null);
    setBusy(true);
    try {
      setSentence(await session.endOtherSessions());
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(NOT_ANSWERING("The other sign-ins weren't ended.")));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel stack" aria-labelledby="sessions-heading">
      <h2 id="sessions-heading">Other sign-ins</h2>
      <p className="muted">
        Signed in on another computer and left it? End every other sign-in of yours; this one stays.
      </p>
      <div className="row">
        <button type="button" className="btn" disabled={busy} onClick={() => void endOthers()}>
          {busy ? "Signing out…" : "Sign out everywhere else"}
        </button>
      </div>
      {sentence !== null && (
        <p className="sentence" role="status">
          {sentence}
        </p>
      )}
      {problem !== null && <ThreePartError parts={problem} />}
    </section>
  );
}
