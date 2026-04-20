"""scanner_interface — Abstract interface for resource scanners.

Decouples the pipeline (select.py, preview.py, rewrite_cfn.py) from the
former2 raw.json schema. To swap scanners (e.g. AWS Config, Steampipe),
implement ``Scanner`` and register it in ``get_scanner()``.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# former2 f2type → AWS CloudFormation Type mapping
# ---------------------------------------------------------------------------

#: Maps ``f2type`` values (``service.resource``) to AWS CFN resource types.
#: Extend as needed when former2 adds new resource support.
_F2TYPE_TO_CFN: dict[str, str] = {
    "autoscaling.autoscalinggroup": "AWS::AutoScaling::AutoScalingGroup",
    "autoscaling.lifecyclehook": "AWS::AutoScaling::LifecycleHook",
    "dynamodb.acceleratorparametergroup": "AWS::DAX::ParameterGroup",
    "dynamodb.table": "AWS::DynamoDB::Table",
    "ec2.flowlog": "AWS::EC2::FlowLog",
    "ec2.instance": "AWS::EC2::Instance",
    "ec2.launchtemplate": "AWS::EC2::LaunchTemplate",
    "ec2.networkinterface": "AWS::EC2::NetworkInterface",
    "ec2.networkinterfaceattachment": "AWS::EC2::NetworkInterfaceAttachment",
    "ec2.networkinterfacepermission": "AWS::EC2::NetworkInterfacePermission",
    "ec2.securitygroup": "AWS::EC2::SecurityGroup",
    "ec2.subnet": "AWS::EC2::Subnet",
    "ec2.volume": "AWS::EC2::Volume",
    "ec2.volumeattachment": "AWS::EC2::VolumeAttachment",
    "ec2.vpc": "AWS::EC2::VPC",
    "elbv2.loadbalancer": "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "elbv2.loadbalancerlistener": "AWS::ElasticLoadBalancingV2::Listener",
    "elbv2.loadbalancerlistenercertificate": "AWS::ElasticLoadBalancingV2::ListenerCertificate",
    "elbv2.loadbalancerlistenerrule": "AWS::ElasticLoadBalancingV2::ListenerRule",
    "elbv2.targetgroup": "AWS::ElasticLoadBalancingV2::TargetGroup",
    "iam.role": "AWS::IAM::Role",
    "iam.policy": "AWS::IAM::Policy",
    "iam.instanceprofile": "AWS::IAM::InstanceProfile",
    "lambda.eventsourcemapping": "AWS::Lambda::EventSourceMapping",
    "lambda.function": "AWS::Lambda::Function",
    "lambda.layerversion": "AWS::Lambda::LayerVersion",
    "lambda.permission": "AWS::Lambda::Permission",
    "lambda.version": "AWS::Lambda::Version",
    "rds.cluster": "AWS::RDS::DBCluster",
    "rds.clusterparametergroup": "AWS::RDS::DBClusterParameterGroup",
    "rds.instance": "AWS::RDS::DBInstance",
    "rds.parametergroup": "AWS::RDS::DBParameterGroup",
    "rds.subnetgroup": "AWS::RDS::DBSubnetGroup",
    "s3.bucket": "AWS::S3::Bucket",
    "efs.filesystem": "AWS::EFS::FileSystem",
    "kms.key": "AWS::KMS::Key",
    "sns.topic": "AWS::SNS::Topic",
    "sqs.queue": "AWS::SQS::Queue",
    "route53.hostedzone": "AWS::Route53::HostedZone",
    "route53.recordset": "AWS::Route53::RecordSet",
    "eks.cluster": "AWS::EKS::Cluster",
    "eks.nodegroup": "AWS::EKS::Nodegroup",
    "ecr.repository": "AWS::ECR::Repository",
    "cloudwatch.alarm": "AWS::CloudWatch::Alarm",
    "logs.loggroup": "AWS::Logs::LogGroup",
    "events.rule": "AWS::Events::Rule",
    "acm.certificate": "AWS::CertificateManager::Certificate",
    "cloudfront.distribution": "AWS::CloudFront::Distribution",
    "wafv2.webacl": "AWS::WAFv2::WebACL",
    "stepfunctions.statemachine": "AWS::StepFunctions::StateMachine",
}

# Alias for backward compatibility
ScannerInterface = None  # set below after Scanner class definition


def _f2type_to_cfn(f2type: str) -> str:
    """Convert a former2 ``f2type`` (e.g. ``lambda.function``) to AWS CFN type.

    Falls back to a best-effort ``AWS::<Service>::<Resource>`` conversion when
    the exact mapping is not in the lookup table.
    """
    lower = f2type.lower()
    if lower in _F2TYPE_TO_CFN:
        return _F2TYPE_TO_CFN[lower]
    # Best-effort: service.resource → AWS::Service::Resource
    parts = lower.split(".", 1)
    if len(parts) == 2:
        svc = parts[0].upper() if len(parts[0]) <= 4 else parts[0].capitalize()
        # Map common service abbreviations
        svc_map = {
            "ec2": "EC2", "rds": "RDS", "s3": "S3", "iam": "IAM",
            "efs": "EFS", "kms": "KMS", "sns": "SNS", "sqs": "SQS",
            "eks": "EKS", "ecr": "ECR", "acm": "CertificateManager",
            "elbv2": "ElasticLoadBalancingV2", "lambda": "Lambda",
            "dynamodb": "DynamoDB", "cloudwatch": "CloudWatch",
            "cloudfront": "CloudFront", "route53": "Route53",
            "autoscaling": "AutoScaling", "logs": "Logs",
            "events": "Events", "wafv2": "WAFv2",
            "stepfunctions": "StepFunctions",
        }
        svc = svc_map.get(parts[0], svc)
        res = parts[1].replace("_", " ").title().replace(" ", "")
        return f"AWS::{svc}::{res}"
    return f2type


def normalize_former2_resource(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a former2 raw.json resource dict to the pipeline-expected format.

    former2 uses ``f2type``/``f2id``/``f2data``/``f2region`` fields.
    The pipeline expects ``Type``/``PhysicalResourceId``/``Tags``/``Region``.

    If the dict already has a ``Type`` field (non-former2 format), it is
    returned unchanged.
    """
    if "Type" in raw and "f2type" not in raw:
        return raw  # already in pipeline format

    f2type = raw.get("f2type", "")
    f2id = raw.get("f2id", "")
    f2data = raw.get("f2data", {}) if isinstance(raw.get("f2data"), dict) else {}
    f2region = raw.get("f2region", "")

    # Extract tags from f2data — former2 stores them as a list of {Key, Value}
    raw_tags = f2data.get("Tags", [])
    tags: dict[str, str] = {}
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if isinstance(tag, dict) and "Key" in tag and "Value" in tag:
                tags[tag["Key"]] = tag["Value"]
    elif isinstance(raw_tags, dict):
        tags = raw_tags

    # Build normalized dict — merge f2data properties as top-level
    result: dict[str, Any] = dict(f2data)
    result["Type"] = _f2type_to_cfn(f2type) if f2type else ""
    result["PhysicalResourceId"] = f2id
    if f2region:
        result["Region"] = f2region
    if tags:
        result["Tags"] = tags

    # Preserve original former2 fields for traceability
    result["_f2type"] = f2type
    result["_f2id"] = f2id

    return result


