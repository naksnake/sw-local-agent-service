"""Deterministic owner routing for findings (CLAUDE.md §5.4, §10.2).

`config/owner-routing.yaml` is rendered from `DEFAULT_OWNER_ROUTING`; the first rule whose
pattern matches the failure signature or the drafted cause names the owner, component and
default severity. A model never assigns an owner; with no match the finding says the owner
is your call. Moved here from the kernel in P7 so the Validation and Factory executors can
route without importing the kernel; `slas_kernel.rca` re-exports every name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final

from pydantic import Field, ValidationError, model_validator

from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage
from slas_schemas.finding import Severity

Owner = str  # EE · FW · SW · ME · TE — a team label, see config/owner-routing.yaml


class OwnerRule(SlasModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    owner: Owner = Field(min_length=1)
    component: str = Field(min_length=1)
    severity: Severity
    description: str = Field(min_length=1)
    patterns: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _patterns_compile(self) -> OwnerRule:
        for pattern in self.patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"rule {self.id}: pattern {pattern!r} is not a regex ({exc})"
                ) from exc
        return self

    def matches(self, text: str) -> str | None:
        for pattern in self.patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return pattern
        return None


class OwnerRouting(SlasModel):
    version: int = 1
    #: Owners a rule may name; keeps typos out of ticket headlines.
    owners: dict[str, str]
    rules: list[OwnerRule]

    @model_validator(mode="after")
    def _rules_name_known_owners(self) -> OwnerRouting:
        ids: set[str] = set()
        for rule in self.rules:
            if rule.owner not in self.owners:
                raise ValueError(f"rule {rule.id} names owner {rule.owner!r}, which is not defined")
            if rule.id in ids:
                raise ValueError(f"rule id {rule.id!r} appears twice")
            ids.add(rule.id)
        return self


class RoutingDecision(SlasModel):
    owner: Owner | None = None
    component: str | None = None
    severity: Severity | None = None
    rule_id: str | None = None
    matched: str | None = None

    def sentence(self) -> str:
        if self.owner is None:
            return "No routing rule matched; the owner is your call."
        return f"Routed to {self.owner} ({self.component}, {self.severity}) by rule {self.rule_id}."


def route_owner(routing: OwnerRouting, text: str) -> RoutingDecision:
    """First matching rule wins; rules are ordered from most to least specific."""
    for rule in routing.rules:
        matched = rule.matches(text)
        if matched is not None:
            return RoutingDecision(
                owner=rule.owner,
                component=rule.component,
                severity=rule.severity,
                rule_id=rule.id,
                matched=matched,
            )
    return RoutingDecision()


class OwnerRoutingError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def routing_from_mapping(data: object, *, source: str = "<memory>") -> OwnerRouting:
    try:
        return OwnerRouting.model_validate(data)
    except ValidationError as exc:
        raise OwnerRoutingError(
            ThreePartMessage(
                f"The owner routing table in {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source}; every rule needs id, owner, component, severity, description "
                "and at least one pattern.",
            )
        ) from exc


def _rule(
    rule_id: str, owner: str, component: str, severity: str, description: str, *patterns: str
) -> dict[str, object]:
    return {
        "id": rule_id,
        "owner": owner,
        "component": component,
        "severity": severity,
        "description": description,
        "patterns": list(patterns),
    }


DEFAULT_OWNER_ROUTING: Final[dict[str, object]] = {
    "version": 1,
    "owners": {
        "EE": "Electrical engineering: boards, PCIe, power, memory hardware.",
        "FW": "Firmware: BMC, BIOS, device firmware.",
        "SW": "Software: drivers, test tools, agent code.",
        "ME": "Mechanical and thermal.",
        "TE": "Test engineering: suites, stations, fixtures.",
    },
    "rules": [
        _rule(
            "pcie-link",
            "EE",
            "PCIe",
            "S2",
            "A PCIe link came up narrower or slower than the baseline, or dropped.",
            r"pcie link",
            r"lnksta",
            r"link (?:lost|down|width|speed|degraded)",
            r"\baer\b",
        ),
        _rule(
            "gpu-xid",
            "SW",
            "GPU driver",
            "S2",
            "The GPU driver reported an Xid; the driver team triages before hardware does.",
            r"\bxid\b",
            r"\bnvrm\b",
            r"nvidia-smi",
        ),
        _rule(
            "memory",
            "EE",
            "Memory",
            "S2",
            "Correctable or uncorrectable memory errors from EDAC or a machine-check.",
            r"\bedac\b",
            r"\bmce\b",
            r"uncorrectable",
            r"\bdimm\b",
        ),
        _rule(
            "boot",
            "FW",
            "Boot",
            "S1",
            "The target did not reach the OS after a power action.",
            r"boot timeout",
            r"failed to boot",
            r"did not boot",
            r"no post\b",
            r"post code",
        ),
        _rule(
            "power",
            "EE",
            "Power",
            "S1",
            "Power delivery: PSU, PDU, voltage rails, unexpected power loss.",
            r"power (?:loss|fail|fault)",
            r"\bpsu\b",
            r"\bpdu\b",
            r"voltage",
        ),
        _rule(
            "thermal",
            "ME",
            "Thermal",
            "S2",
            "Temperatures, throttling or fan faults.",
            r"thermal",
            r"overheat",
            r"throttl",
            r"\bfan\b",
        ),
        _rule(
            "bmc",
            "FW",
            "BMC",
            "S3",
            "The BMC, Redfish, IPMI or the SEL misbehaved.",
            r"\bbmc\b",
            r"redfish",
            r"\bipmi\b",
            r"\bsel\b",
        ),
        _rule(
            "storage",
            "FW",
            "Storage",
            "S3",
            "NVMe, SMART, RAID or block I/O errors.",
            r"\bnvme\b",
            r"\bsmart\b",
            r"\braid\b",
            r"i/o error",
        ),
        _rule(
            "network",
            "EE",
            "Network",
            "S3",
            "NIC or fabric link problems.",
            r"\bnic\b",
            r"link flap",
            r"\beth\d",
            r"\bmlx\d",
            r"\bib\d",
        ),
        _rule(
            "station",
            "TE",
            "Test station",
            "S3",
            "The station GUI, runner or fixture did not behave.",
            r"station",
            r"burnin",
            r"\bmes\b",
            r"fixture",
            r"window .* not found",
        ),
        _rule(
            "software",
            "SW",
            "Test software",
            "S3",
            "A tool or script failed on its own: tracebacks, bad exits, missing commands.",
            r"traceback",
            r"segfault",
            r"exit(?:ed with)? code",
            r"command not found",
            r"assertion",
        ),
    ],
}

OWNER_ROUTING_FILE_HEADER: Final = (
    "Owner routing for RCA findings in SW Local Agent Service (CLAUDE.md §5.4, §10.2).\n"
    "Rendered from slas_kernel.rca.DEFAULT_OWNER_ROUTING; a unit test keeps file and code in\n"
    "step. Deterministic: the first rule whose pattern matches the failure signature or the\n"
    "drafted cause names the owner, component and default severity. A model never assigns an\n"
    "owner; when no rule matches, the finding says the owner is your call."
)


def default_routing() -> OwnerRouting:
    return routing_from_mapping(DEFAULT_OWNER_ROUTING, source="config/owner-routing.yaml")


def render_owner_routing_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    routing = routing_from_mapping(data)
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
    lines.append(f"version: {routing.version}")
    lines.append("owners:")
    for owner, description in routing.owners.items():
        lines.append(f"  {owner}: {json.dumps(description, ensure_ascii=False)}")
    lines.append("rules:")
    for rule in routing.rules:
        lines.append(f"  - id: {rule.id}")
        lines.append(f"    owner: {rule.owner}")
        lines.append(f"    component: {json.dumps(rule.component, ensure_ascii=False)}")
        lines.append(f"    severity: {rule.severity}")
        lines.append(f"    description: {json.dumps(rule.description, ensure_ascii=False)}")
        lines.append("    patterns:")
        lines.extend(
            f"      - {json.dumps(pattern, ensure_ascii=False)}" for pattern in rule.patterns
        )
    return "\n".join(lines) + "\n"
