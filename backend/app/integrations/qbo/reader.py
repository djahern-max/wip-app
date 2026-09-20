"""Reads from one connected company (F05). One request in flight per connection:
each call holds a transaction-level advisory lock keyed on the connection id, in a
session of its own, for the length of the HTTP request. A 401 causes exactly one
refresh and one retry.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.integrations.qbo import client
from app.integrations.qbo.tokens import access_for


class CompanyReader:
    def __init__(self, engine: Engine, tenant_id: UUID, connection_id: UUID) -> None:
        self.engine = engine
        self.tenant_id = tenant_id
        self.connection_id = connection_id

    def get(self, resource: str, params: dict[str, str] | None = None, *, operation: str) -> Any:
        access = access_for(self.engine, self.tenant_id, self.connection_id)
        try:
            return self._locked_get(access, resource, params, operation)
        except client.Unauthorized:
            rejected_at = datetime.now(UTC)
        access = access_for(
            self.engine, self.tenant_id, self.connection_id, rejected_at=rejected_at
        )
        return self._locked_get(access, resource, params, operation)

    def _locked_get(self, access, resource, params, operation) -> Any:
        with Session(self.engine) as lock, lock.begin():
            lock.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"qbo-request:{self.connection_id}"},
            )
            return client.api_get(
                environment=access.environment,
                realm_id=access.realm_id,
                access_token=access.access_token,
                resource=resource,
                params=params,
                operation=operation,
            )

    def company_info(self) -> dict:
        access = access_for(self.engine, self.tenant_id, self.connection_id)
        body = self.get(f"companyinfo/{access.realm_id}", operation="CompanyInfo")
        return body.get("CompanyInfo", {}) if isinstance(body, dict) else {}

    def query(self, statement: str, *, operation: str) -> dict:
        body = self.get("query", {"query": statement}, operation=operation)
        return body.get("QueryResponse", {}) if isinstance(body, dict) else {}
