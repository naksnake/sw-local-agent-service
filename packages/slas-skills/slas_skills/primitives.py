"""The primitive whitelist: the only verbs a skill may use (CLAUDE.md §6.2, INV-12).

No `shell`, no `eval`, no `python`, no `download`, no `sudo`. `run` executes argv only.
Firmware flash, secure erase and BIOS reset are not skill primitives.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from slas_authz import Capability

Risk = Literal["safe", "caution", "destructive"]
RISK_ORDER: Final[dict[str, int]] = {"safe": 0, "caution": 1, "destructive": 2}

REDFISH_SAFE_ACTIONS: Final[frozenset[str]] = frozenset(
    {"get_power_state", "get_sel", "get_inventory"}
)
REDFISH_DESTRUCTIVE_ACTIONS: Final[frozenset[str]] = frozenset(
    {"power_on", "power_off", "force_off", "graceful_restart"}
)
REDFISH_ACTIONS: Final[frozenset[str]] = REDFISH_SAFE_ACTIONS | REDFISH_DESTRUCTIVE_ACTIONS


@dataclass(frozen=True, slots=True)
class Primitive:
    name: str
    description: str
    #: Capabilities that satisfy this primitive (any one of them); empty = none needed.
    needs: tuple[Capability, ...]
    risk: Risk
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    #: At least one of these argument groups must be present in full.
    one_of: tuple[tuple[str, ...], ...] = ()
    control: bool = False

    @property
    def screen(self) -> bool:
        return self.needs == (Capability.SCREEN,)

    def known_args(self) -> frozenset[str]:
        names = set(self.required) | set(self.optional)
        for group in self.one_of:
            names.update(group)
        return frozenset(names)


_SCREEN = (Capability.SCREEN,)
_TARGETING = (("text",), ("image",), ("target",), ("x", "y"))

PRIMITIVES: Final[dict[str, Primitive]] = {
    p.name: p
    for p in (
        Primitive(
            "focus_window",
            "Bring a window to the front by title or class.",
            _SCREEN,
            "safe",
            one_of=(("title",), ("class",)),
        ),
        Primitive(
            "click",
            "Click a target found by text, image, accessible target or coordinates.",
            _SCREEN,
            "safe",
            one_of=_TARGETING,
        ),
        Primitive("double_click", "Double-click a target.", _SCREEN, "safe", one_of=_TARGETING),
        Primitive("right_click", "Right-click a target.", _SCREEN, "safe", one_of=_TARGETING),
        Primitive(
            "type",
            "Type text; may reference a {{ secret }} and is then never logged.",
            _SCREEN,
            "safe",
            required=("text",),
        ),
        Primitive(
            "key",
            "Press a key or chord such as Enter, Tab or ctrl+s.",
            _SCREEN,
            "safe",
            required=("press",),
        ),
        Primitive(
            "scroll",
            "Scroll in a direction by an amount.",
            _SCREEN,
            "safe",
            required=("direction", "amount"),
        ),
        Primitive(
            "wait_for",
            "Wait until a window, text or image appears.",
            _SCREEN,
            "safe",
            optional=("timeout_s",),
            one_of=(("window",), ("text",), ("image",)),
        ),
        Primitive("screenshot", "Take a named screenshot.", _SCREEN, "safe", required=("name",)),
        Primitive(
            "assert_visible",
            "Fail with a message unless text or an image is visible.",
            _SCREEN,
            "safe",
            required=("message",),
            one_of=(("text",), ("image",)),
        ),
        Primitive(
            "run",
            "Run an argv list in the sandbox or on the target; never a shell line.",
            (Capability.FILES, Capability.SSH),
            "caution",
            required=("command",),
            optional=("cwd", "expect_exit", "timeout_s", "capture"),
        ),
        Primitive(
            "ssh",
            "Run an argv list on a target over SSH.",
            (Capability.SSH,),
            "caution",
            required=("target", "command"),
            optional=("timeout_s",),
        ),
        Primitive(
            "copy",
            "Copy a file within the workspace or over SSH.",
            (Capability.FILES, Capability.SSH),
            "caution",
            required=("from", "to"),
        ),
        Primitive(
            "redfish",
            "Read or control a BMC; power actions are destructive.",
            (Capability.REDFISH,),
            "safe",
            required=("target", "action"),
        ),
        Primitive(
            "sel_snapshot",
            "Save the BMC event log.",
            (Capability.REDFISH, Capability.SSH),
            "safe",
            required=("target",),
        ),
        Primitive(
            "inventory_snapshot",
            "Save the hardware inventory.",
            (Capability.REDFISH, Capability.SSH),
            "safe",
            required=("target",),
        ),
        Primitive(
            "wait",
            "Pause for a number of seconds (at most 3600).",
            (),
            "safe",
            required=("seconds",),
        ),
        Primitive(
            "set",
            "Set a variable from a value or a previous step's output.",
            (),
            "safe",
            required=("var", "value"),
        ),
        Primitive(
            "assert",
            "Fail with a message unless a condition holds.",
            (),
            "safe",
            required=("condition", "message"),
        ),
        Primitive(
            "if",
            "Run `then` steps when a condition holds, else `else` steps.",
            (),
            "safe",
            required=("condition", "then"),
            optional=("else",),
            control=True,
        ),
        Primitive(
            "foreach",
            "Run `then` steps once per item (at most 100 items).",
            (),
            "safe",
            required=("items", "then"),
            optional=("as",),
            control=True,
        ),
    )
}

SCREEN_PRIMITIVES: Final[frozenset[str]] = frozenset(
    p.name for p in PRIMITIVES.values() if p.screen
)
CONTROL_PRIMITIVES: Final[frozenset[str]] = frozenset(
    p.name for p in PRIMITIVES.values() if p.control
)
MAX_FOREACH_ITEMS: Final = 100
MAX_WAIT_SECONDS: Final = 3600


def risk_of(primitive: str, args: Mapping[str, object]) -> Risk:
    """The risk class of one step; `redfish` depends on its action."""
    spec = PRIMITIVES[primitive]
    if primitive == "redfish":
        action = str(args.get("action", ""))
        if action in REDFISH_DESTRUCTIVE_ACTIONS:
            return "destructive"
        return "safe"
    return spec.risk


def max_risk(risks: list[Risk]) -> Risk:
    if not risks:
        return "safe"
    return max(risks, key=lambda risk: RISK_ORDER[risk])
