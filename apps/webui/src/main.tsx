import React from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./index.css";

const root = document.getElementById("root");
if (root === null) {
  throw new Error("The page has no #root element to mount into.");
}
createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
