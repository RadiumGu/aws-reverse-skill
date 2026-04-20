#!/usr/bin/env python3
"""region_lock.py — Detect AWS resources that are locked to a specific region.

Certain CloudFormation resources have *hard* region constraints that cannot be
satisfied by a simple ``${AWS::Region}`` substitution:

- ``AWS::CloudFront::Distribution`` — global service; its ACM cert and WAFv2
  WebACL must live in ``us-east-1``.
- ``AWS::WAFv2::WebACL`` with ``Scope: CLOUDFRONT`` must be created in
  ``us-east-1`` (REGIONAL scope has no such restriction).
- ``AWS::CertificateManager::Certificate`` used by CloudFront must live in
  ``us-east-1`` regardless of the app region.
- ``AWS::Lambda::Function`` flagged as a Lambda@Edge function must live in
  ``us-east-1``.

``find_region_locked()`` walks a parsed template and returns one decision
record per affected resource. It is advisory only: ``rewrite_cfn.R15`` emits
the records as ``region-constraint`` review decisions — no YAML mutation
happens here.
"""

from __future__ import annotations

from typing import Any

#: Map of resource type → region constraint metadata.
REGION_LOCKED_RESOURCES: dict[str, dict[str, Any]] = {
    "AWS::CloudFront::Distribution": {
        "required_region": "us-east-1",
        "affected_properties": [
            "DistributionConfig.ViewerCertificate.AcmCertificateArn",
            "DistributionConfig.ViewerCertificate.IamCertificateId",
            "DistributionConfig.WebACLId",
        ],
        "note": (
            "CloudFront is a global service; ACM cert and WAFv2 ACL must be "
            "in us-east-1."
        ),
    },
    "AWS::WAFv2::WebACL": {
        "required_region": "us-east-1",
        "conditional_on": "Scope == CLOUDFRONT",
        "note": "WAFv2 with CLOUDFRONT scope must be created in us-east-1.",
    },
    "AWS::CertificateManager::Certificate": {
        "required_region": "us-east-1",
        "conditional_on": "used_by_cloudfront",
        "note": "ACM cert used by CloudFront must be in us-east-1.",
    },
    "AWS::Lambda::Function": {
        "required_region": "us-east-1",
        "conditional_on": "edge_lambda",
        "note": "Lambda@Edge functions must be created in us-east-1.",
    },
}


