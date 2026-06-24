import React from "react";
import { createRoot } from "react-dom/client";
import "allotment/dist/style.css";
import "./styles.css";
import { App } from "./App";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
