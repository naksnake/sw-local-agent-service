import { useCallback, useEffect, useState } from "react";

import { asApiError } from "../api/http";
import { ThreePartError } from "../components/ThreePartError";
import { models as copy, type ThreePart } from "../copy/en";
import { type ModelsApi, problemParts, type RegistryView } from "./api";

// Models (docs/ui/models.md; CLAUDE.md §7): the registry sentence, one card per model, who
// serves each role and the cross-check voters. Read-only in round 1; a registry problem is
// shown in three parts instead of an empty page.

interface Props {
  api: ModelsApi;
}

export function ModelsPage({ api }: Props) {
  const [view, setView] = useState<RegistryView | null>(null);
  const [loadProblem, setLoadProblem] = useState<ThreePart | null>(null);

  const load = useCallback(async () => {
    setLoadProblem(null);
    try {
      setView(await api.registry());
    } catch (error: unknown) {
      setLoadProblem(asApiError(error).describe(copy.failedToLoad));
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  const nameOf = (id: string) => view?.models.find((m) => m.id === id)?.display_name ?? id;
  const familyOf = (id: string) => view?.models.find((m) => m.id === id)?.family ?? "";
  const families = view === null ? 0 : new Set(view.voters.map(familyOf)).size;

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

        {view !== null && view.problem == null && (
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

            <div className="cols cols-2">
              <section className="panel" aria-labelledby="roles-heading">
                <h2 id="roles-heading">{copy.rolesHeading}</h2>
                {Object.keys(view.roles).length === 0 ? (
                  <p className="muted">{copy.noRoles}</p>
                ) : (
                  <ul className="plain lines">
                    {Object.entries(view.roles).map(([role, id]) => (
                      <li key={role}>{copy.roleLine(role, nameOf(id))}</li>
                    ))}
                  </ul>
                )}
              </section>
              <section className="panel" aria-labelledby="voters-heading">
                <h2 id="voters-heading">{copy.votersHeading}</h2>
                {view.voters.length === 0 ? (
                  <p className="muted">{copy.noVoters}</p>
                ) : (
                  <>
                    <p className="sentence">{copy.voterCount(view.voters.length, families)}</p>
                    <ul className="plain lines">
                      {view.voters.map((id) => (
                        <li key={id}>{copy.voterLine(nameOf(id), familyOf(id))}</li>
                      ))}
                    </ul>
                  </>
                )}
              </section>
            </div>
            <p className="muted">{copy.readOnly}</p>
          </>
        )}
      </div>
    </div>
  );
}
