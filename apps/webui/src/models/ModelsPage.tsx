import { useCallback, useEffect, useMemo, useState } from "react";

import { asApiError } from "../api/http";
import { Field } from "../components/Field";
import { ThreePartError } from "../components/ThreePartError";
import { models as copy, type ThreePart } from "../copy/en";
import {
  type FetchRecordView,
  isRunning,
  type ModelsApi,
  problemParts,
  type RegistryView,
  ROLES,
  type RolesChange,
} from "./api";

// Models (docs/ui/models.md; CLAUDE.md §7, §9): the registry sentence, "Add a model" from a
// pasted link (ADR-0018) with one progress line per download, one card per model, and the
// Roles and Voters panels as a form saved through the model manager (INV-9). A registry
// problem is shown in three parts instead of an empty page. One primary action per panel.

interface Props {
  api: ModelsApi;
  /** Whether the person holds model:manage; without it the forms are disabled and say why. */
  canManage?: boolean;
  /** How often the downloads are polled while one is running. */
  pollMs?: number;
}

const GIB = 1024 ** 3;

function gib(bytes: number): string {
  return `${(bytes / GIB).toFixed(1)} GiB`;
}

interface RolesDraft {
  roles: Record<string, string>;
  voters: string[];
}

function draftOf(view: RegistryView): RolesDraft {
  return { roles: { ...view.roles }, voters: [...view.voters] };
}

/** What differs between the draft and the saved registry, as `PUT /models/roles` takes it. */
function changesOf(draft: RolesDraft, saved: RegistryView): RolesChange {
  const roles: Record<string, string | null> = {};
  for (const role of ROLES) {
    const now = draft.roles[role];
    const was = saved.roles[role];
    if (now !== was) {
      roles[role] = now ?? null;
    }
  }
  const change: RolesChange = {};
  if (Object.keys(roles).length > 0) {
    change.roles = roles;
  }
  const votersChanged = draft.voters.length !== saved.voters.length || draft.voters.some((v, i) => saved.voters[i] !== v);
  if (votersChanged) {
    change.voters = [...draft.voters];
  }
  return change;
}

