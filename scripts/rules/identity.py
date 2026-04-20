"""rules.identity — Account ID, region, IAM principal, and external ARN parameter rules."""

from __future__ import annotations

from typing import Any

from ruamel.yaml.comments import CommentedMap

from .common import (
    _Context,
    _ensure_parameters,
    _record_review,
    find_external_account_ids,
    rewrite_account_id,
    rewrite_region,
)


def _record_cross_account_decision(
    ctx: _Context,
    arn: str,
    parameter_name: str,
    service: str,
    location: str,
) -> None:
    """Emit a D.cross-account-arn review decision for R13/R14."""
    decision_id = f"D.cross-account-arn.{parameter_name}.{location}"
    _record_review(
        ctx,
        {
            "id": decision_id,
            "kind": "cross-account-arn",
            "category": "cross-account-arn",
            "resource": parameter_name,
            "parameter_name": parameter_name,
            "arn": arn,
            "service": service,
            "location": location,
            "suggested_action": (
                f"Supply the target-environment ARN that replaces {arn}."
            ),
            "detail": (
                f"Cross-account ARN {arn} referenced from {location} surfaced "
                f"as Parameter {parameter_name}."
            ),
        },
    )


def _parse_service_from_arn(arn: str) -> str:
    """Return the ``service`` segment of *arn* (best-effort)."""
    parts = arn.split(":", 5)
    if len(parts) >= 3:
        return parts[2]
    return "unknown"


def _ingest_rewrite_result(
    ctx: _Context,
    result: dict[str, Any],
    location: str,
) -> None:
    """Merge a rewrite_arns_in_text() result into *ctx* state + review decisions."""
    for param_name, spec in result.get("parameters_needed", {}).items():
        if param_name not in ctx.external_arn_parameters:
            ctx.external_arn_parameters[param_name] = dict(spec)
    for record in result.get("rewrites", []):
        if record.get("action") == "parameter" and record.get("parameter_name"):
            parsed = _parse_service_from_arn(record.get("original", ""))
            _record_cross_account_decision(
                ctx,
                arn=record["original"],
                parameter_name=record["parameter_name"],
                service=parsed,
                location=location,
            )


def add_external_arn_parameters(template: CommentedMap, ctx: _Context) -> None:
    """Add a String Parameter for every external-account ARN captured by R13/R14."""
    if not ctx.external_arn_parameters:
        return
    params = _ensure_parameters(template)
    for name in sorted(ctx.external_arn_parameters):
        if name in params:
            continue
        spec = ctx.external_arn_parameters[name]
        params[name] = CommentedMap(
            [
                ("Type", "String"),
                (
                    "Description",
                    spec.get("description", f"External ARN placeholder ({name})."),
                ),
            ]
        )
        example = spec.get("example_arn")
        if example:
            params[name]["Description"] = (
                f"{params[name]['Description']} Example source ARN: {example}"
            )
