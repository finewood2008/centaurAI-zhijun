from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

SECTIONS = ("who", "people", "matters", "principles", "ways", "direction")
Section = Literal["who", "people", "matters", "principles", "ways", "direction"]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]


class AccessError(Exception):
    def __init__(self, code="ACCESS_DENIED", status=403):
        self.code, self.status = code, status
        super().__init__(code)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Subject(Strict):
    accountId: Identifier
    boxId: Identifier
    workspaceId: Identifier
    ownershipEpoch: int = Field(ge=1)


class Principal(Subject):
    agentId: Identifier
    grantId: Identifier
    audience: str = Field(min_length=1, max_length=512)
    expiresAt: int


class GrantSpec(Strict):
    # Only the authenticated first-party management surface may submit this.
    agentId: Identifier
    agentName: str = Field(min_length=1, max_length=100)
    sections: list[Section] = Field(default_factory=list, max_length=6)
    materialIds: list[Identifier] = Field(default_factory=list, max_length=1000)
    excludedClaimIds: list[Identifier] = Field(default_factory=list, max_length=5000)
    acknowledgedLegacyIds: list[Identifier] = Field(default_factory=list, max_length=5000)
    days: Literal[1, 7, 30] = 30
    disclosureAccepted: Literal[True]


class GrantChange(GrantSpec):
    expectedRevision: int = Field(ge=1)


class StateChange(Strict):
    expectedRevision: int = Field(ge=1)
    state: Literal["active", "paused", "revoked"]


class EnableChange(Strict):
    enabled: bool


class PersonalArgs(Strict):
    sections: list[Section] = Field(default_factory=list, max_length=6)
    limit: int = Field(default=30, ge=1, le=100)
    purpose: str = Field(default="", max_length=300)


class SearchArgs(Strict):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=20)
    purpose: str = Field(default="", max_length=300)


class EvidenceArgs(Strict):
    reference: Identifier


class RequestArgs(Strict):
    requestId: Identifier


class DecisionArgs(Strict):
    decision: Literal["masked", "original", "cancel"]


TOOLS = {
    "zhijun_get_access": Strict,
    "zhijun_get_personal_context": PersonalArgs,
    "zhijun_search_work_data": SearchArgs,
    "zhijun_read_work_evidence": EvidenceArgs,
    "zhijun_get_request_status": RequestArgs,
}
