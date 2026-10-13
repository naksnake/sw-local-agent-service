import { useCallback, useEffect, useRef, useState } from "react";

import { asApiError } from "../api/http";
import { Dialog } from "../components/Dialog";
import { Field } from "../components/Field";
import { ThreePartError } from "../components/ThreePartError";
import type { ThreePart } from "../copy/en";
import {
  AGENT_LABEL,
  BLANK_SKILL,
  type SkillAgent,
  type SkillCheck,
  type SkillDetail,
  type SkillExample,
  type SkillRisk,
  type SkillRow,
  type SkillsApi,
} from "./api";

// The Skills page (CLAUDE.md §5.6, §6, §9; copy in docs/ui/skills.md). One primary action,
// New skill. The library lists every imported skill with its risk and one switch per agent
// its author allows; a skill opens in place to show its steps. The editor checks a skill
// file with the importer before anything is saved, so the person reads what it will do and
// what it needs first. Shipped examples can be added as they are or used as a start.
// Skills are data (INV-12): the page never interprets a skill; the orchestrator does.

interface Props {
  api: SkillsApi;
}

type Editor = { title: string; yaml: string };

const NOT_ANSWERING = (what: string): ThreePart => ({
  whatHappened: what,
  likelyCause: "The orchestrator didn't answer.",
  whatToDo: "Try again; if it repeats, run `slas logs agent-core-orchestrator` on the host.",
});

const RISK_TONE: Record<SkillRisk, string> = { safe: "pill pass", caution: "pill warn", destructive: "pill fail" };
const RISK_WORD: Record<SkillRisk, string> = {
  safe: "Safe",
  caution: "Caution: runs commands",
  destructive: "Destructive: asks for approval",
};

function agentsSentence(agents: SkillAgent[]): string {
  return `For the ${agents.map((a) => AGENT_LABEL[a]).join(", the ")}`;
}