def _get(node: Any, *path: str) -> Any:
    """Safe getter — returns None when any segment is missing."""
    cur: Any = node
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _as_text(node: Any) -> str:
    """Flatten a nested CFN value to a plain string for substring scans."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        parts: list[str] = []
        for k, v in node.items():
            parts.append(str(k))
            parts.append(_as_text(v))
        return " ".join(parts)
    if isinstance(node, (list, tuple)):
        return " ".join(_as_text(v) for v in node)
    if node is None:
        return ""
    return str(node)


def _cloudfront_cert_arns(template: dict[str, Any]) -> set[str]:
    """Return every ACM cert ARN referenced by a CloudFront Distribution."""
    seen: set[str] = set()
    resources = template.get("Resources") if isinstance(template, dict) else None
    if not isinstance(resources, dict):
        return seen
    for _lid, resource in resources.items():
        if not isinstance(resource, dict):
            continue
        if resource.get("Type") != "AWS::CloudFront::Distribution":
            continue
        props = resource.get("Properties") or {}
        vc = _get(props, "DistributionConfig", "ViewerCertificate")
        if not isinstance(vc, dict):
            continue
        arn = vc.get("AcmCertificateArn")
        if isinstance(arn, str):
            seen.add(arn)
    return seen


def _cloudfront_cert_logical_ids(template: dict[str, Any]) -> set[str]:
    """Return logical IDs of ACM Certificates referenced via !Ref by CloudFront."""
    seen: set[str] = set()
    resources = template.get("Resources") if isinstance(template, dict) else None
    if not isinstance(resources, dict):
        return seen
    for _lid, resource in resources.items():
        if not isinstance(resource, dict):
            continue
        if resource.get("Type") != "AWS::CloudFront::Distribution":
            continue
        props = resource.get("Properties") or {}
        vc = _get(props, "DistributionConfig", "ViewerCertificate")
        if not isinstance(vc, dict):
            continue
        arn_val = vc.get("AcmCertificateArn")
        # Plain dict intrinsic form: {"Ref": "MyCert"}
        if isinstance(arn_val, dict):
            ref = arn_val.get("Ref")
            if isinstance(ref, str):
                seen.add(ref)
        # ruamel TaggedScalar form: tag=!Ref, value=MyCert
        tag = getattr(arn_val, "tag", None)
        tag_value = getattr(tag, "value", str(tag)) if tag is not None else ""
        if tag_value == "!Ref":
            val = getattr(arn_val, "value", None)
            if isinstance(val, str):
                seen.add(val)
    return seen


def _is_edge_lambda(resource: dict[str, Any]) -> bool:
    """Return True if a Lambda::Function resource is a Lambda@Edge function.

    Detection is heuristic and intentionally conservative: we flag when the
    resource Type is exactly ``AWS::CloudFront::Function`` OR when a
    ``AWS::Lambda::Function`` carries common Lambda@Edge markers.
    """
    if resource.get("Type") != "AWS::Lambda::Function":
        return False
    props = resource.get("Properties") or {}
    if not isinstance(props, dict):
        return False
    # CloudFormation doesn't have a first-class "edge" flag; managers typically
    # signal it via tags, name, or the companion AWS::CloudFront::Distribution
    # referencing the function ARN via LambdaFunctionAssociations. We check
    # the tags + function name for the conventional markers.
    name = props.get("FunctionName")
    if isinstance(name, str) and (
        "edge" in name.lower() or name.lower().startswith("cf-")
    ):
        return True
    tags = props.get("Tags")
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            key = str(tag.get("Key", "")).lower()
            value = str(tag.get("Value", "")).lower()
            if key in ("lambda-edge", "lambda@edge", "edge") and value in (
                "true",
                "yes",
                "1",
            ):
                return True
            if key == "purpose" and "edge" in value:
                return True
    description = props.get("Description")
    if isinstance(description, str) and "lambda@edge" in description.lower():
        return True
    return False


def _cloudfront_attaches_edge_lambda(
    template: dict[str, Any],
) -> set[str]:
    """Return Lambda logical IDs attached to CloudFront LambdaFunctionAssociations."""
    hits: set[str] = set()
    resources = template.get("Resources") if isinstance(template, dict) else None
    if not isinstance(resources, dict):
        return hits
    for _lid, resource in resources.items():
        if not isinstance(resource, dict):
            continue
        if resource.get("Type") != "AWS::CloudFront::Distribution":
            continue
        text = _as_text(resource.get("Properties"))
        # Heuristic: look for references to Lambda logical IDs via LambdaFunctionARN.
        if "LambdaFunctionAssociations" in text or "LambdaFunctionARN" in text:
            # Best-effort: collect any token that matches a Lambda logical id.
            for lid2, res2 in resources.items():
                if (
                    isinstance(res2, dict)
                    and res2.get("Type") == "AWS::Lambda::Function"
                    and str(lid2) in text
                ):
                    hits.add(str(lid2))
    return hits


def find_region_locked(
    template: dict[str, Any], target_region: str
) -> list[dict[str, Any]]:
    """Return region-lock decisions for every affected resource in *template*.

    Args:
        template: Parsed CFN template (dict / ruamel CommentedMap).
        target_region: Destination region the template is expected to deploy to.

    Returns:
        A list of decision dicts with keys ``resource_logical_id``,
        ``resource_type``, ``required_region``, ``target_region``,
        ``is_violation``, ``affected_properties`` and ``note``.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(template, dict):
        return out
    resources = template.get("Resources")
    if not isinstance(resources, dict):
        return out

    cf_cert_arns = _cloudfront_cert_arns(template)
    cf_cert_refs = _cloudfront_cert_logical_ids(template)
    edge_lambda_refs = _cloudfront_attaches_edge_lambda(template)

    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            continue
        rtype = resource.get("Type")
        if not isinstance(rtype, str):
            continue
        meta = REGION_LOCKED_RESOURCES.get(rtype)
        if meta is None:
            continue

        props = resource.get("Properties") or {}
        affected: list[str] = list(meta.get("affected_properties", []) or [])

        if rtype == "AWS::WAFv2::WebACL":
            scope = props.get("Scope") if isinstance(props, dict) else None
            if not (isinstance(scope, str) and scope.upper() == "CLOUDFRONT"):
                continue
            affected = affected or ["Scope"]

        elif rtype == "AWS::CertificateManager::Certificate":
            name = props.get("DomainName") if isinstance(props, dict) else None
            used = str(logical_id) in cf_cert_refs or (
                isinstance(name, str) and name in cf_cert_arns
            )
            if not used:
                continue
            affected = affected or ["DomainName"]

        elif rtype == "AWS::Lambda::Function":
            if not (
                _is_edge_lambda(resource) or str(logical_id) in edge_lambda_refs
            ):
                continue
            affected = affected or ["FunctionName"]

        required_region = meta["required_region"]
        is_violation = bool(target_region) and target_region != required_region

        out.append(
            {
                "resource_logical_id": str(logical_id),
                "resource_type": rtype,
                "required_region": required_region,
                "target_region": target_region or "",
                "is_violation": is_violation,
                "affected_properties": affected,
                "note": meta["note"],
            }
        )
    return out


__all__ = ["REGION_LOCKED_RESOURCES", "find_region_locked"]


def main() -> None:
    """CLI entry point: detect region-locked resources in a CFN template."""
    import argparse
    import json
    import sys
    from pathlib import Path
    from ruamel.yaml import YAML

    parser = argparse.ArgumentParser(description="Detect region-locked CFN resources")
    parser.add_argument("--input", required=True, help="Path to CFN YAML template")
    parser.add_argument("--target-region", required=True, help="Target deployment region")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    yaml = YAML()
    yaml.preserve_quotes = True
    with Path(args.input).open(encoding="utf-8") as fh:
        template = yaml.load(fh)

    results = find_region_locked(template, args.target_region)
    violations = [r for r in results if r.get("is_violation")]

    if args.format == "json":
        print(json.dumps(violations, indent=2))
    else:
        if not violations:
            print(f"No region-lock violations for target region {args.target_region}")
        else:
            for v in violations:
                print(f"⚠️  {v['resource_type']} `{v['resource_logical_id']}` "
                      f"requires {v['required_region']}, target is {v['target_region']}")
                print(f"   {v['note']}")
        print(f"\n{len(violations)} violation(s) found.")


if __name__ == "__main__":
    main()
