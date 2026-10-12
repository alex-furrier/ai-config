"""Target validators for ai-config.

Provides validators for converted plugin output for each target tool.
"""

from __future__ import annotations

from ai_config.validators.target.codex import CodexOutputValidator
from ai_config.validators.target.cursor import CursorOutputValidator
from ai_config.validators.target.opencode import OpenCodeOutputValidator
from ai_config.validators.target.pi import PiOutputValidator
from ai_config.validators.target.prime import PrimeOutputValidator

# Type alias for any output validator
OutputValidator = (
    CodexOutputValidator
    | CursorOutputValidator
    | OpenCodeOutputValidator
    | PiOutputValidator
    | PrimeOutputValidator
)


def get_output_validator(
    target: str,
) -> (
    CodexOutputValidator
    | CursorOutputValidator
    | OpenCodeOutputValidator
    | PiOutputValidator
    | PrimeOutputValidator
):
    """Get the appropriate output validator for a target tool.

    Args:
        target: Target tool name ("codex", "cursor", "opencode", "pi")

    Returns:
        The appropriate validator instance.

    Raises:
        ValueError: If target is not recognized.
    """
    validators = {
        "codex": CodexOutputValidator,
        "cursor": CursorOutputValidator,
        "opencode": OpenCodeOutputValidator,
        "pi": PiOutputValidator,
        "prime": PrimeOutputValidator,
    }

    target_lower = target.lower()
    if target_lower not in validators:
        valid_targets = ", ".join(sorted(validators.keys()))
        raise ValueError(f"Unknown target '{target}'. Valid targets: {valid_targets}")

    return validators[target_lower]()


__all__ = [
    "CodexOutputValidator",
    "CursorOutputValidator",
    "OpenCodeOutputValidator",
    "PiOutputValidator",
    "PrimeOutputValidator",
    "get_output_validator",
    "OutputValidator",
]