/** Saves a text as a file in the browser; nothing leaves the network. */
function download(name: string, text: string): void {
  if (typeof URL.createObjectURL !== "function") {
    return;
  }
  const url = URL.createObjectURL(new Blob([text], { type: "application/yaml" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

export function SkillsPage({ api }: Props) {
  const [rows, setRows] = useState<SkillRow[] | null>(null);
  const [examples, setExamples] = useState<SkillExample[]>([]);
  const [loadProblem, setLoadProblem] = useState<ThreePart | null>(null);
  const [problem, setProblem] = useState<ThreePart | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [deleting, setDeleting] = useState<SkillRow | null>(null);
  const file = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setLoadProblem(null);
    try {
      const [library, shipped] = await Promise.all([api.list(), api.examples()]);
      setRows(library);
      setExamples(shipped);
    } catch (error: unknown) {
      setLoadProblem(asApiError(error).describe(NOT_ANSWERING("The skill library didn't load.")));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const open = async (id: string) => {
    if (openId === id) {
      setOpenId(null);
      return;
    }
    setOpenId(id);
    setDetail(null);
    try {
      setDetail(await api.detail(id));
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(NOT_ANSWERING("The skill didn't open.")));
    }
  };

  const toggle = async (row: SkillRow, agent: SkillAgent, on: boolean) => {
    setProblem(null);
    try {
      const updated = await api.setEnabled(row.id, agent, on);
      setRows((current) => (current ?? []).map((r) => (r.id === updated.id ? updated : r)));
      setNotice(`${updated.name}: ${updated.sentence}`);
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(NOT_ANSWERING(`${row.name} wasn't switched.`)));
    }
  };

  const exportOne = async (row: SkillRow) => {
    setProblem(null);
    try {
      const exported = await api.exportFile(row.id);
      download(`${row.id}.skill.yaml`, exported.yaml);
      setNotice(`${row.id}.skill.yaml is downloading. Import it on another installation under Skills.`);
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(NOT_ANSWERING(`${row.name} wasn't exported.`)));
    }
  };

  const addExample = async (example: SkillExample) => {
    setProblem(null);
    try {
      const saved = await api.save(example.yaml);
      setNotice(`${saved.sentence} ${saved.state_sentence} Switch it on below for the agents that should use it.`);
      await load();
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(NOT_ANSWERING(`${example.name} wasn't added.`)));
    }
  };

  const readFile = async (chosen: File | undefined) => {
    if (chosen === undefined) {
      return;
    }
    setEditor({ title: `Import ${chosen.name}`, yaml: await chosen.text() });
    if (file.current !== null) {
      file.current.value = "";
    }
  };

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Skills</h1>
          <p className="lede">
            Reusable recipes made only of whitelisted steps. Write one once, then switch it on for any agent it allows. A
            skill never gives anyone a capability they don't already hold.
          </p>
        </div>
        <div className="row">
          <label className="btn">
            Import a file
            <input
              ref={file}
              type="file"
              accept=".yaml,.yml"
              className="sr-only"
              aria-label="Skill file to import"
              onChange={(event) => void readFile(event.target.files?.[0])}
            />
          </label>
          {editor === null && (
            <button type="button" className="btn primary" onClick={() => setEditor({ title: "New skill", yaml: BLANK_SKILL })}>
              New skill
            </button>
          )}
        </div>
      </div>

      {notice !== null && (
        <p className="sentence" role="status">
          {notice}
        </p>
      )}
      {problem !== null && <ThreePartError parts={problem} />}
      {loadProblem !== null && (
        <ThreePartError
          parts={loadProblem}
          action={
            <button type="button" className="btn small" onClick={() => void load()}>
              Try again
            </button>
          }
        />
      )}

      {editor !== null && (
        <SkillEditor
          key={editor.title + editor.yaml.length}
          api={api}
          editor={editor}
          onCancel={() => setEditor(null)}
          onSaved={async (sentence) => {
            setEditor(null);
            setNotice(sentence);
            setOpenId(null);
            await load();
          }}
        />
      )}

      <section aria-labelledby="library-heading" className="stack">
        <h2 id="library-heading">Your library</h2>
        {rows === null && loadProblem === null && <p className="muted">Loading skills…</p>}
        {rows !== null && rows.length === 0 && (
          <section className="panel empty">
            <p className="sentence">
              No skill yet. Add one of the examples below, write one with New skill, or import a .skill.yaml file.
            </p>
          </section>
        )}
        {rows !== null && rows.length > 0 && (
          <section className="panel tight">
            <table className="list">
              <thead>
                <tr>
                  <th>Skill</th>
                  <th>Risk</th>
                  <th>Switched on for</th>
                  <th>Needs</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <SkillRowView
                    key={row.id}
                    row={row}
                    open={openId === row.id}
                    detail={openId === row.id ? detail : null}
                    onOpen={() => void open(row.id)}
                    onToggle={(agent, on) => void toggle(row, agent, on)}
                    onEdit={(yaml) => setEditor({ title: `Edit ${row.name}`, yaml })}
                    onExport={() => void exportOne(row)}
                    onDelete={() => setDeleting(row)}
                  />
                ))}
              </tbody>
            </table>
          </section>
        )}
      </section>

      {examples.length > 0 && (
        <section aria-labelledby="examples-heading" className="stack">
          <h2 id="examples-heading">Examples that ship with the platform</h2>
          <div className="cards">
            {examples.map((example) => (
              <article key={example.id} className="card" aria-label={example.name}>
                <div className="row between">
                  <strong>{example.name}</strong>
                  <span className={RISK_TONE[example.risk]}>{example.risk}</span>
                </div>
                {example.description !== "" && <span className="muted">{example.description}</span>}
                <span className="faint">
                  {agentsSentence(example.agents)} · needs {example.requires.join(", ") || "nothing"}
                </span>
                <div className="row wrap" style={{ marginTop: 6 }}>
                  <button
                    type="button"
                    className="btn small"
                    disabled={example.installed}
                    onClick={() => void addExample(example)}
                  >
                    {example.installed ? "In your library" : "Add to library"}
                  </button>
                  <button
                    type="button"
                    className="btn small ghost"
                    onClick={() => setEditor({ title: `New skill from ${example.name}`, yaml: example.yaml })}
                  >
                    Start from this
                  </button>
                </div>
              </article>
            ))}
          </div>
        </section>
      )}

      {deleting !== null && (
        <DeleteSkillDialog
          api={api}
          row={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={(sentence) => {
            setRows((current) => (current ?? []).filter((r) => r.id !== deleting.id));
            setDeleting(null);
            setNotice(sentence);
            void load();
          }}
        />
      )}
    </div>
  );
}

