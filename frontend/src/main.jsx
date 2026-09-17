import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import { PRODUCT_NAME } from "./product.js";

document.title = PRODUCT_NAME;
createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
