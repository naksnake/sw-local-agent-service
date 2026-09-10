"""Naming is used exactly as CLAUDE.md §0.1 says."""

from slas_kernel import branding


def test_product_name_and_slug() -> None:
    assert branding.PRODUCT_NAME == "SW Local Agent Service"
    assert branding.SLUG == "slas"
    assert branding.SLUG == branding.SLUG.lower()


def test_prefixes_derive_from_slug() -> None:
    assert branding.METRIC_PREFIX == f"{branding.SLUG}_"
    assert branding.NETWORK_PREFIX == f"{branding.SLUG}-"
    assert branding.CLI_NAME == branding.SLUG


def test_default_data_root_and_agents() -> None:
    assert branding.DEFAULT_DATA_ROOT == "/AI/Agent"
    assert branding.AGENT_NAMES == ("Coding Agent", "Validation Agent", "Factory Agent")
    assert branding.KERNEL_NAME == "Agent Kernel"


def test_cli_mirrors_branding() -> None:
    """slas_cli cannot import slas_kernel (it must run before anything is installed)."""
    from slas_cli import cli

    assert cli.PRODUCT_NAME == branding.PRODUCT_NAME
    assert cli.DEFAULT_DATA_ROOT == branding.DEFAULT_DATA_ROOT
