"""Process settings for the gateway: the environment for facts, YAML files for the rules.

Read once at start. `SLAS_CONSENSUS_FILE` and `SLAS_REDACTION_FILE` are the files compose
mounts from `config/`; a missing file means the shipped defaults with a warning event, a
malformed one stops the start with three parts (a wrong redaction rule is not something to
guess about, INV-5). `pydantic-settings` is not a dependency of this service, so this is
plain `os.environ`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from slas_llm_gateway.consensus import (
    ConsensusConfigError,
    ConsensusRules,
    default_rules,
    rules_from_mapping,
)
from slas_llm_gateway.redaction import (
    RedactionError,
    Redactor,
    default_redactor,
)
from slas_llm_gateway.redaction import (
    rules_from_mapping as redaction_from_mapping,
)
from slas_observability.events import EventLog
from slas_schemas.errors import ThreePartMessage

DEFAULT_BIND: Final = "0.0.0.0:8000"  # the container's own interface, as compose probes it
DEFAULT_CONSENSUS_FILE: Final = Path("/etc/slas/consensus.yaml")
DEFAULT_REDACTION_FILE: Final = Path("/etc/slas/redaction.yaml")
#: Tokens the whole platform may spend per UTC day; cross-checks get `token_budget_pct` of it
#: (CLAUDE.md §5.3). An assumption until the Models page gets a real allowance setting.
DEFAULT_DAILY_TOKENS: Final = 20_000_000
DEFAULT_VLLM_TIMEOUT_S: Final = 120.0


class SettingsError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def _number(environ: Mapping[str, str], name: str, default: float, *, integer: bool) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise SettingsError(
            ThreePartMessage(
                f"The setting {name} could not be read.",
                f"It is {raw!r}, not a {'whole ' if integer else ''}number.",
                f"Set {name} to a number in compose or .env and start the gateway again.",
            )
        ) from None
    if value <= 0:
        raise SettingsError(
            ThreePartMessage(
                f"The setting {name} could not be used.",
                f"It is {raw}; the gateway needs a value above zero.",
                f"Set {name} to a positive number and start the gateway again.",
            )
        )
    return value


@dataclass(frozen=True)
class Settings:
    bind: str = DEFAULT_BIND
    consensus_file: Path = DEFAULT_CONSENSUS_FILE
    redaction_file: Path = DEFAULT_REDACTION_FILE
    vllm_timeout_s: float = DEFAULT_VLLM_TIMEOUT_S
    daily_tokens: int = DEFAULT_DAILY_TOKENS
    #: `CONSENSUS_TOKEN_BUDGET_PCT` from compose; `None` keeps the rules file's value.
    token_budget_pct: float | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        pct_raw = env.get("CONSENSUS_TOKEN_BUDGET_PCT", "").strip()
        return cls(
            bind=env.get("SLAS_BIND", "").strip() or DEFAULT_BIND,
            consensus_file=Path(
                env.get("SLAS_CONSENSUS_FILE", "").strip() or DEFAULT_CONSENSUS_FILE
            ),
            redaction_file=Path(
                env.get("SLAS_REDACTION_FILE", "").strip() or DEFAULT_REDACTION_FILE
            ),
            vllm_timeout_s=_number(
                env, "SLAS_VLLM_TIMEOUT_S", DEFAULT_VLLM_TIMEOUT_S, integer=False
            ),
            daily_tokens=int(_number(env, "SLAS_DAILY_TOKENS", DEFAULT_DAILY_TOKENS, integer=True)),
            token_budget_pct=(
                _number(env, "CONSENSUS_TOKEN_BUDGET_PCT", 0, integer=False) if pct_raw else None
            ),
        )


# --- YAML loaders -----------------------------------------------------------------------


def _read_yaml(path: Path, what: str) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SettingsError(
            ThreePartMessage(
                f"The {what} in {path} could not be read.",
                f"The file is not valid YAML: {str(exc).splitlines()[0]}",
                f"Fix {path}; the shipped file under config/ is the reference.",
            )
        ) from exc


def load_consensus_rules(path: Path, log: EventLog) -> ConsensusRules:
    """`SLAS_CONSENSUS_FILE` → `ConsensusRules`; the shipped defaults when the file is absent."""
    if not path.is_file():
        log.warning("config.default_used", file=str(path), what="consensus rules")
        return default_rules()
    try:
        rules = rules_from_mapping(_read_yaml(path, "cross-check rules"), source=str(path))
    except ConsensusConfigError as exc:
        raise SettingsError(exc.message) from exc
    log.info(
        "config.loaded", file=str(path), what="consensus rules", decisions=len(rules.decisions)
    )
    return rules


def load_redactor(path: Path, log: EventLog) -> Redactor:
    """`SLAS_REDACTION_FILE` → `Redactor`; the shipped defaults when the file is absent."""
    if not path.is_file():
        log.warning("config.default_used", file=str(path), what="redaction rules")
        return default_redactor()
    try:
        rules = redaction_from_mapping(_read_yaml(path, "redaction rules"), source=str(path))
    except RedactionError as exc:
        raise SettingsError(exc.message) from exc
    log.info("config.loaded", file=str(path), what="redaction rules", rules=len(rules.rules))
    return Redactor(rules)