class Resource:
    """Normalized resource representation used across the pipeline.

    Attributes:
        type: AWS resource type (e.g. ``AWS::Lambda::Function``).
        physical_id: Physical resource identifier.
        region: AWS region where the resource lives.
        tags: Key-value tags.
        properties: Raw provider-specific properties.
    """

    __slots__ = ("type", "physical_id", "region", "tags", "properties")

    def __init__(
        self,
        type: str,
        physical_id: str = "",
        region: str = "",
        tags: dict[str, str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        self.type = type
        self.physical_id = physical_id
        self.region = region
        self.tags = tags or {}
        self.properties = properties or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert to the dict format expected by select.py / preview.py."""
        d: dict[str, Any] = dict(self.properties)
        d["Type"] = self.type
        if self.physical_id:
            d.setdefault("PhysicalResourceId", self.physical_id)
        if self.region:
            d.setdefault("Region", self.region)
        if self.tags:
            d.setdefault("Tags", self.tags)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Resource:
        """Create from a raw.json resource dict (auto-detects former2 format)."""
        normed = normalize_former2_resource(data)
        tags = normed.get("Tags")
        if isinstance(tags, list):
            tags = {t["Key"]: t["Value"] for t in tags if isinstance(t, dict) and "Key" in t}
        elif not isinstance(tags, dict):
            tags = {}
        return cls(
            type=normed.get("Type", ""),
            physical_id=normed.get("PhysicalResourceId", ""),
            region=normed.get("Region", ""),
            tags=tags,
            properties={
                k: v
                for k, v in normed.items()
                if k not in ("Type", "PhysicalResourceId", "Region", "Tags",
                             "_f2type", "_f2id")
            },
        )


class Scanner(ABC):
    """Abstract base class for resource scanners."""

    @abstractmethod
    def scan(
        self,
        region: str,
        services: list[str] | None = None,
        profile: str | None = None,
    ) -> list[Resource]:
        """Scan AWS resources and return normalized Resource objects."""
        ...

    @abstractmethod
    def name(self) -> str:
        """Human-readable scanner name."""
        ...


# Backward-compatible alias (BUG-09)
ScannerInterface = Scanner


class Former2Scanner(Scanner):
    """Scanner backed by an existing former2 raw.json file."""

    def __init__(self, raw_path: Path) -> None:
        self._path = raw_path

    def name(self) -> str:
        return "former2"

    def scan(
        self,
        region: str = "",
        services: list[str] | None = None,
        profile: str | None = None,
    ) -> list[Resource]:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        items: list[dict[str, Any]]
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("resources", [])
        else:
            raise ValueError(f"Unexpected JSON structure in {self._path}")
        return [Resource.from_dict(item) for item in items if isinstance(item, dict)]


def _load_raw_items(path: Path) -> list[dict[str, Any]]:
    """Load raw items from a JSON file (list or {resources: [...]})."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("resources", [])
    raise ValueError(f"Unexpected JSON structure in {path}")


def load_resources(path: Path) -> list[Resource]:
    """Convenience: load resources from a raw.json file via Former2Scanner."""
    return Former2Scanner(path).scan()


def load_resources_as_dicts(path: Path) -> list[dict[str, Any]]:
    """Load resources as normalized pipeline dicts.

    Auto-detects former2 format (``f2type``/``f2id``/``f2data``) and converts
    to the pipeline-expected format (``Type``/``PhysicalResourceId``/``Tags``).
    """
    items = _load_raw_items(path)
    return [normalize_former2_resource(item) for item in items if isinstance(item, dict)]
