import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { realApis, wantsFakes } from "./apis";
import { App, type AppProps } from "./App";
import "./index.css";

const container = document.getElementById("root");
if (container === null) {
  throw new Error('index.html must contain an element with id="root"');
}

// Production talks to /api/v1 on the same origin (docs/api-contract.md); nothing here reaches
// another host and nothing here holds a credential. `vite dev` and the Playwright smoke test
// run on in-memory fakes unless VITE_SLAS_FAKE_API=0 (see apis.ts); the fakes are a separate
// chunk that a production build never loads.
async function chooseApis(): Promise<AppProps> {
  if (wantsFakes(import.meta.env)) {
    const { fakeApis } = await import("./apis.fake");
    return fakeApis();
  }
  return realApis();
}

void chooseApis().then((apis) => {
  createRoot(container).render(
    <StrictMode>
      <BrowserRouter>
        <App {...apis} />
      </BrowserRouter>
    </StrictMode>,
  );
});
