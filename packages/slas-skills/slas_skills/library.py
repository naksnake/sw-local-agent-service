"""The skills that ship with the platform: the two examples from CLAUDE.md §6.3.

`skills/library/<id>.skill.yaml` is rendered from these mappings; a test keeps them in step.
"""

from __future__ import annotations

from typing import Any, Final

STATION_LOGIN_BURNIN: Final[dict[str, Any]] = {
    "skill": {
        "id": "station-login-burnin",
        "name": "Log in to the test station and start BurnIn",
        "version": "1.0.0",
        "agents": ["factory"],
        "requires": ["screen", "ssh"],
        "inputs": {
            "station": {"type": "target_ref", "required": True},
            "user": {"type": "string", "default": "operator"},
            "password": {"type": "secret", "required": True},
        },
        "steps": [
            {"focus_window": {"title": "Login"}},
            {"click": {"target": "#username"}},
            {"type": {"text": "{{ user }}"}},
            {"key": {"press": "Tab"}},
            {"type": {"text": "{{ password }}"}},
            {"key": {"press": "Enter"}},
            {"wait_for": {"window": "BurnIn v3.2", "timeout_s": 60}},
            {"click": {"text": "Start test"}},
            {
                "ssh": {
                    "target": "{{ station }}",
                    "command": ["burnin-ctl", "status", "--json"],
                },
                "id": "status",
            },
        ],
        "outputs": {"burnin_status": {"from": "status"}},
        "on_failure": "screenshot_and_stop",
    }
}

SEL_COLLECT_CLEAR: Final[dict[str, Any]] = {
    "skill": {
        "id": "sel-collect-clear",
        "name": "Collect and clear the BMC event log",
        "version": "1.2.0",
        "agents": ["validation", "factory"],
        "requires": ["redfish"],
        "inputs": {"target": {"type": "target_ref", "required": True}},
        "steps": [
            {"redfish": {"target": "{{ target }}", "action": "get_sel"}, "id": "sel"},
            {"copy": {"from": "{{ steps.sel.file }}", "to": "logs/sel-before.json"}},
            {
                "assert": {
                    "condition": "{{ steps.sel.count }} < 4000",
                    "message": "SEL nearly full before clearing",
                }
            },
        ],
        "outputs": {"sel_file": {"from": "sel"}},
        "on_failure": "stop",
    }
}

LIBRARY: Final[dict[str, dict[str, Any]]] = {
    "station-login-burnin": STATION_LOGIN_BURNIN,
    "sel-collect-clear": SEL_COLLECT_CLEAR,
}

LIBRARY_HEADER: Final = (
    "Shipped skill for SW Local Agent Service (CLAUDE.md §6.3).\n"
    "Rendered from slas_skills.library; a unit test keeps this file and the code in step.\n"
    "Skills are data: whitelisted verbs only, run with the importing user's capabilities."
)
