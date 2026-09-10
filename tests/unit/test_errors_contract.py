"""The CLI's standard-library error mirror never drifts from the Pydantic schema."""

import dataclasses

from slas_cli.report import ThreePartError as CliThreePartError
from slas_schemas.errors import ThreePartError


def test_field_names_match() -> None:
    schema_fields = list(ThreePartError.model_fields)
    cli_fields = [f.name for f in dataclasses.fields(CliThreePartError)]
    assert schema_fields == cli_fields == ["what_happened", "likely_cause", "what_to_do"]


def test_schema_rejects_empty_and_extra() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ThreePartError(what_happened="", likely_cause="x", what_to_do="y")
    with pytest.raises(ValidationError):
        ThreePartError.model_validate(
            {"what_happened": "a", "likely_cause": "b", "what_to_do": "c", "code": "E42"}
        )


def test_schema_round_trips() -> None:
    err = ThreePartError(
        what_happened="The BMC at 10.20.2.3 did not answer for 5 minutes.",
        likely_cause="The BMC is rebooting after a firmware update, or the cable is out.",
        what_to_do="Wait 5 more minutes, or check the cable on the rack2 switch, port 3.",
    )
    assert ThreePartError.model_validate_json(err.model_dump_json()) == err
