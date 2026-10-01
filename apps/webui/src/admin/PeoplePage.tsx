import { useCallback, useEffect, useState } from "react";

import { asApiError } from "../api/http";
import { Dialog } from "../components/Dialog";
import { Field } from "../components/Field";
import { ThreePartError } from "../components/ThreePartError";
import { DEFAULT_ROLE, people as copy, roleInfo, ROLES, type ThreePart } from "../copy/en";
import type { Person } from "../session/api";
import { humanTime } from "../time";
import type { PeopleApi } from "./api";

// Admin → People (docs/ui/admin-people.md). One primary action (Add person); every other
// action lives in the row menu. One dialog per action whose content advances from
// confirmation to result; dialogs never stack. A one-time password appears once, in the
// result panel, and nowhere else.

interface Props {
  api: PeopleApi;
  /** The signed-in person, so the page can refuse "switch off yourself" before asking the api. */
  me: Person | null;
}

type DialogState =
  | { kind: "add" }
  | { kind: "role"; person: Person }
  | { kind: "reset"; person: Person }
  | { kind: "active"; person: Person; on: boolean }
  | { kind: "result"; name: string; password: string };

export function statusWord(person: Person): string {
  if (!person.is_active) {
    return copy.status.switchedOff;
  }
  if (person.must_change_password) {
    return copy.status.mustChoose;
  }
  return copy.status.canSignIn;
}

export function PeoplePage({ api, me }: Props) {
  const [people, setPeople] = useState<Person[] | null>(null);
  const [loadProblem, setLoadProblem] = useState<ThreePart | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadProblem(null);
    try {
      setPeople(await api.list());
    } catch (error: unknown) {
      setPeople(null);
      setLoadProblem(asApiError(error).describe(copy.failedToLoad));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const replace = (updated: Person) =>
    setPeople((current) => (current ?? []).map((p) => (p.id === updated.id ? updated : p)));
  const close = useCallback(() => setDialog(null), []);

  const onlyYou = people !== null && people.length === 1 && (me === null || people[0]?.id === me.id);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>{copy.heading}</h1>
          <p className="lede">{copy.lede}</p>
        </div>
        <button type="button" className="btn primary" onClick={() => setDialog({ kind: "add" })}>
          {copy.addButton}
        </button>
      </div>

      <div className="stack">
        {people === null && loadProblem === null && <p className="muted">{copy.loading}</p>}
        {loadProblem !== null && (
          <ThreePartError
            parts={loadProblem}
            action={
              <button type="button" className="btn small" onClick={() => void load()}>
                {copy.tryAgain}
              </button>
            }
          />
        )}
        {onlyYou && <p className="sentence">{copy.onlyYou}</p>}
        {people !== null && (
          <section className="panel tight">
            <table className="list">
              <thead>
                <tr>
                  {copy.columns.map((column) => (
                    <th key={column}>{column}</th>
                  ))}
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {people.map((person) => (
                  <tr key={person.id} aria-label={person.display_name}>
                    <td>
                      <div className="title">{person.display_name}</div>
                      <div className="muted">{person.email}</div>
                    </td>
                    <td>{person.role_label}</td>
                    <td className="muted">{roleInfo(person.role).sentence}</td>
                    <td>{humanTime(person.last_sign_in_at)}</td>
                    <td>{statusWord(person)}</td>
                    <td className="menu-cell">
                      <button
                        type="button"
                        className="btn small ghost"
                        aria-label={copy.menu.label(person.display_name)}
                        aria-haspopup="menu"
                        aria-expanded={menuFor === person.id}
                        onClick={() => setMenuFor((current) => (current === person.id ? null : person.id))}
                      >
                        ···
                      </button>
                      {menuFor === person.id && (
                        <div role="menu" className="menu" aria-label={copy.menu.label(person.display_name)}>
                          <button
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              setMenuFor(null);
                              setDialog({ kind: "role", person });
                            }}
                          >
                            {copy.menu.changeRole}
                          </button>
                          <button
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              setMenuFor(null);
                              setDialog({ kind: "reset", person });
                            }}
                          >
                            {copy.menu.resetPassword}
                          </button>
                          <button
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              setMenuFor(null);
                              setDialog({ kind: "active", person, on: !person.is_active });
                            }}
                          >
                            {person.is_active ? copy.menu.switchOff : copy.menu.switchOn}
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}
      </div>

      {dialog?.kind === "add" && (
        <AddPersonDialog
          api={api}
          onClose={close}
          onAdded={(person, password) => {
            setPeople((current) => [...(current ?? []), person].sort((a, b) => a.display_name.localeCompare(b.display_name)));
            setDialog({ kind: "result", name: person.display_name, password });
          }}
        />
      )}
      {dialog?.kind === "role" && (
        <ChangeRoleDialog
          api={api}
          person={dialog.person}
          onClose={close}
          onChanged={(updated) => {
            replace(updated);
            close();
          }}
        />
      )}
      {dialog?.kind === "reset" && (
        <ResetPasswordDialog
          api={api}
          person={dialog.person}
          onClose={close}
          onReset={(password) => {
            replace({ ...dialog.person, must_change_password: true });
            setDialog({ kind: "result", name: dialog.person.display_name, password });
          }}
        />
      )}
      {dialog?.kind === "active" && (
        <SwitchDialog
          api={api}
          person={dialog.person}
          on={dialog.on}
          isMe={me !== null && me.id === dialog.person.id}
          onClose={close}
          onSwitched={(updated) => {
            replace(updated);
            close();
          }}
        />
      )}
      {dialog?.kind === "result" && <OneTimePasswordPanel name={dialog.name} password={dialog.password} onDone={close} />}
    </div>
  );
}