export function ModelsPage({ api, canManage = true, pollMs = 3000 }: Props) {
  const [view, setView] = useState<RegistryView | null>(null);
  const [loadProblem, setLoadProblem] = useState<ThreePart | null>(null);

  const [fetches, setFetches] = useState<FetchRecordView[]>([]);
  const [link, setLink] = useState("");
  const [wantedId, setWantedId] = useState("");
  const [starting, setStarting] = useState(false);
  const [fetchProblem, setFetchProblem] = useState<ThreePart | null>(null);
  const [fetchNotice, setFetchNotice] = useState<string | null>(null);

  const [draft, setDraft] = useState<RolesDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveProblem, setSaveProblem] = useState<ThreePart | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadProblem(null);
    try {
      const fresh = await api.registry();
      setView(fresh);
      setDraft(draftOf(fresh));
    } catch (error: unknown) {
      setLoadProblem(asApiError(error).describe(copy.failedToLoad));
    }
  }, [api]);

  const refreshFetches = useCallback(async (): Promise<FetchRecordView[]> => {
    try {
      const records = await api.fetches();
      setFetches(records);
      return records;
    } catch {
      // The fetcher is optional (prod has none): the page keeps what it had.
      return [];
    }
  }, [api]);

  useEffect(() => {
    void load();
    void refreshFetches();
  }, [load, refreshFetches]);

  const anyRunning = fetches.some(isRunning);

  // Live progress (CLAUDE.md §9): poll while a download runs, and when one lands, re-read
  // the registry so its card appears.
  useEffect(() => {
    if (!anyRunning) {
      return undefined;
    }
    const timer = setInterval(() => {
      void refreshFetches().then((records) => {
        if (!records.some(isRunning) || records.some((r) => r.state === "done")) {
          void load();
        }
      });
    }, pollMs);
    return () => clearInterval(timer);
  }, [anyRunning, pollMs, refreshFetches, load]);

  const nameOf = (id: string) => view?.models.find((m) => m.id === id)?.display_name ?? id;
  const familyOf = (id: string) => view?.models.find((m) => m.id === id)?.family ?? "";
  const presentModels = useMemo(() => view?.models.filter((m) => m.present) ?? [], [view]);
  const changes = view !== null && draft !== null ? changesOf(draft, view) : {};
  const dirty = Object.keys(changes).length > 0;
  const draftFamilies = draft === null ? 0 : new Set(draft.voters.map(familyOf)).size;

  async function startFetch(event: React.FormEvent) {
    event.preventDefault();
    setFetchProblem(null);
    setFetchNotice(null);
    setStarting(true);
    try {
      const record = await api.startFetch(link.trim(), wantedId);
      setFetches((current) => [record, ...current.filter((r) => r.id !== record.id)]);
      setLink("");
      setWantedId("");
    } catch (error: unknown) {
      setFetchProblem(asApiError(error).describe(copy.add.notStarted));
    } finally {
      setStarting(false);
    }
  }

  async function cancelOrRemove(record: FetchRecordView) {
    setFetchProblem(null);
    try {
      const sentence = await api.cancelFetch(record.id);
      setFetchNotice(sentence);
      await refreshFetches();
    } catch (error: unknown) {
      setFetchProblem(asApiError(error).describe(copy.add.notCancelled));
    }
  }

  async function saveRoles(event: React.FormEvent) {
    event.preventDefault();
    if (!dirty) {
      return;
    }
    setSaveProblem(null);
    setSaved(null);
    setSaving(true);
    try {
      const result = await api.saveRoles(changes);
      setSaved(result.sentence);
      await load();
    } catch (error: unknown) {
      setSaveProblem(asApiError(error).describe(copy.edit.notSaved));
    } finally {
      setSaving(false);
    }
  }

  const setRole = (role: string, modelId: string) => {
    if (draft === null) {
      return;
    }
    const roles = { ...draft.roles };
    if (modelId === "") {
      delete roles[role];
    } else {
      roles[role] = modelId;
    }
    setDraft({ ...draft, roles });
    setSaved(null);
  };

  const toggleVoter = (modelId: string) => {
    if (draft === null) {
      return;
    }
    const voters = draft.voters.includes(modelId) ? draft.voters.filter((v) => v !== modelId) : [...draft.voters, modelId];
    setDraft({ ...draft, voters });
    setSaved(null);
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>{copy.heading}</h1>
          <p className="lede">{copy.lede}</p>
        </div>
      </div>
      <div className="stack">
        {view === null && loadProblem === null && <p className="muted">{copy.loading}</p>}
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
        {view?.problem != null && <ThreePartError parts={problemParts(view.problem)} />}
        {view !== null && view.sentence !== "" && (
          <p className="sentence" data-testid="registry-sentence">
            {view.sentence}
          </p>
        )}

        {view !== null && (
          <form className="panel stack" aria-labelledby="add-model-heading" onSubmit={(event) => void startFetch(event)}>
            <h2 id="add-model-heading">{copy.add.heading}</h2>
            <Field label={copy.add.linkLabel}>
              {(control) => (
                <input
                  {...control}
                  className="input"
                  placeholder={copy.add.linkPlaceholder}
                  value={link}
                  disabled={!canManage || starting}
                  onChange={(event) => setLink(event.target.value)}
                />
              )}
            </Field>
            <Field label={copy.add.idLabel} help={copy.add.idHelp}>
              {(control) => (
                <input
                  {...control}
                  className="input"
                  value={wantedId}
                  disabled={!canManage || starting}
                  onChange={(event) => setWantedId(event.target.value)}
                />
              )}
            </Field>
            <p className="sentence">{copy.add.whatHappens}</p>
            {!canManage && <p className="muted">{copy.add.notAllowed}</p>}
            <div className="row">
              <button type="submit" className="btn primary" disabled={!canManage || starting || link.trim() === ""}>
                {starting ? copy.add.starting : copy.add.button}
              </button>
            </div>
            {fetchProblem !== null && <ThreePartError parts={fetchProblem} />}
            {fetchNotice !== null && (
              <p role="status" className="muted">
                {fetchNotice}
              </p>
            )}
            {fetches.length > 0 && (
              <section aria-labelledby="fetches-heading">
                <h3 id="fetches-heading">{copy.add.fetchesHeading}</h3>
                <ul className="plain lines">
                  {fetches.map((record) => (
                    <li key={record.id} aria-label={`${record.display_name} download`}>
                      <div className="row between">
                        <strong>{record.display_name}</strong>
                        <span className={"pill " + (record.state === "done" ? "pass" : record.state === "failed" ? "fail" : isRunning(record) ? "warn" : "info")}>
                          {copy.add.state[record.state] ?? record.state}
                        </span>
                      </div>
                      {isRunning(record) && record.bytes_total > 0 && (
                        <div
                          className="bar"
                          role="progressbar"
                          aria-valuemin={0}
                          aria-valuemax={record.bytes_total}
                          aria-valuenow={record.bytes_done}
                          aria-label={copy.add.progressLabel(gib(record.bytes_done), gib(record.bytes_total))}
                        >
                          <i style={{ width: `${Math.round((record.bytes_done / record.bytes_total) * 100)}%` }} />
                        </div>
                      )}
                      {record.state === "failed" && record.problem !== null ? (
                        <ThreePartError parts={problemParts(record.problem)} />
                      ) : (
                        <p className="sentence">{record.sentence}</p>
                      )}
                      {canManage && (
                        <div className="row">
                          <button type="button" className="btn small" onClick={() => void cancelOrRemove(record)}>
                            {isRunning(record) ? copy.add.cancel : copy.add.remove}
                          </button>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </form>
        )}

        {view !== null && view.problem == null && draft !== null && (
          <>
            <section className="panel" aria-labelledby="models-heading">
              <h2 id="models-heading">{copy.modelsHeading}</h2>
              {view.models.length === 0 ? (
                <p className="muted">{copy.noModels}</p>
              ) : (
                <ul className="plain cards">
                  {view.models.map((model) => (
                    <li key={model.id} className="card" aria-label={model.display_name}>
                      <div className="row between">
                        <h3>{model.display_name}</h3>
                        <span className={model.present ? "pill pass" : "pill warn"}>{model.present ? copy.present : copy.notPresent}</span>
                      </div>
                      <p className="muted">
                        {copy.family(model.family)} · {copy.quant[model.quant] ?? model.quant}
                      </p>
                      <p>
                        {copy.vram(model.vram_gib)} · {copy.context(model.context)}
                      </p>
                      <p className="muted">{copy.roles(model.roles)}</p>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <form className="stack" onSubmit={(event) => void saveRoles(event)} aria-labelledby="roles-heading">
              <div className="cols cols-2">
                <section className="panel" aria-labelledby="roles-heading">
                  <h2 id="roles-heading">{copy.rolesHeading}</h2>
                  {view.models.length === 0 ? (
                    <p className="muted">{copy.noRoles}</p>
                  ) : (
                    <div className="stack">
                      {ROLES.map((role) => (
                        <Field key={role} label={role}>
                          {(control) => (
                            <select {...control} className="input" value={draft.roles[role] ?? ""} disabled={!canManage || saving} onChange={(event) => setRole(role, event.target.value)}>
                              <option value="">{copy.edit.noModel}</option>
                              {view.models.map((model) => (
                                <option key={model.id} value={model.id} disabled={!model.present}>
                                  {model.present ? model.display_name : copy.edit.notHere(model.display_name)}
                                </option>
                              ))}
                            </select>
                          )}
                        </Field>
                      ))}
                    </div>
                  )}
                </section>
                <section className="panel" aria-labelledby="voters-heading">
                  <h2 id="voters-heading">{copy.votersHeading}</h2>
                  {presentModels.length === 0 ? (
                    <p className="muted">{copy.noVoters}</p>
                  ) : (
                    <fieldset className="choices">
                      <legend className="sentence">{draft.voters.length === 0 ? copy.noVoters : copy.voterCount(draft.voters.length, draftFamilies)}</legend>
                      {presentModels.map((model) => (
                        <label key={model.id} className="choice">
                          <input type="checkbox" checked={draft.voters.includes(model.id)} disabled={!canManage || saving} onChange={() => toggleVoter(model.id)} />
                          <span>{copy.voterLine(nameOf(model.id), model.family)}</span>
                        </label>
                      ))}
                      <small className="help">{copy.edit.voterHelp}</small>
                    </fieldset>
                  )}
                </section>
              </div>
              {!canManage && <p className="muted">{copy.edit.notAllowed}</p>}
              <div className="row">
                <button type="submit" className="btn primary" disabled={!canManage || saving || !dirty}>
                  {saving ? copy.edit.saving : copy.edit.save}
                </button>
                {saved !== null && (
                  <span role="status" className="sentence">
                    {saved}
                  </span>
                )}
              </div>
              {saveProblem !== null && <ThreePartError parts={saveProblem} />}
            </form>
            <p className="muted">{copy.footer}</p>
          </>
        )}
      </div>
    </div>
  );
}
