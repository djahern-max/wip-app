import { useState } from "react";
import Accounts from "./config/Accounts.jsx";
import BurdenRates from "./config/BurdenRates.jsx";
import CostCategories from "./config/CostCategories.jsx";
import CostCodes from "./config/CostCodes.jsx";
import Divisions from "./config/Divisions.jsx";
import Policy from "./config/Policy.jsx";

// Tenant configuration (F04). Read for firm roles and client_admin; write for firm
// roles; policy keys for firm_admin. The server enforces every one of these; the
// flags below only decide what to draw.
const SECTIONS = [
  ["accounts", "Accounts"],
  ["divisions", "Divisions"],
  ["categories", "Cost categories"],
  ["codes", "Cost codes"],
  ["burden", "Burden rates"],
  ["policy", "Policy"],
];

export default function Config({ me }) {
  const [section, setSection] = useState("accounts");
  const canManage = me.role === "firm_admin" || me.role === "firm_staff";
  const canSetPolicy = me.role === "firm_admin";
  return (
    <div>
      <h2>Configuration</h2>
      <ul className="subnav">
        {SECTIONS.map(([key, label]) => (
          <li key={key}>
            {key === section ? (
              <span className="current">{label}</span>
            ) : (
              <button type="button" className="link-button" onClick={() => setSection(key)}>
                {label}
              </button>
            )}
          </li>
        ))}
      </ul>
      {section === "accounts" && <Accounts me={me} canManage={canManage} />}
      {section === "divisions" && <Divisions me={me} canManage={canManage} />}
      {section === "categories" && <CostCategories me={me} canManage={canManage} />}
      {section === "codes" && <CostCodes me={me} />}
      {section === "burden" && <BurdenRates me={me} canManage={canManage} />}
      {section === "policy" && <Policy me={me} canSetPolicy={canSetPolicy} />}
    </div>
  );
}
