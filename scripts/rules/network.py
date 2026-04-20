"""rules.network — VPC/Subnet/SG ID rewriting (R11) and SG CIDR classification (R12)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml import YAML

sys.path.insert(0, str(Path(__file__).parent.parent))
from cidr_analyzer import classify_cidr  # noqa: E402

from .common import (
    _Context,
    _ensure_parameters,
    _record_review,
    _VPC_ID_KEYS,
    _SUBNET_SINGLE_KEYS,
    _SUBNET_LIST_KEYS,
    _SG_SINGLE_KEYS,
    _SG_LIST_KEYS,
    is_vpc_id,
    is_subnet_id,
    is_sg_id,
)


# ---------------------------------------------------------------------------
# Pre-parsed YAML templates for R11 replacements
# ---------------------------------------------------------------------------

_yaml_rt = YAML()
_SPLIT_SUBNETS_TEMPLATE: CommentedSeq = _yaml_rt.load(
    'x: !Split [",", !Ref TargetSubnetIds]\n'
)["x"]
_SPLIT_SGS_TEMPLATE: CommentedSeq = _yaml_rt.load(
    'x: !Split [",", !Ref TargetSecurityGroupIds]\n'
)["x"]
_SELECT_FIRST_SUBNET_TEMPLATE: CommentedSeq = _yaml_rt.load(
    'x: !Select [0, !Split [",", !Ref TargetSubnetIds]]\n'
)["x"]
_SELECT_FIRST_SG_TEMPLATE: CommentedSeq = _yaml_rt.load(
    'x: !Select [0, !Split [",", !Ref TargetSecurityGroupIds]]\n'
)["x"]


# ---------------------------------------------------------------------------
# R11 scalar / list rewriters
# ---------------------------------------------------------------------------


def _rewrite_vpc_scalar(
    value: str, ctx: _Context
) -> TaggedScalar | None:
    if not is_vpc_id(value):
        return None
    if value not in ctx.vpc_ids:
        ctx.vpc_ids.append(value)
    return TaggedScalar(value="TargetVpcId", tag="!Ref")


def _rewrite_subnet_scalar(
    value: str, ctx: _Context
) -> CommentedSeq | None:
    if not is_subnet_id(value):
        return None
    if value not in ctx.subnet_ids:
        ctx.subnet_ids.append(value)
    return copy.deepcopy(_SELECT_FIRST_SUBNET_TEMPLATE)


def _rewrite_sg_scalar(
    value: str, ctx: _Context
) -> CommentedSeq | None:
    if not is_sg_id(value):
        return None
    if value not in ctx.security_group_ids:
        ctx.security_group_ids.append(value)
    return copy.deepcopy(_SELECT_FIRST_SG_TEMPLATE)


def _rewrite_subnet_list(
    seq: CommentedSeq | list, ctx: _Context
) -> CommentedSeq | None:
    if not isinstance(seq, (CommentedSeq, list)):
        return None
    literal_items = [v for v in seq if isinstance(v, str) and is_subnet_id(v)]
    if not literal_items or len(literal_items) != len(seq):
        for v in literal_items:
            if v not in ctx.subnet_ids:
                ctx.subnet_ids.append(v)
        return None
    for v in literal_items:
        if v not in ctx.subnet_ids:
            ctx.subnet_ids.append(v)
    return copy.deepcopy(_SPLIT_SUBNETS_TEMPLATE)


def _rewrite_sg_list(
    seq: CommentedSeq | list, ctx: _Context
) -> CommentedSeq | None:
    if not isinstance(seq, (CommentedSeq, list)):
        return None
    literal_items = [v for v in seq if isinstance(v, str) and is_sg_id(v)]
    if not literal_items or len(literal_items) != len(seq):
        for v in literal_items:
            if v not in ctx.security_group_ids:
                ctx.security_group_ids.append(v)
        return None
    for v in literal_items:
        if v not in ctx.security_group_ids:
            ctx.security_group_ids.append(v)
    return copy.deepcopy(_SPLIT_SGS_TEMPLATE)


# ---------------------------------------------------------------------------
# R11 — apply_vpc_resource_ids_rule
# ---------------------------------------------------------------------------


def apply_vpc_resource_ids_rule(
    resources: CommentedMap, ctx: _Context
) -> None:
    """R11 — Replace literal VPC/Subnet/SG IDs with CFN Parameter references."""
    if "vpc_resource_ids" not in ctx.rules:
        return
    if not isinstance(resources, CommentedMap):
        return

    def _process(node: Any) -> Any:
        if isinstance(node, CommentedMap):
            for key in list(node.keys()):
                val = node[key]
                skey = str(key)
                if skey in _VPC_ID_KEYS and isinstance(val, str):
                    new = _rewrite_vpc_scalar(val, ctx)
                    if new is not None:
                        node[key] = new
                        continue
                if skey in _SUBNET_SINGLE_KEYS and isinstance(val, str):
                    new = _rewrite_subnet_scalar(val, ctx)
                    if new is not None:
                        node[key] = new
                        continue
                if skey in _SG_SINGLE_KEYS and isinstance(val, str):
                    new = _rewrite_sg_scalar(val, ctx)
                    if new is not None:
                        node[key] = new
                        continue
                if skey in _SUBNET_LIST_KEYS:
                    new_seq = _rewrite_subnet_list(val, ctx)
                    if new_seq is not None:
                        node[key] = new_seq
                        continue
                if skey in _SG_LIST_KEYS:
                    new_seq = _rewrite_sg_list(val, ctx)
                    if new_seq is not None:
                        node[key] = new_seq
                        continue
                _process(val)
        elif isinstance(node, (CommentedSeq, list)):
            for item in node:
                _process(item)

    for _lid, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        props = resource.get("Properties")
        if isinstance(props, CommentedMap):
            _process(props)


def add_vpc_resource_parameters(template: CommentedMap, ctx: _Context) -> None:
    """Add TargetVpcId / TargetSubnetIds / TargetSecurityGroupIds parameters."""
    params_needed = (
        bool(ctx.vpc_ids) or bool(ctx.subnet_ids) or bool(ctx.security_group_ids)
    )
    if not params_needed:
        return
    params = _ensure_parameters(template)
    if ctx.vpc_ids and "TargetVpcId" not in params:
        params["TargetVpcId"] = CommentedMap(
            [
                ("Type", "AWS::EC2::VPC::Id"),
                (
                    "Description",
                    f"Target-environment VPC ID (source refs: {', '.join(ctx.vpc_ids)})",
                ),
            ]
        )
    if ctx.subnet_ids and "TargetSubnetIds" not in params:
        params["TargetSubnetIds"] = CommentedMap(
            [
                ("Type", "CommaDelimitedList"),
                (
                    "Description",
                    "Comma-separated target-environment subnet IDs "
                    f"(source refs: {', '.join(ctx.subnet_ids)})",
                ),
            ]
        )
    if ctx.security_group_ids and "TargetSecurityGroupIds" not in params:
        params["TargetSecurityGroupIds"] = CommentedMap(
            [
                ("Type", "CommaDelimitedList"),
                (
                    "Description",
                    "Comma-separated target-environment security group IDs "
                    f"(source refs: {', '.join(ctx.security_group_ids)})",
                ),
            ]
        )


# ---------------------------------------------------------------------------
# R12 — SG CIDR classification
# ---------------------------------------------------------------------------


def _format_sg_port(props: CommentedMap | dict[str, Any]) -> str:
    if not isinstance(props, (CommentedMap, dict)):
        return "all"
    proto = props.get("IpProtocol")
    if proto in ("-1", -1):
        return "all"
    from_port = props.get("FromPort")
    to_port = props.get("ToPort")
    if from_port is None and to_port is None:
        return "all"
    if from_port == to_port:
        return str(from_port)
    return f"{from_port}-{to_port}"


def _record_sg_cidr_decision(
    ctx: _Context,
    logical_id: str,
    direction: str,
    port: str,
    cidr: str,
    classification: dict[str, Any],
) -> None:
    category = classification["category"]
    if category == "rfc1918-external":
        kind = "sg-cidr-rfc1918"
        suggested = (
            "Ask admin for the equivalent CIDR in the target environment "
            "(peer/on-prem network)."
        )
    elif category == "aws-public-range":
        kind = "sg-cidr-aws-range"
        suggested = (
            "Consider replacing with a managed AWS PrefixList "
            "(e.g. com.amazonaws.<region>.s3)."
        )
    elif category == "public":
        kind = "sg-cidr-public-check"
        suggested = (
            "Confirm this public CIDR is still valid for the target environment."
        )
    else:
        return

    decision_id = (
        f"D.{kind}.{logical_id}.{direction}.{port}.{cidr.replace('/', '_')}"
    )
    _record_review(
        ctx,
        {
            "id": decision_id,
            "kind": kind,
            "category": "sg-cidr",
            "resource": logical_id,
            "resource_logical_id": logical_id,
            "direction": direction,
            "port": port,
            "cidr": cidr,
            "classification": category,
            "suggested_action": suggested,
            "detail": (
                f"SG `{logical_id}` {direction} {port} CIDR {cidr} classified "
                f"as {category}."
            ),
        },
    )


def _process_sg_rule_list(
    rules_node: Any,
    direction: str,
    logical_id: str,
    ctx: _Context,
) -> None:
    if not isinstance(rules_node, (CommentedSeq, list)):
        return
    for rule in rules_node:
        if not isinstance(rule, (CommentedMap, dict)):
            continue
        port = _format_sg_port(rule)
        for cidr_key in ("CidrIp", "CidrIpv6"):
            cidr_val = rule.get(cidr_key) if hasattr(rule, "get") else None
            if not isinstance(cidr_val, str):
                continue
            try:
                result = classify_cidr(cidr_val, ctx.source_vpc_cidrs)
            except ValueError:
                continue
            category = result["category"]
            if category == "vpc-internal" and cidr_key == "CidrIp":
                rule[cidr_key] = TaggedScalar(value="TargetVpcCidr", tag="!Ref")
                ctx.sg_vpc_cidr_used = True
            else:
                _record_sg_cidr_decision(
                    ctx, logical_id, direction, port, cidr_val, result
                )


def apply_sg_cidr_rule(resources: CommentedMap, ctx: _Context) -> None:
    """R12 — classify SG ingress/egress CIDRs."""
    if "sg_cidr" not in ctx.rules:
        return
    if not isinstance(resources, CommentedMap):
        return
    if not ctx.source_vpc_cidrs:
        ctx.sg_cidr_warnings.append(
            "R12 skipped: no source VPC CIDRs provided (pass --source-vpc-cidr "
            "or include vpc_cidr entries in raw.json)."
        )
        return

    for logical_id, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        rtype = resource.get("Type")
        props = resource.get("Properties")
        if rtype == "AWS::EC2::SecurityGroup" and isinstance(props, CommentedMap):
            _process_sg_rule_list(
                props.get("SecurityGroupIngress"), "Ingress", str(logical_id), ctx,
            )
            _process_sg_rule_list(
                props.get("SecurityGroupEgress"), "Egress", str(logical_id), ctx,
            )
        elif rtype == "AWS::EC2::SecurityGroupIngress" and isinstance(
            props, CommentedMap
        ):
            _process_sg_rule_list([props], "Ingress", str(logical_id), ctx)
        elif rtype == "AWS::EC2::SecurityGroupEgress" and isinstance(
            props, CommentedMap
        ):
            _process_sg_rule_list([props], "Egress", str(logical_id), ctx)


def add_sg_cidr_parameter(template: CommentedMap, ctx: _Context) -> None:
    """Add ``TargetVpcCidr`` parameter if R12 replaced any vpc-internal CIDR."""
    if not ctx.sg_vpc_cidr_used:
        return
    params = _ensure_parameters(template)
    if "TargetVpcCidr" in params:
        return
    params["TargetVpcCidr"] = CommentedMap(
        [
            ("Type", "String"),
            (
                "Description",
                "Target-environment VPC CIDR (replaces source-VPC-internal "
                f"CIDRs; source VPCs: {', '.join(ctx.source_vpc_cidrs)})",
            ),
            ("AllowedPattern", r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$"),
        ]
    )


def _emit_vpc_resource_mapping_decisions(ctx: _Context) -> None:
    """Emit D.vpc-resource-mapping entries for each R11 Parameter that was added."""
    if ctx.vpc_ids:
        _record_review(
            ctx,
            {
                "id": "D.vpc-resource-mapping.TargetVpcId",
                "kind": "vpc-resource-mapping",
                "category": "vpc-resource-mapping",
                "resource": "TargetVpcId",
                "parameter_name": "TargetVpcId",
                "source_ids": list(ctx.vpc_ids),
                "required_type": "AWS::EC2::VPC::Id",
                "suggested_action": "Supply the target-environment VPC ID.",
                "detail": (
                    "Parameter TargetVpcId replaces source VPC refs: "
                    + ", ".join(ctx.vpc_ids)
                ),
            },
        )
    if ctx.subnet_ids:
        _record_review(
            ctx,
            {
                "id": "D.vpc-resource-mapping.TargetSubnetIds",
                "kind": "vpc-resource-mapping",
                "category": "vpc-resource-mapping",
                "resource": "TargetSubnetIds",
                "parameter_name": "TargetSubnetIds",
                "source_ids": list(ctx.subnet_ids),
                "required_type": "CommaDelimitedList<AWS::EC2::Subnet::Id>",
                "suggested_action": "Supply comma-separated target-environment subnet IDs.",
                "detail": (
                    "Parameter TargetSubnetIds replaces source subnet refs: "
                    + ", ".join(ctx.subnet_ids)
                ),
            },
        )
    if ctx.security_group_ids:
        _record_review(
            ctx,
            {
                "id": "D.vpc-resource-mapping.TargetSecurityGroupIds",
                "kind": "vpc-resource-mapping",
                "category": "vpc-resource-mapping",
                "resource": "TargetSecurityGroupIds",
                "parameter_name": "TargetSecurityGroupIds",
                "source_ids": list(ctx.security_group_ids),
                "required_type": "CommaDelimitedList<AWS::EC2::SecurityGroup::Id>",
                "suggested_action": "Supply comma-separated target-environment security group IDs.",
                "detail": (
                    "Parameter TargetSecurityGroupIds replaces source SG refs: "
                    + ", ".join(ctx.security_group_ids)
                ),
            },
        )