// --- dialogs ---------------------------------------------------------------------------------

function RoleChoices({ value, onChange, name }: { value: string; onChange: (role: string) => void; name: string }) {
  return (
    <fieldset className="choices">
      <legend>{copy.add.role}</legend>
      {ROLES.map((role) => (
        <label key={role.id} className="choice">
          <input type="radio" name={name} value={role.id} checked={value === role.id} onChange={() => onChange(role.id)} />
          <span>
            <span className="title">{role.label}</span>
            <span className="muted">{role.sentence}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

function AddPersonDialog({
  api,
  onClose,
  onAdded,
}: {
  api: PeopleApi;
  onClose: () => void;
  onAdded: (person: Person, password: string) => void;
}) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState(DEFAULT_ROLE);
  const [waiting, setWaiting] = useState(false);
  const [problem, setProblem] = useState<ThreePart | null>(null);
  const shown = name.trim();

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setProblem(null);
    setWaiting(true);
    try {
      const added = await api.add({ email: email.trim(), display_name: shown, role });
      onAdded(added.person, added.one_time_password);
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(copy.add.notAdded(shown)));
    } finally {
      setWaiting(false);
    }
  }

  return (
    <Dialog title={copy.add.title} onClose={onClose}>
      {/* noValidate: the api's own sentence for a bad address, not a browser bubble (§9). */}
      <form className="stack" noValidate onSubmit={(event) => void submit(event)}>
        <Field label={copy.add.name} help={copy.add.nameHelp}>
          {(control) => <input {...control} className="input" required value={name} onChange={(event) => setName(event.target.value)} />}
        </Field>
        <Field label={copy.add.email} help={copy.add.emailHelp}>
          {(control) => (
            <input {...control} className="input" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} />
          )}
        </Field>
        <RoleChoices name="add-role" value={role} onChange={setRole} />
        {shown !== "" && <p className="sentence">{copy.add.closing(shown)}</p>}
        <div className="row">
          <button type="submit" className="btn primary" disabled={waiting || shown === "" || email.trim() === ""}>
            {waiting ? copy.add.buttonWaiting : copy.add.button(shown)}
          </button>
          <button type="button" className="btn ghost" onClick={onClose}>
            {copy.cancel}
          </button>
        </div>
        {problem !== null && <ThreePartError parts={problem} />}
      </form>
    </Dialog>
  );
}

function ChangeRoleDialog({
  api,
  person,
  onClose,
  onChanged,
}: {
  api: PeopleApi;
  person: Person;
  onClose: () => void;
  onChanged: (person: Person) => void;
}) {
  const [role, setRole] = useState(person.role);
  const [waiting, setWaiting] = useState(false);
  const [problem, setProblem] = useState<ThreePart | null>(null);
  const name = person.display_name;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setProblem(null);
    setWaiting(true);
    try {
      onChanged(await api.changeRole(person.id, role));
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(copy.changeRole.notChanged(name)));
    } finally {
      setWaiting(false);
    }
  }

  return (
    <Dialog title={copy.changeRole.title(name)} onClose={onClose}>
      <form className="stack" onSubmit={(event) => void submit(event)}>
        <RoleChoices name="change-role" value={role} onChange={setRole} />
        <p className="sentence">{copy.changeRole.closing(name, roleInfo(role).label)}</p>
        <div className="row">
          <button type="submit" className="btn primary" disabled={waiting || role === person.role}>
            {waiting ? copy.changeRole.buttonWaiting : copy.changeRole.button}
          </button>
          <button type="button" className="btn ghost" onClick={onClose}>
            {copy.cancel}
          </button>
        </div>
        {problem !== null && <ThreePartError parts={problem} />}
      </form>
    </Dialog>
  );
}

