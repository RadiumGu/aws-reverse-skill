#!/usr/bin/env python3
"""arn_rewriter.py — Generic AWS ARN scanner + rewriter for cross-env templates.

Consumed by Hotfix B (nested ARN handling). This module is intentionally pure
Python (``re`` + ``hashlib`` only) and has no knowledge of CloudFormation
serialization — callers handle wrapping results in ``!Sub`` / ``!Ref`` context.

Rewrite rules (see ``rewrite_arn`` below):

  * ARN whose account matches *source_account* and region matches
    *source_region* → return a ``!Sub`` friendly string with both
    ``${AWS::AccountId}`` and ``${AWS::Region}``. action = ``sub-both``.
  * ARN with only *source_account* match (different region) → ``sub-account``.
  * ARN with only *source_region* match (different account) → ``sub-region``.
  * ARN whose account is *different* from *source_account* (and non-empty) →
    Parameter template ``${External<Service>Arn<Hash>}``. action =
    ``parameter``.
  * ARN whose ``service`` is not in :data:`KNOWN_SERVICES` → unchanged
    (defensive — avoid rewriting arbitrary strings that look ARN-ish).
  * Global ARN (empty region, e.g. IAM / S3 / CloudFront) → never emit
    ``${AWS::Region}``; account-only substitution still applies.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# 6 segments, partition allows aws / aws-cn / aws-us-gov. The resource part is
# greedy on purpose — ARNs may contain additional ``:`` and ``/`` separators
# (e.g. ``lambda:function:foo`` or ``s3:::bucket/key``).
ARN_PATTERN = (
    r"arn:aws(?:-cn|-us-gov)?:"
    r"[a-z0-9-]+:"          # service
    r"[a-z0-9-]*:"          # region (may be empty for global services)
    r"[0-9]*:"              # account (may be empty for some global ARNs)
    r"[a-zA-Z0-9:/_.\-]+"   # resource (path-like)
)

_ARN_RE = re.compile(ARN_PATTERN)

#: Whitelist of AWS service identifiers. ARNs with a service name outside this
#: set are treated as ambiguous and left untouched to avoid rewriting arbitrary
#: strings that happen to match :data:`ARN_PATTERN`.
KNOWN_SERVICES: frozenset[str] = frozenset({
    "iam", "s3", "sns", "sqs", "lambda", "dynamodb", "kms", "ec2",
    "rds", "elasticloadbalancing", "states", "events", "apigateway",
    "execute-api", "logs", "cognito-idp", "cognito-identity",
    "secretsmanager", "ssm", "acm", "cloudfront", "wafv2", "waf",
    "route53", "elasticache", "eks", "ecs", "ecr", "glue", "athena",
    "kinesis", "firehose", "mq", "msk", "efs", "fsx", "elasticfilesystem",
    "cloudformation", "cloudwatch", "config", "organizations", "sts",
    "codepipeline", "codebuild", "codecommit", "codedeploy",
    "stepfunctions",
})

#: Map from service identifier to a CFN-safe PascalCase stem used when
#: constructing external-account parameter names. Names containing hyphens get
#: their hyphens stripped so the resulting identifier matches
#: ``^[A-Za-z][A-Za-z0-9]*$``.
_SERVICE_PARAM_STEM: dict[str, str] = {
    "cognito-idp": "CognitoIdp",
    "cognito-identity": "CognitoIdentity",
    "execute-api": "ExecuteApi",
    "elasticloadbalancing": "Elb",
    "elasticfilesystem": "Efs",
    "stepfunctions": "States",
}

_PARAM_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _parse_arn(arn: str) -> dict[str, Any] | None:
    """Parse a candidate ARN into its 6 components.

    Returns ``None`` when the input does not split into exactly 6 colon-
    separated segments — such strings are not valid ARNs even if they match
    a loose regex.
    """
    segments = arn.split(":", 5)
    if len(segments) != 6:
        return None
    arn_literal, partition_raw, service, region, account, resource = segments
    if arn_literal != "arn":
        return None
    partition = partition_raw
    if partition not in {"aws", "aws-cn", "aws-us-gov"}:
        return None
    return {
        "partition": partition,
        "service": service,
        "region": region,
        "account": account,
        "resource": resource,
    }


def find_arns(text: str) -> list[dict[str, Any]]:
    """Scan *text* and return every ARN-looking substring with parsed fields.

    Each element is a dict with the 6 ARN components plus ``arn`` (original
    literal), ``start`` / ``end`` offsets in the source text, and
    ``is_known_service`` which reflects :data:`KNOWN_SERVICES` membership.
    Substrings that match :data:`ARN_PATTERN` but fail structural validation
    are skipped silently.
    """
    results: list[dict[str, Any]] = []
    for match in _ARN_RE.finditer(text):
        arn_str = match.group(0)
        parsed = _parse_arn(arn_str)
        if parsed is None:
            continue
        results.append({
            "arn": arn_str,
            "partition": parsed["partition"],
            "service": parsed["service"],
            "region": parsed["region"],
            "account": parsed["account"],
            "resource": parsed["resource"],
            "start": match.start(),
            "end": match.end(),
            "is_known_service": parsed["service"] in KNOWN_SERVICES,
        })
    return results


def _service_stem(service: str) -> str:
    """Return a PascalCase identifier stem for *service*.

    Falls back to stripping non-alphanumerics and title-casing.
    """
    if service in _SERVICE_PARAM_STEM:
        return _SERVICE_PARAM_STEM[service]
    cleaned = re.sub(r"[^A-Za-z0-9]", "", service)
    if not cleaned:
        return "Service"
    return cleaned[:1].upper() + cleaned[1:]


def _parameter_name(service: str, arn: str) -> str:
    """Build a deterministic CFN Parameter name for an external-account ARN.

    Format: ``External<ServiceStem>Arn<Hash8>`` where ``<Hash8>`` is the first
    8 hex chars of ``sha256(arn)``. Guaranteed to match
    ``^[A-Za-z][A-Za-z0-9]*$``.
    """
    stem = _service_stem(service)
    digest = hashlib.sha256(arn.encode("utf-8")).hexdigest()[:8]
    # sha256 hex is all [0-9a-f] — safe for CFN param names.
    name = f"External{stem}Arn{digest}"
    if not _PARAM_NAME_RE.match(name):
        # Defensive — should be unreachable given _service_stem() cleanup.
        name = f"ExternalArn{digest}"
    return name


def _build_sub_arn(parsed: dict[str, Any], *, sub_account: bool, sub_region: bool) -> str:
    """Reassemble an ARN literal, substituting account/region placeholders."""
    region = "${AWS::Region}" if sub_region else parsed["region"]
    account = "${AWS::AccountId}" if sub_account else parsed["account"]
    return (
        f"arn:{parsed['partition']}:{parsed['service']}:"
        f"{region}:{account}:{parsed['resource']}"
    )


def rewrite_arn(
    arn: str,
    source_account: str,
    source_region: str,
    target_account: str | None = None,
    target_region: str | None = None,
) -> dict[str, Any]:
    """Rewrite a single ARN according to the module-level rules.

    Args:
        arn: Candidate ARN literal.
        source_account: Account ID of the source environment.
        source_region: Region code of the source environment.
        target_account: Reserved — currently informational only.
        target_region: Reserved — currently informational only.

    Returns:
        Dict with keys ``new_arn``, ``action``, ``parameter_name``, ``notes``.
        ``new_arn`` is either the original ARN (``action == "unchanged"``) or
        a ``!Sub``-friendly string containing ``${AWS::AccountId}`` /
        ``${AWS::Region}`` / ``${ParameterName}`` placeholders.
    """
    del target_account, target_region  # reserved for future rules

    parsed = _parse_arn(arn)
    if parsed is None:
        return {
            "new_arn": arn,
            "action": "unchanged",
            "parameter_name": None,
            "notes": "Not a structurally valid ARN.",
        }

    service = parsed["service"]
    if service not in KNOWN_SERVICES:
        return {
            "new_arn": arn,
            "action": "unchanged",
            "parameter_name": None,
            "notes": f"Service '{service}' not in KNOWN_SERVICES — left unchanged.",
        }

    account = parsed["account"]
    region = parsed["region"]

    # Non-empty account that does not match source → external parameter.
    if account and account != source_account:
        param = _parameter_name(service, arn)
        return {
            "new_arn": "${" + param + "}",
            "action": "parameter",
            "parameter_name": param,
            "notes": (
                f"ARN belongs to external account {account}; surface as "
                f"Parameter '{param}' for operator to supply target ARN."
            ),
        }

    account_matches = bool(account) and account == source_account
    region_matches = bool(region) and region == source_region

    if account_matches and region_matches:
        return {
            "new_arn": _build_sub_arn(parsed, sub_account=True, sub_region=True),
            "action": "sub-both",
            "parameter_name": None,
            "notes": "Source account + source region ARN → substitute both.",
        }
    if account_matches:
        return {
            "new_arn": _build_sub_arn(parsed, sub_account=True, sub_region=False),
            "action": "sub-account",
            "parameter_name": None,
            "notes": (
                "Source account ARN with different / empty region — "
                "account substituted only."
            ),
        }
    if region_matches:
        return {
            "new_arn": _build_sub_arn(parsed, sub_account=False, sub_region=True),
            "action": "sub-region",
            "parameter_name": None,
            "notes": (
                "Source region ARN with different / empty account — "
                "region substituted only."
            ),
        }

    return {
        "new_arn": arn,
        "action": "unchanged",
        "parameter_name": None,
        "notes": "ARN does not match source account or source region.",
    }


def rewrite_arns_in_text(
    text: str,
    source_account: str,
    source_region: str,
    target_account: str | None = None,
    target_region: str | None = None,
) -> dict[str, Any]:
    """Scan *text* for ARNs and return a rewritten copy plus Parameter needs.

    Performs replacements from right-to-left so that earlier offsets remain
    valid while splicing in (possibly shorter or longer) replacement strings.

    Returns:
        Dict with:
          * ``new_text`` — *text* with each recognised ARN replaced.
          * ``rewrites`` — list of per-ARN rewrite records (in original
            scan order) including ``original``, ``new_arn``, ``action``,
            ``parameter_name``, ``notes``.
          * ``parameters_needed`` — mapping ``param_name -> {description,
            example_arn}`` for every external-account ARN encountered.
    """
    matches = find_arns(text)

    rewrites: list[dict[str, Any]] = []
    parameters_needed: dict[str, dict[str, str]] = {}

    # Rewrite right-to-left so index arithmetic stays valid.
    new_text = text
    for match in sorted(matches, key=lambda m: m["start"], reverse=True):
        original = match["arn"]
        decision = rewrite_arn(
            original,
            source_account=source_account,
            source_region=source_region,
            target_account=target_account,
            target_region=target_region,
        )
        record = {
            "original": original,
            "new_arn": decision["new_arn"],
            "action": decision["action"],
            "parameter_name": decision["parameter_name"],
            "notes": decision["notes"],
            "start": match["start"],
            "end": match["end"],
        }
        rewrites.append(record)

        if decision["action"] == "parameter" and decision["parameter_name"]:
            param = decision["parameter_name"]
            if param not in parameters_needed:
                parameters_needed[param] = {
                    "description": (
                        f"Target-environment ARN for external {match['service']} "
                        f"resource (source: {original})."
                    ),
                    "example_arn": original,
                }

        new_text = (
            new_text[: match["start"]] + decision["new_arn"] + new_text[match["end"] :]
        )

    # Emit rewrites in natural scan (left-to-right) order for stable output.
    rewrites.reverse()
    return {
        "new_text": new_text,
        "rewrites": rewrites,
        "parameters_needed": parameters_needed,
    }


def main() -> None:
    """CLI entry point: scan and rewrite ARNs in a text string or file."""
    import argparse
    import json
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Scan/rewrite ARNs in text")
    parser.add_argument("--text", help="Text string to scan for ARNs")
    parser.add_argument("--file", help="File path to scan for ARNs")
    parser.add_argument("--source-account", default="", help="Source AWS account ID")
    parser.add_argument("--source-region", default="", help="Source AWS region")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read()

    arns = find_arns(text)
    if not arns:
        print("No ARNs found." if args.format == "text" else "[]")
        return

    if args.source_account or args.source_region:
        result = rewrite_arns_in_text(
            text,
            source_account=args.source_account,
            source_region=args.source_region,
        )
        if args.format == "json":
            print(json.dumps(result, indent=2))
        else:
            for r in result.get("rewrites", []):
                action = r.get("action", "unchanged")
                print(f"  {action}: {r.get('original', '')} → {r.get('rewritten', r.get('original', ''))}")
            if result.get("parameters_needed"):
                print(f"\nParameters needed: {list(result['parameters_needed'].keys())}")
    else:
        if args.format == "json":
            print(json.dumps(arns, indent=2))
        else:
            for arn in arns:
                print(f"  {arn}")
            print(f"\n{len(arns)} ARN(s) found.")


if __name__ == "__main__":
    main()
