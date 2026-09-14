import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { FakeCodingApi } from "./coding/api";
import { FakeGitApi } from "./git/api";
import "./index.css";

const container = document.getElementById("root");
if (container === null) {
  throw new Error('index.html must contain an element with id="root"');
}

// Until apps/api exposes the Coding and Git calls over HTTP, the pages run on in-memory
// fakes so the wizard, the Git panel and their copy can be reviewed. Nothing here reaches
// a network, and nothing here holds a credential.
createRoot(container).render(
  <StrictMode>
    <App codingApi={new FakeCodingApi()} gitApi={new FakeGitApi()} />
  </StrictMode>,
);
