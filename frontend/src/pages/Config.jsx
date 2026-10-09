import { useEffect, useState } from "react";
import Accounts from "./config/Accounts.jsx";
import BurdenRates from "./config/BurdenRates.jsx";
import CostCategories from "./config/CostCategories.jsx";
import CostCodes from "./config/CostCodes.jsx";
import Divisions from "./config/Divisions.jsx";
import Policy from "./config/Policy.jsx";

// Tenant configuration (F04). Read for firm roles and client_admin; write for firm
// roles; policy keys for firm_admin. The server enforces every one of these; the
// flags below only decide what to draw.
//
// Layout (F09.4): the six sections are a row of tabs under the heading; the current
// one is marked the way the main navigation marks the current page (D-47).
const SECTIONS = [
  ["accounts", "Accounts"],
  ["divisions", "Divisions"],
  ["categories", "Cost categories"],
  ["codes", "Cost codes"],
  ["burden", "Burden rates"],
  ["policy", "Policy"],
];

export default function Config({ me, target }) {
  const [section, setSection] = useState(target || "accounts");
  useEffect(() => {
    if (target) setSection(target); // F07.3: Home opens the section it named
  }, [target]);
  const canManage = me.role === "firm_admin" || me.role === "firm_staff";
  const canSetPolicy = me.role === "firm_admin";
  return (
    <div>
      <h1>Configuration</h1>
      <nav className="tabs" aria-label="Configuration sections">
        {SECTIONS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            className="nav-link"
            aria-current={key === section ? "page" : undefined}
            onClick={() => setSection(key)}
          >
            {label}
          </button>
        ))}
      </nav>
      {section === "accounts" && <Accounts me={me} canManage={canManage} />}
      {section === "divisions" && <Divisions me={me} canManage={canManage} />}
      {section === "categories" && <CostCategories me={me} canManage={canManage} />}
      {section === "codes" && <CostCodes me={me} />}
      {section === "burden" && <BurdenRates me={me} canManage={canManage} />}
      {section === "policy" && <Policy me={me} canSetPolicy={canSetPolicy} />}
    </div>
  );
}
