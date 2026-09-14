import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { FakeCodingApi } from "./coding/api";
import "./index.css";

const container = document.getElementById("root");
if (container === null) {
  throw new Error('index.html must contain an element with id="root"');
}

// Until apps/api exposes the Coding calls over HTTP, the page runs on the in-memory fake so
// the wizard and its copy can be reviewed. Nothing here reaches a network.
createRoot(container).render(
  <StrictMode>
    <App codingApi={new FakeCodingApi()} />
  </StrictMode>,
);
