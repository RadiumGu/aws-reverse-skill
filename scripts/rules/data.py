"""rules.data — S3 bucket name, AMI, KMS, Route53, and DeletionPolicy rules."""

from __future__ import annotations

from ruamel.yaml.comments import CommentedMap, TaggedScalar

from .common import (
    _AMI_SSM_DEFAULT,
    _Context,
    _ensure_parameters,
    _record_review,
    PEERING_TYPES,
    RETAIN_TYPES,
    is_ami_id,
    is_hosted_zone_id,
    is_kms_arn,
)


def apply_deletion_policy(resources: CommentedMap) -> None:
    """Add ``DeletionPolicy: Retain`` to critical resource types."""
    if not isinstance(resources, CommentedMap):
        return
    for _lid, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        if resource.get("Type") in RETAIN_TYPES and "DeletionPolicy" not in resource:
            resource["DeletionPolicy"] = "Retain"


def add_ami_parameter(template: CommentedMap, ami_ids: list[str]) -> None:
    """Add an ``AmiId`` parameter referencing SSM public path."""
    if not ami_ids:
        return
    params = _ensure_parameters(template)
    params["AmiId"] = CommentedMap(
        [
            ("Type", "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>"),
            ("Default", _AMI_SSM_DEFAULT),
            ("Description", f"AMI ID (original: {ami_ids[0]})"),
        ]
    )


def add_kms_parameter(template: CommentedMap, kms_arns: list[str]) -> None:
    """Add a ``KmsKeyArn`` parameter if any KMS ARNs were rewritten."""
    if not kms_arns:
        return
    params = _ensure_parameters(template)
    params["KmsKeyArn"] = CommentedMap(
        [
            ("Type", "String"),
            ("Description", f"Target-account KMS CMK ARN (original: {kms_arns[0]})"),
            ("AllowedPattern", r"^arn:aws[a-z\-]*:kms:[a-z0-9\-]+:\d{12}:key/.+$"),
        ]
    )


def add_hosted_zone_parameter(
    template: CommentedMap, hosted_zone_ids: list[str]
) -> None:
    """Add ``HostedZoneId`` parameter if any Route53 zone IDs were rewritten."""
    if not hosted_zone_ids:
        return
    params = _ensure_parameters(template)
    params["HostedZoneId"] = CommentedMap(
        [
            ("Type", "AWS::Route53::HostedZone::Id"),
            ("Description", f"Route53 hosted zone ID (original: {hosted_zone_ids[0]})"),
        ]
    )


def add_bucket_prefix_parameter(template: CommentedMap, buckets: list[str]) -> None:
    """Add ``BucketPrefix`` parameter if any S3 buckets were rewritten."""
    if not buckets:
        return
    params = _ensure_parameters(template)
    params["BucketPrefix"] = CommentedMap(
        [
            ("Type", "String"),
            (
                "Description",
                "Prefix prepended to S3 bucket names (avoids global collisions).",
            ),
            ("Default", "myapp"),
        ]
    )


def apply_s3_bucket_name_rule(resources: CommentedMap, ctx: _Context) -> None:
    """R10 — wrap S3 bucket names with ``!Sub '${BucketPrefix}-<name>'``."""
    if "s3_bucket_name" not in ctx.rules:
        return
    if not isinstance(resources, CommentedMap):
        return
    for _lid, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        if resource.get("Type") != "AWS::S3::Bucket":
            continue
        props = resource.get("Properties")
        if not isinstance(props, CommentedMap):
            continue
        name = props.get("BucketName")
        if isinstance(name, str) and not name.startswith("${"):
            ctx.s3_buckets.append(name)
            props["BucketName"] = TaggedScalar(
                value=f"${{BucketPrefix}}-{name}", tag="!Sub"
            )


def scan_peering_resources(resources: CommentedMap, ctx: _Context) -> None:
    """R7 — record peering / TGW attachment resources for review."""
    if "peering" not in ctx.rules:
        return
    if not isinstance(resources, CommentedMap):
        return
    for logical_id, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        rtype = resource.get("Type")
        if rtype in PEERING_TYPES:
            _record_review(
                ctx,
                {
                    "id": f"D.peering.{logical_id}",
                    "category": "peering",
                    "resource": str(logical_id),
                    "type": rtype,
                    "detail": (
                        f"{rtype} requires cross-account handshake — "
                        "fill target PeeringConnectionId/AttachmentId manually."
                    ),
                    "hint": "Run the target-account accept-peering / attach command.",
                },
            )