function ResetPasswordDialog({
  api,
  person,
  onClose,
  onReset,
}: {
  api: PeopleApi;
  person: Person;
  onClose: () => void;
  onReset: (password: string) => void;
}) {
  const [waiting, setWaiting] = useState(false);
  const [problem, setProblem] = useState<ThreePart | null>(null);
  const name = person.display_name;

  async function reset() {
    setProblem(null);
    setWaiting(true);
    try {
      onReset((await api.resetPassword(person.id)).one_time_password);
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(copy.resetPassword.notReset(name)));
    } finally {
      setWaiting(false);
    }
  }

  return (
    <Dialog title={copy.resetPassword.title(name)} onClose={onClose}>
      <div className="stack">
        <p className="sentence">{copy.resetPassword.body(name)}</p>
        <div className="row">
          <button type="button" className="btn primary" disabled={waiting} onClick={() => void reset()}>
            {waiting ? copy.resetPassword.buttonWaiting : copy.resetPassword.button}
          </button>
          <button type="button" className="btn ghost" onClick={onClose}>
            {copy.cancel}
          </button>
        </div>
        {problem !== null && <ThreePartError parts={problem} />}
      </div>
    </Dialog>
  );
}

function SwitchDialog({
  api,
  person,
  on,
  isMe,
  onClose,
  onSwitched,
}: {
  api: PeopleApi;
  person: Person;
  on: boolean;
  isMe: boolean;
  onClose: () => void;
  onSwitched: (person: Person) => void;
}) {
  const [waiting, setWaiting] = useState(false);
  const [problem, setProblem] = useState<ThreePart | null>(null);
  const name = person.display_name;
  const words = on ? copy.switchOn : copy.switchOff;

  async function apply() {
    setProblem(null);
    if (!on && isMe) {
      setProblem(copy.switchOff.yourself);
      return;
    }
    setWaiting(true);
    try {
      onSwitched(await api.setActive(person.id, on));
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(words.notSwitched(name)));
    } finally {
      setWaiting(false);
    }
  }

  return (
    <Dialog title={words.title(name)} onClose={onClose}>
      <div className="stack">
        <p className="sentence">{words.body(name)}</p>
        <div className="row">
          <button type="button" className={on ? "btn primary" : "btn danger"} disabled={waiting} onClick={() => void apply()}>
            {waiting ? words.buttonWaiting : words.button}
          </button>
          <button type="button" className="btn ghost" onClick={onClose}>
            {copy.cancel}
          </button>
        </div>
        {problem !== null && <ThreePartError parts={problem} />}
      </div>
    </Dialog>
  );
}

function OneTimePasswordPanel({ name, password, onDone }: { name: string; password: string; onDone: () => void }) {
  const [copied, setCopied] = useState(false);

  async function copyIt() {
    try {
      await navigator.clipboard.writeText(password);
      setCopied(true);
    } catch {
      // The clipboard is not available (no secure context or permission); the password stays
      // visible in the panel and the person types it over.
      setCopied(false);
    }
  }

  return (
    <Dialog title={copy.result.title(name)}>
      <div className="stack">
        <p className="sentence">{copy.result.body(name)}</p>
        <output className="otp mono" aria-label={copy.result.passwordLabel}>
          {password}
        </output>
        <div className="row">
          <button type="button" className="btn" onClick={() => void copyIt()}>
            {copied ? copy.result.copied : copy.result.copy}
          </button>
          <button type="button" className="btn primary" onClick={onDone}>
            {copy.result.done}
          </button>
        </div>
      </div>
    </Dialog>
  );
}
