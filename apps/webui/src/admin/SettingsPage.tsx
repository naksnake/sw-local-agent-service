import { useCallback, useEffect, useState } from "react";

import { asApiError } from "../api/http";
import { Field } from "../components/Field";
import { ThreePartError } from "../components/ThreePartError";
import { lifetimeWords, settings as copy, type ThreePart } from "../copy/en";
import { clockShort } from "../time";
import type { ChineseVariant, RuntimeSettings, SettingsApi, SettingsView } from "./api";

// Admin → Settings (docs/ui/admin-settings.md, ADR-0008): what you can change now, and what
// was set at install. Save changes is the one primary action and stays disabled until a
// value differs from the saved one. Nothing here needs a restart, and the page says so.

interface Props {
  api: SettingsApi;
}

function changed(draft: RuntimeSettings, saved: RuntimeSettings): Partial<RuntimeSettings> {
  const diff: Partial<RuntimeSettings> = {};
  if (draft.installation_name !== saved.installation_name) {
    diff.installation_name = draft.installation_name;
  }
  if (draft.chinese_variant !== saved.chinese_variant) {
    diff.chinese_variant = draft.chinese_variant;
  }
  if (draft.session_lifetime_hours !== saved.session_lifetime_hours) {
    diff.session_lifetime_hours = draft.session_lifetime_hours;
  }
  return diff;
}

export function SettingsPage({ api }: Props) {
  const [view, setView] = useState<SettingsView | null>(null);
  const [draft, setDraft] = useState<RuntimeSettings | null>(null);
  const [loadProblem, setLoadProblem] = useState<ThreePart | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [mirror, setMirror] = useState<ThreePart | null>(null);
  const [problem, setProblem] = useState<ThreePart | null>(null);

  const load = useCallback(async () => {
    setLoadProblem(null);
    try {
      const value = await api.get();
      setView(value);
      setDraft({ ...value.runtime });
    } catch (error: unknown) {
      setLoadProblem(asApiError(error).describe(copy.failedToLoad));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  if (loadProblem !== null) {
    return (
      <Page>
        <ThreePartError
          parts={loadProblem}
          action={
            <button type="button" className="btn small" onClick={() => void load()}>
              {copy.tryAgain}
            </button>
          }
        />
      </Page>
    );
  }
  if (view === null || draft === null) {
    return (
      <Page>
        <p className="muted">{copy.loading}</p>
      </Page>
    );
  }

  const diff = changed(draft, view.runtime);
  const dirty = Object.keys(diff).length > 0;
  const lifetimes: number[] = copy.sessionLifetime.choices.map((c) => c.value);
  if (!lifetimes.includes(draft.session_lifetime_hours)) {
    lifetimes.push(draft.session_lifetime_hours);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (draft === null || view === null) {
      return;
    }
    setProblem(null);
    setSavedAt(null);
    setMirror(null);
    if (draft.installation_name.length > copy.installationName.maxLength) {
      setProblem(copy.nameTooLong(draft.installation_name.length));
      return;
    }
    setSaving(true);
    try {
      const result = await api.save(diff);
      setView({ ...view, runtime: result.runtime });
      setDraft({ ...result.runtime });
      setSavedAt(clockShort());
      setMirror(result.notice === null ? null : copy.mirrorFailed(`${view.install.data_root}/.env`));
    } catch (error: unknown) {
      setProblem(asApiError(error).describe(copy.notSaved));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Page>
      <form className="panel stack" onSubmit={(event) => void save(event)} aria-labelledby="runtime-heading">
        <h2 id="runtime-heading">{copy.runtimeHeading}</h2>
        <Field label={copy.installationName.label} help={copy.installationName.help}>
          {(control) => (
            <input
              {...control}
              className="input"
              value={draft.installation_name}
              onChange={(event) => setDraft({ ...draft, installation_name: event.target.value })}
            />
          )}
        </Field>
        <fieldset className="choices">
          <legend>{copy.chineseVariant.label}</legend>
          {copy.chineseVariant.choices.map((choice) => (
            <label key={choice.value} className="choice">
              <input
                type="radio"
                name="chinese_variant"
                value={choice.value}
                checked={draft.chinese_variant === choice.value}
                onChange={() => setDraft({ ...draft, chinese_variant: choice.value as ChineseVariant })}
              />
              <span>{choice.label}</span>
            </label>
          ))}
          <small className="help">{copy.chineseVariant.help}</small>
        </fieldset>
        <fieldset className="choices">
          <legend>{copy.sessionLifetime.label}</legend>
          {lifetimes.map((hours) => (
            <label key={hours} className="choice">
              <input
                type="radio"
                name="session_lifetime_hours"
                value={hours}
                checked={draft.session_lifetime_hours === hours}
                onChange={() => setDraft({ ...draft, session_lifetime_hours: hours })}
              />
              <span>{lifetimeWords(hours)}</span>
            </label>
          ))}
          <small className="help">{copy.sessionLifetime.help}</small>
        </fieldset>
        <div className="row">
          <button type="submit" className="btn primary" disabled={!dirty || saving}>
            {saving ? copy.saving : copy.save}
          </button>
          {savedAt !== null && (
            <span role="status" className="muted">
              {copy.saved(savedAt)}
            </span>
          )}
        </div>
        {mirror !== null && <ThreePartError parts={mirror} tone="warn" />}
        {problem !== null && <ThreePartError parts={problem} />}
      </form>

      <section className="panel" aria-labelledby="install-heading">
        <h2 id="install-heading">{copy.installHeading}</h2>
        <dl className="facts">
          <dt>{copy.facts.dataRoot}</dt>
          <dd className="mono">{view.install.data_root}</dd>
          <dt>{copy.facts.port}</dt>
          <dd>{view.install.https_port}</dd>
          <dt>{copy.facts.tlsNames}</dt>
          <dd>{view.install.tls_names.join(", ")}</dd>
          <dt>{copy.facts.tlsMode}</dt>
          <dd>{copy.tlsModeWords(view.install.tls_mode)}</dd>
          <dt>{copy.facts.profile}</dt>
          <dd>{view.install.profile}</dd>
          <dt>{copy.facts.version}</dt>
          <dd>{view.install.version}</dd>
        </dl>
        <p className="muted" style={{ marginTop: 12 }}>
          {copy.footer}
        </p>
      </section>
    </Page>
  );
}

function Page({ children }: { children: React.ReactNode }) {
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>{copy.heading}</h1>
          <p className="lede">{copy.lede}</p>
        </div>
      </div>
      <div className="stack">{children}</div>
    </div>
  );
}
