import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./index.css";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("index.html must contain an element with id=\"root\"");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
