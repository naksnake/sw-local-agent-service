import { useEffect, useState } from "react";

import {
  type RetentionView,
  retentionSentence,
  type ScreenTuningView,
  type StationRecordView,
  type StationsAdminApi,
  tuningSentence,
  type WindowMatch,
} from "./api";

// Admin → Stations (CLAUDE.md §5.2, §10.3, P10): the stations the Factory Agent may drive,
// their one-time enrolment codes, the per-station window matching and timing knobs, the
// screenshot retention, and whether the operator can watch over VNC. Copy: docs/ui/admin-stations.md.
// Nothing here shows a key or a certificate beyond its fingerprint.

interface Props {
  api: StationsAdminApi;
  /** Who is signed in; recorded on every issued code. */
  user?: string;
}

const field = "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900";
const small = "mt-1 w-28 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900";
const primary = "rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40 dark:bg-slate-100 dark:text-slate-900";
const secondary = "rounded-md border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700";

export function StationsAdmin({ api, user = "you" }: Props) {
  const [records, setRecords] = useState<StationRecordView[] | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [codes, setCodes] = useState<Record<string, string>>({});
  const [notices, setNotices] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [draftScreen, setDraftScreen] = useState<ScreenTuningView | null>(null);
  const [draftRetention, setDraftRetention] = useState<RetentionView | null>(null);
  const [draftVnc, setDraftVnc] = useState(true);

  useEffect(() => {
    void api.listStationRecords().then(setRecords);
  }, [api]);

  function replace(record: StationRecordView) {
    setRecords((current) => (current ?? []).map((r) => (r.name === record.name ? record : r)));
  }

  async function add() {
    setProblem(null);
    const result = await api.addStation(name.trim().toLowerCase(), description.trim());
    if (typeof result === "string") {
      setProblem(result);
      return;
    }
    setRecords((current) => [...(current ?? []), result]);
    setNotices((n) => ({ ...n, [result.name]: `${result.name} is added. Issue its code, then enter the code on the station.` }));
    setName("");
    setDescription("");
  }

  async function issue(station: string) {
    const issued = await api.issueCode(station, user);
    setCodes((c) => ({ ...c, [station]: issued.sentence }));
  }

  async function revoke(station: string) {
    const record = await api.revoke(station);
    replace(record);
    setCodes((c) => ({ ...c, [station]: "" }));
    setNotices((n) => ({
      ...n,
      [station]: `${station} is no longer enrolled: its certificate and batch key are refused from now on. Issue a new code to enrol it again.`,
    }));
  }

  async function remove(station: string) {
    await api.removeStation(station);
    setRecords((current) => (current ?? []).filter((r) => r.name !== station));
  }

  function startEditing(record: StationRecordView) {
    setEditing(record.name);
    setDraftScreen({ ...record.screen });
    setDraftRetention({ ...record.retention });
    setDraftVnc(record.vncEnabled);
  }

  async function save(station: string) {
    if (draftScreen === null || draftRetention === null) {
      return;
    }
    const record = await api.saveTuning(station, draftScreen, draftRetention, draftVnc);
    replace(record);
    setEditing(null);
    setNotices((n) => ({
      ...n,
      [station]: `${station} saved. ${tuningSentence(record.screen)} ${retentionSentence(record.retention)} The station picks the change up on its next enrolment or restart.`,
    }));
  }

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Stations</h1>
        <p className="mt-1 text-base text-slate-700 dark:text-slate-300">
          The test stations the Factory Agent may drive. A station joins by entering a one-time code
          you issue here; it then holds its own certificate and signing key, and the platform never
          sees its display — only screenshots and results. Changes apply as soon as you save; nothing
          is restarted here.
        </p>
      </header>

      <section aria-labelledby="stations-heading">
        <h2 id="stations-heading" className="text-lg font-medium">
          Stations
        </h2>
        {records === null ? (
          <p className="text-sm">Loading stations…</p>
        ) : records.length === 0 ? (
          <p className="text-sm text-slate-600 dark:text-slate-400">No station yet. Add one below, then issue its code.</p>
        ) : (
          <ul className="mt-2 space-y-3">
            {records.map((record) => (
              <li key={record.name} className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-800" aria-label={record.name}>
                <p>
                  <span className="font-medium">{record.name}</span>
                  {record.description ? <span className="text-slate-500"> · {record.description}</span> : null}
                </p>
                <p data-testid={`${record.name}-sentence`}>{record.sentence}</p>
                <p className="text-slate-600 dark:text-slate-400">
                  {tuningSentence(record.screen)} {retentionSentence(record.retention)}{" "}
                  {record.vncEnabled
                    ? `The operator can watch and take over through VNC (port ${record.vncPort}).`
                    : "VNC is off: the operator cannot watch or take over this station."}
                </p>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button type="button" className={primary} aria-label={`Issue code ${record.name}`} onClick={() => void issue(record.name)}>
                    {record.enrolled ? "Issue a new code" : "Issue code"}
                  </button>
                  <button type="button" className={secondary} aria-label={`Tune ${record.name}`} onClick={() => startEditing(record)}>
                    Tune
                  </button>
                  {record.enrolled && (
                    <button type="button" className={secondary} aria-label={`Revoke ${record.name}`} onClick={() => void revoke(record.name)}>
                      Revoke
                    </button>
                  )}
                  {!record.enrolled && (
                    <button type="button" className={`${secondary} text-red-700 dark:text-red-300`} aria-label={`Remove ${record.name}`} onClick={() => void remove(record.name)}>
                      Remove
                    </button>
                  )}
                </div>
                {codes[record.name] ? (
                  <p role="status" className="mt-2 rounded-md bg-amber-50 p-2 font-medium dark:bg-amber-950" data-testid={`${record.name}-code`}>
                    {codes[record.name]}
                  </p>
                ) : null}
                {notices[record.name] ? (
                  <p role="status" className="mt-2" data-testid={`${record.name}-notice`}>
                    {notices[record.name]}
                  </p>
                ) : null}
                {editing === record.name && draftScreen !== null && draftRetention !== null && (
                  <div className="mt-3 space-y-3 rounded-md bg-slate-100 p-3 dark:bg-slate-900" aria-label={`Tuning ${record.name}`}>
                    <p>
                      Window matching and timing for this station's GUI. Tune against the real station with
                      <code className="mx-1">slas-station-runner windows</code>; the skill itself does not change.
                    </p>
                    <div className="flex flex-wrap gap-4">
                      <label className="block">
                        Window titles match by
                        <select
                          aria-label={`Window match ${record.name}`}
                          className={field}
                          value={draftScreen.windowMatch}
                          onChange={(e) => setDraftScreen({ ...draftScreen, windowMatch: e.target.value as WindowMatch })}
                        >
                          <option value="contains">contains (the recipe's title appears anywhere)</option>
                          <option value="prefix">prefix (the real title starts with it)</option>
                          <option value="exact">exact (the whole title, case-insensitive)</option>
                          <option value="regex">regular expression</option>
                        </select>
                      </label>
                      <label className="block">
                        Settle after each action (s)
                        <input aria-label={`Settle ${record.name}`} type="number" min={0} max={10} step={0.1} className={small} value={draftScreen.actionSettleS} onChange={(e) => setDraftScreen({ ...draftScreen, actionSettleS: Number(e.target.value) })} />
                      </label>
                      <label className="block">
                        Wait timeouts ×
                        <input aria-label={`Timeout scale ${record.name}`} type="number" min={0.1} max={10} step={0.1} className={small} value={draftScreen.waitTimeoutScale} onChange={(e) => setDraftScreen({ ...draftScreen, waitTimeoutScale: Number(e.target.value) })} />
                      </label>
                      <label className="block">
                        Actions per second, at most
                        <input aria-label={`Actions per second ${record.name}`} type="number" min={1} max={60} className={small} value={draftScreen.maxActionsPerSecond} onChange={(e) => setDraftScreen({ ...draftScreen, maxActionsPerSecond: Number(e.target.value) })} />
                      </label>
                    </div>
                    <p>Screenshot retention on the platform and on this station.</p>
                    <div className="flex flex-wrap gap-4">
                      <label className="block">
                        Keep for (days)
                        <input aria-label={`Keep days ${record.name}`} type="number" min={1} max={3650} className={small} value={draftRetention.keepDays} onChange={(e) => setDraftRetention({ ...draftRetention, keepDays: Number(e.target.value) })} />
                      </label>
                      <label className="block">
                        Failed or held jobs (days)
                        <input aria-label={`Keep failed days ${record.name}`} type="number" min={1} max={3650} className={small} value={draftRetention.keepFailedDays} onChange={(e) => setDraftRetention({ ...draftRetention, keepFailedDays: Number(e.target.value) })} />
                      </label>
                      <label className="block">
                        At most per job
                        <input aria-label={`Max per job ${record.name}`} type="number" min={10} max={100000} className={small} value={draftRetention.maxPerJob} onChange={(e) => setDraftRetention({ ...draftRetention, maxPerJob: Number(e.target.value) })} />
                      </label>
                    </div>
                    <label className="flex items-center gap-2">
                      <input aria-label={`VNC ${record.name}`} type="checkbox" checked={draftVnc} onChange={(e) => setDraftVnc(e.target.checked)} />
                      The operator can watch and take over through VNC
                    </label>
                    <div className="flex gap-2">
                      <button type="button" className={primary} aria-label={`Save tuning ${record.name}`} onClick={() => void save(record.name)}>
                        Save
                      </button>
                      <button type="button" className={secondary} onClick={() => setEditing(null)}>
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="add-station-heading" className="space-y-3">
        <h2 id="add-station-heading" className="text-lg font-medium">
          Add a station
        </h2>
        <p className="text-sm text-slate-700 dark:text-slate-300">
          Adding a station creates its record; nothing reaches the station until you issue a code and
          someone enters it there.
        </p>
        <label className="block text-sm">
          Name
          <input aria-label="Station name" className={field} placeholder="station-09" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block text-sm">
          Description (optional)
          <input aria-label="Station description" className={field} placeholder="Final test, line 2" value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <button type="button" className={primary} disabled={name.trim() === ""} onClick={() => void add()}>
          Add station
        </button>
        {problem !== null && (
          <p role="alert" className="text-sm text-red-700 dark:text-red-300">
            {problem}
          </p>
        )}
      </section>
    </div>
  );
}
