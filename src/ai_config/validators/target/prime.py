"""Validate Prime Rust Markdown skills, independently of Pi extensions."""

from pathlib import Path

import yaml

from ai_config.validators.base import ValidationResult
from ai_config.validators.target.skill_invariants import generated_skill_invariant_errors


class PrimeOutputValidator:
    name = "prime_output"
    description = "Validates Prime Rust Markdown skills"

    def validate_all(self, output_dir: Path) -> list[ValidationResult]:
        results: list[ValidationResult] = []
        for base in (output_dir / ".prime/agent/skills",):
            if not base.exists():
                continue
            for directory in sorted(base.iterdir()):
                if not directory.is_dir():
                    continue
                errors: list[str] = []
                try:
                    raw = (directory / "SKILL.md").read_text()
                    parts = raw.split("---", 2)
                    meta = (
                        yaml.safe_load(parts[1])
                        if raw.startswith("---") and len(parts) == 3
                        else None
                    )
                    if not isinstance(meta, dict):
                        errors.append("Missing YAML frontmatter")
                    else:
                        if meta.get("name") != directory.name:
                            errors.append("Skill name must match its directory")
                        description = meta.get("description")
                        if not isinstance(description, str) or not description.strip():
                            errors.append("Prime requires a nonempty description")
                        errors.extend(generated_skill_invariant_errors(directory, meta))
                    if (directory / "pyproject.toml").exists():
                        errors.append("Python skill packages are outside this adapter contract")
                except (OSError, yaml.YAMLError) as error:
                    errors.append(str(error))
                results.append(
                    ValidationResult(
                        check_name=f"prime_skill_{directory.name}",
                        status="fail" if errors else "pass",
                        message="; ".join(errors)
                        if errors
                        else f"Valid Prime skill {directory.name}",
                    )
                )
        return results or [
            ValidationResult(
                check_name="prime_skills", status="warn", message="No Prime skills found"
            )
        ]
