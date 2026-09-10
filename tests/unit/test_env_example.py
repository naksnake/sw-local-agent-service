"""config/.env.example names every variable install.sh will generate and holds no secret."""

from pathlib import Path

REQUIRED = {
    "SLAS_PROFILE",
    "SLAS_DATA_ROOT",
    "SLAS_EDGE_PORT",
    "SLAS_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "REDIS_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
}
SECRET_MARKERS = ("SECRET", "PASSWORD", "TOKEN", "KEY")


def parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line, f"not KEY=VALUE: {raw!r}"
        key, _, value = line.partition("=")
        assert key == key.strip() and key.isupper(), f"keys are UPPER_CASE: {raw!r}"
        values[key] = value
    return values


def test_required_variables_present(repo_root: Path) -> None:
    values = parse(repo_root / "config/.env.example")
    missing = REQUIRED - values.keys()
    assert not missing, f"missing from .env.example: {sorted(missing)}"


def test_secrets_are_empty(repo_root: Path) -> None:
    values = parse(repo_root / "config/.env.example")
    for key, value in values.items():
        if any(marker in key for marker in SECRET_MARKERS):
            assert value == "", f"{key} must be empty in the example; install.sh generates it"


def test_non_secret_defaults_match_claude_md(repo_root: Path) -> None:
    values = parse(repo_root / "config/.env.example")
    assert values["SLAS_DATA_ROOT"] == "/AI/Agent"
    assert values["SLAS_PROFILE"] == "quickstart"
    assert values["SLAS_EDGE_PORT"] == "443"