function SkillRowView({
  row,
  open,
  detail,
  onOpen,
  onToggle,
  onEdit,
  onExport,
  onDelete,
}: {
  row: SkillRow;
  open: boolean;
  detail: SkillDetail | null;
  onOpen: () => void;
  onToggle: (agent: SkillAgent, on: boolean) => void;
  onEdit: (yaml: string) => void;
  onExport: () => void;
  onDelete: () => void;
}) {
  return (
    <>
      <tr aria-label={row.name}>
        <td>
          <button type="button" className="link-button" aria-expanded={open} onClick={onOpen}>
            <span className="title">{row.name}</span>
          </button>
          <div className="faint mono">
            {row.id} v{row.version}
          </div>
        </td>
        <td>
          <span className={RISK_TONE[row.risk]}>{RISK_WORD[row.risk]}</span>
        </td>
        <td>
          <div className="switches">
            {row.agents.map((agent) => (
              <label key={agent} className="switch">
                <input
                  type="checkbox"
                  role="switch"
                  checked={row.enabled[agent] === true}
                  aria-label={`${row.name} for the ${AGENT_LABEL[agent]}`}
                  onChange={(event) => onToggle(agent, event.target.checked)}
                />
                <span>{AGENT_LABEL[agent]}</span>
              </label>
            ))}
          </div>
        </td>
        <td className="muted">{row.requires.join(", ") || "nothing"}</td>
        <td className="menu-cell">
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="btn small ghost" onClick={onExport} aria-label={`Export ${row.name}`}>
              Export
            </button>
            <button type="button" className="btn small ghost danger" onClick={onDelete} aria-label={`Delete ${row.name}`}>
              Delete
            </button>
          </div>
        </td>
      </tr>
      {open && (
        <tr className="detail-row">
          <td colSpan={5}>
            {detail === null ? (
              <p className="muted">Opening {row.name}…</p>
            ) : (
              <div className="stack" aria-label={`${row.name} details`}>
                {detail.description !== "" && <p className="sentence">{detail.description}</p>}
                <p className="muted">{detail.sentence}</p>
                <div>
                  <h4 className="label">Steps</h4>
                  <ol className="mono steps">
                    {detail.steps.map((step, index) => (
                      <li key={index}>{step}</li>
                    ))}
                  </ol>
                </div>
                {detail.inputs.length > 0 && (
                  <p className="muted">
                    Asks for:{" "}
                    {detail.inputs.map((input) => `${input.name} (${input.type}${input.required ? ", required" : ""})`).join(", ")}
                  </p>
                )}
                {detail.imported_by !== null && (
                  <p className="faint">
                    Imported by {detail.imported_by}
                    {detail.imported_at !== null ? ` on ${new Date(detail.imported_at).toLocaleString()}` : ""}.
                  </p>
                )}
                <div className="row">
                  <button type="button" className="btn small" onClick={() => onEdit(detail.yaml)}>
                    Edit
                  </button>
                </div>
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function SkillEditor({
  api,
  editor,
  onCancel,
  onSaved,
}: {
  api: SkillsApi;
  editor: Editor;
  onCancel: () => void;
  onSaved: (sentence: string) => Promise<void>;
}) {
  const [yaml, setYaml] = useState(editor.yaml);
  const [checked, setChecked] = useState<SkillCheck | null>(null);
  const [problem, setProblem] = useState<ThreePart | null>(null);
  const [busy, setBusy] = useState<"check" | "save" | null>(null);

  const check = async () => {
    setProblem(null);
    setBusy("check");
    try {
      setChecked(await api.check(yaml));
    } catch (error: unknown) {
      setChecked(null);
      setProblem(asApiError(error).describe(NOT_ANSWERING("The skill wasn't checked.")));
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    setProblem(null);
    setBusy("save");
    try {
      const saved = await api.save(yaml);
      await onSaved(`${saved.sentence} ${saved.state_sentence}`);
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(NOT_ANSWERING("The skill wasn't saved.")));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel stack" aria-labelledby="skill-editor-heading">
      <div className="row between wrap">
        <h2 id="skill-editor-heading">{editor.title}</h2>
        <span className="faint">Check reads the file the way an import does and saves nothing.</span>
      </div>
      <Field
        label="Skill file"
        help="YAML in the format of CLAUDE.md §6: an id, a name, a version, the agents it is for, what it needs and its steps."
      >
        {(control) => (
          <textarea
            {...control}
            className="input mono"
            rows={16}
            spellCheck={false}
            value={yaml}
            onChange={(event) => {
              setYaml(event.target.value);
              setChecked(null);
            }}
          />
        )}
      </Field>
      {checked !== null && (
        <section className="notice ok" role="status" aria-label="Check result">
          <strong>{checked.sentence}</strong>
          <ol className="mono steps">
            {checked.steps.map((step, index) => (
              <li key={index}>{step}</li>
            ))}
          </ol>
          {checked.approval_sentence !== null && <p>{checked.approval_sentence}</p>}
          <p className="muted">
            {checked.replaces === null
              ? `Saving adds ${checked.skill.name} to the library, switched off for every agent.`
              : `Saving replaces version ${checked.replaces} in the library; its switches stay on unless the new version allows more agents, needs more or is riskier.`}
          </p>
        </section>
      )}
      {problem !== null && <ThreePartError parts={problem} />}
      <div className="row">
        <button type="button" className="btn" disabled={busy !== null} onClick={() => void check()}>
          {busy === "check" ? "Checking…" : "Check"}
        </button>
        <button type="button" className="btn primary" disabled={busy !== null} onClick={() => void save()}>
          {busy === "save" ? "Saving…" : "Save to library"}
        </button>
        <button type="button" className="btn ghost" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </section>
  );
}

function DeleteSkillDialog({
  api,
  row,
  onClose,
  onDeleted,
}: {
  api: SkillsApi;
  row: SkillRow;
  onClose: () => void;
  onDeleted: (sentence: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<ThreePart | null>(null);

  const remove = async () => {
    setProblem(null);
    setBusy(true);
    try {
      onDeleted(await api.remove(row.id));
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(NOT_ANSWERING(`${row.name} wasn't deleted.`)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title={`Delete ${row.name}?`} onClose={onClose}>
      <div className="stack">
        <p className="sentence">
          {row.name} v{row.version} leaves the library: no agent can use it any more. Tickets that already ran it keep
          their record. To stop using it for a while, switch it off instead.
        </p>
        <div className="row">
          <button type="button" className="btn danger" disabled={busy} onClick={() => void remove()}>
            {busy ? "Deleting…" : `Delete ${row.name}`}
          </button>
          <button type="button" className="btn ghost" onClick={onClose}>
            Cancel
          </button>
        </div>
        {problem !== null && <ThreePartError parts={problem} />}
      </div>
    </Dialog>
  );
}
