#!/usr/bin/env python3
"""deployment_advice.py — Generate post-deploy action suggestions.

Reads raw.json (former2 scan) and emits ``deployment-advice.md`` based on
PRD §10 layered guidance (data / images / network / identity / containers /
observability / integration). Each rule fires per AWS resource type found in
the scan and attaches a CLI command template + solution ranking.

Formats:
    --format md         human-readable markdown (default)
    --format checklist  compact markdown checkbox list (embed into review.md)
    --format json       machine-consumable list
"""

import argparse
import ipaddress
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).parent))
from arn_rewriter import find_arns  # noqa: E402


# ---------------------------------------------------------------------------
# Advice data model
# ---------------------------------------------------------------------------


@dataclass
class Advice:
    """A single piece of deployment advice."""

    layer: str  # data / image / network / identity / container / observability / integration
    priority: str  # red / yellow / gray
    resource_type: str
    title: str
    detail: str
    options: list[str] = field(default_factory=list)  # ranked solutions (best first)
    commands: list[str] = field(default_factory=list)  # CLI snippets
    resources: list[str] = field(default_factory=list)  # physical ids


PRIORITY_LABEL = {
    "red": "🔴",
    "yellow": "🟡",
    "gray": "⚪",
}

LAYER_LABEL = {
    "data": "数据层",
    "image": "镜像/工件",
    "network": "网络层",
    "identity": "身份与权限",
    "container": "容器与编排",
    "observability": "可观测性",
    "integration": "集成与事件",
    "cross-account": "🔗 跨账号集成梳理",
}


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------


RuleFn = Callable[[list[dict[str, Any]]], list[Advice]]


def _of_type(
    resources: list[dict[str, Any]], type_name: str
) -> list[dict[str, Any]]:
    return [r for r in resources if r.get("Type") == type_name]


def rule_rds(resources: list[dict[str, Any]]) -> list[Advice]:
    rds = _of_type(resources, "AWS::RDS::DBInstance") + _of_type(
        resources, "AWS::RDS::DBCluster"
    )
    if not rds:
        return []
    return [
        Advice(
            layer="data",
            priority="red",
            resource_type="AWS::RDS::DBInstance",
            title=f"RDS 数据不自动迁移（{len(rds)} 项）",
            detail="骨架模板只建空实例；源实例数据需独立方案。",
            options=[
                "⭐ AWS DMS 持续同步（低停机，推荐生产）",
                "Snapshot + Copy + Restore（接受停机窗口）",
                "Aurora Global Database（仅限 Aurora 引擎）",
                "放弃数据，空库启动（dev/uat 可用）",
            ],
            commands=[
                "aws rds create-db-snapshot --db-instance-identifier <src> --db-snapshot-identifier <snap>",
                "aws rds copy-db-snapshot --source-db-snapshot-identifier <snap> --target-db-snapshot-identifier <snap-tgt> --source-region <src> --region <tgt>",
                "aws rds restore-db-instance-from-db-snapshot --db-instance-identifier <new> --db-snapshot-identifier <snap-tgt>",
            ],
            resources=[r.get("PhysicalId", "") for r in rds],
        )
    ]


def rule_dynamodb(resources: list[dict[str, Any]]) -> list[Advice]:
    tables = _of_type(resources, "AWS::DynamoDB::Table")
    if not tables:
        return []
    return [
        Advice(
            layer="data",
            priority="red",
            resource_type="AWS::DynamoDB::Table",
            title=f"DynamoDB 表 ({len(tables)} 项)",
            detail="表结构会部署，但数据不会复制。",
            options=[
                "⭐ DynamoDB Global Tables（双向复制）",
                "Export to S3 + Import",
                "放弃数据（session/cache 类表）",
            ],
            commands=[
                "aws dynamodb update-table --table-name <name> --replica-updates \\'[{\"Create\":{\"RegionName\":\"<target>\"}}]\\'",
            ],
            resources=[r.get("PhysicalId", "") for r in tables],
        )
    ]


def rule_s3(resources: list[dict[str, Any]]) -> list[Advice]:
    buckets = _of_type(resources, "AWS::S3::Bucket")
    if not buckets:
        return []
    return [
        Advice(
            layer="data",
            priority="red",
            resource_type="AWS::S3::Bucket",
            title=f"S3 对象 ({len(buckets)} 个 bucket)",
            detail="Bucket 会创建，但对象需独立复制。",
            options=[
                "⭐ S3 Cross-Region Replication (CRR)（实时）",
                "S3 Batch Replication（历史对象）",
                "aws s3 sync（小数据量，简单）",
            ],
            commands=[
                "aws s3 sync s3://<src-bucket> s3://<tgt-bucket> --source-region <src> --region <tgt>",
            ],
            resources=[r.get("PhysicalId", "") for r in buckets],
        )
    ]


def rule_efs(resources: list[dict[str, Any]]) -> list[Advice]:
    efs = _of_type(resources, "AWS::EFS::FileSystem")
    if not efs:
        return []
    return [
        Advice(
            layer="data",
            priority="red",
            resource_type="AWS::EFS::FileSystem",
            title=f"EFS 文件数据 ({len(efs)} 项)",
            detail="文件系统会创建，但数据不自动迁移。",
            options=["AWS DataSync", "EFS Replication（EFS 原生）"],
            commands=[
                "aws datasync create-task --source-location-arn <arn> --destination-location-arn <arn>",
            ],
            resources=[r.get("PhysicalId", "") for r in efs],
        )
    ]


def rule_secrets(resources: list[dict[str, Any]]) -> list[Advice]:
    secrets = _of_type(resources, "AWS::SecretsManager::Secret")
    if not secrets:
        return []
    return [
        Advice(
            layer="data",
            priority="yellow",
            resource_type="AWS::SecretsManager::Secret",
            title=f"Secrets Manager 密文 ({len(secrets)} 项)",
            detail="Secret 容器会创建，值需手工迁移。",
            options=["Secrets Manager replication", "手工 get-secret-value + create-secret"],
            commands=[
                "aws secretsmanager get-secret-value --secret-id <src-arn>",
                "aws secretsmanager create-secret --name <name> --secret-string <value>",
            ],
            resources=[r.get("PhysicalId", "") for r in secrets],
        )
    ]


def rule_ami(resources: list[dict[str, Any]]) -> list[Advice]:
    ec2s = _of_type(resources, "AWS::EC2::Instance")
    # Only emit if any AMI references are present
    ami_ids = [r.get("ImageId", "") for r in ec2s if r.get("ImageId")]
    if not ami_ids:
        return []
    return [
        Advice(
            layer="image",
            priority="yellow",
            resource_type="AWS::EC2::Instance",
            title=f"AMI 跨 region Copy ({len(ami_ids)} 项)",
            detail="ImageId 是 region 专属，跨 region 部署需先 copy。",
            options=["⭐ aws ec2 copy-image", "AWS Backup 跨 region", "重新 Image Builder 构建"],
            commands=[
                "aws ec2 copy-image --source-region <src> --source-image-id <ami-xxx> --region <tgt> --name <name>",
            ],
            resources=ami_ids,
        )
    ]


def rule_ecr(resources: list[dict[str, Any]]) -> list[Advice]:
    repos = _of_type(resources, "AWS::ECR::Repository")
    if not repos:
        return []
    return [
        Advice(
            layer="image",
            priority="yellow",
            resource_type="AWS::ECR::Repository",
            title=f"ECR 容器镜像 ({len(repos)} 仓库)",
            detail="仓库会创建，镜像 layer 需独立同步。",
            options=["⭐ ECR Replication（原生跨 region/跨账号）", "docker pull + push"],
            commands=[
                "aws ecr put-replication-configuration --replication-configuration '{\"rules\":[{\"destinations\":[{\"region\":\"<tgt>\",\"registryId\":\"<tgt-acct>\"}]}]}'",
            ],
            resources=[r.get("PhysicalId", "") for r in repos],
        )
    ]


def rule_vpc_peering(resources: list[dict[str, Any]]) -> list[Advice]:
    peering = _of_type(resources, "AWS::EC2::VPCPeeringConnection")
    if not peering:
        return []
    return [
        Advice(
            layer="network",
            priority="yellow",
            resource_type="AWS::EC2::VPCPeeringConnection",
            title=f"VPC Peering ({len(peering)} 项)",
            detail="跨账号 Peering 需双边握手；CFN 不自动完成。",
            options=["目标账号执行 accept-vpc-peering-connection", "改用 Transit Gateway + RAM 共享"],
            commands=[
                "aws ec2 accept-vpc-peering-connection --vpc-peering-connection-id <pcx-xxx>",
            ],
            resources=[r.get("PhysicalId", "") for r in peering],
        )
    ]


def rule_tgw(resources: list[dict[str, Any]]) -> list[Advice]:
    tgw = _of_type(resources, "AWS::EC2::TransitGatewayAttachment") + _of_type(
        resources, "AWS::EC2::TransitGatewayVpcAttachment"
    )
    if not tgw:
        return []
    return [
        Advice(
            layer="network",
            priority="yellow",
            resource_type="AWS::EC2::TransitGatewayAttachment",
            title=f"Transit Gateway 附件 ({len(tgw)} 项)",
            detail="跨账号 TGW attachment 需 RAM 共享。",
            options=["RAM 共享 TGW", "每账号独立 attachment"],
            commands=[
                "aws ram create-resource-share --name tgw-share --resource-arns <tgw-arn>",
            ],
            resources=[r.get("PhysicalId", "") for r in tgw],
        )
    ]


def rule_route53(resources: list[dict[str, Any]]) -> list[Advice]:
    zones = _of_type(resources, "AWS::Route53::HostedZone")
    if not zones:
        return []
    return [
        Advice(
            layer="network",
            priority="yellow",
            resource_type="AWS::Route53::HostedZone",
            title=f"Route53 Hosted Zone ({len(zones)} 项)",
            detail="Private Zone 跨账号关联需要 cross-account association。",
            options=["使用现有 Zone ID + cross-account association", "目标账号新建 Zone"],
            commands=[
                "aws route53 associate-vpc-with-hosted-zone --hosted-zone-id <zid> --vpc VPCRegion=<r>,VPCId=<vpc>",
            ],
            resources=[r.get("PhysicalId", "") for r in zones],
        )
    ]


def rule_iam_trust(resources: list[dict[str, Any]]) -> list[Advice]:
    roles = _of_type(resources, "AWS::IAM::Role")
    external_roles: list[str] = []
    for r in roles:
        trust = r.get("AssumeRolePolicyDocument") or {}
        text = json.dumps(trust)
        if ":iam::" in text:
            external_roles.append(r.get("PhysicalId", ""))
    if not external_roles:
        return []
    return [
        Advice(
            layer="identity",
            priority="yellow",
            resource_type="AWS::IAM::Role",
            title=f"IAM Role trust policy ({len(external_roles)} 项)",
            detail="跨账号 trust 需确认目标环境依然信任源 principal。",
            options=["替换为目标账号 principal", "保留跨账号 trust（白名单确认）"],
            commands=[],
            resources=external_roles,
        )
    ]


def rule_kms_cross(resources: list[dict[str, Any]]) -> list[Advice]:
    keys = _of_type(resources, "AWS::KMS::Key")
    if not keys:
        return []
    multi_region = [k for k in keys if k.get("MultiRegion")]
    return [
        Advice(
            layer="identity",
            priority="yellow",
            resource_type="AWS::KMS::Key",
            title=f"KMS CMK ({len(keys)} 项)",
            detail="密钥材料不可导出；目标账号需新建 CMK 或使用 multi-region key。",
            options=[
                "⭐ 目标账号新建 CMK（简单安全）",
                f"KMS multi-region keys（已识别 {len(multi_region)} 个 MRK）",
                "Cross-account grant（临时）",
            ],
            commands=[
                "aws kms create-key --description '<desc>' --key-usage ENCRYPT_DECRYPT",
            ],
            resources=[k.get("PhysicalId", "") for k in keys],
        )
    ]


def rule_eks(resources: list[dict[str, Any]]) -> list[Advice]:
    clusters = _of_type(resources, "AWS::EKS::Cluster")
    if not clusters:
        return []
    return [
        Advice(
            layer="container",
            priority="yellow",
            resource_type="AWS::EKS::Cluster",
            title=f"EKS 集群 ({len(clusters)} 项)",
            detail="K8s workload / RBAC / ConfigMap 不在 CFN scope 内。",
            options=["⭐ GitOps (ArgoCD / Flux) 重新 apply", "Velero 跨集群备份恢复", "kubectl 导出 YAML 后 apply"],
            commands=[
                "velero backup create <name> --include-namespaces <ns>",
                "velero restore create --from-backup <name>",
            ],
            resources=[c.get("PhysicalId", "") for c in clusters],
        )
    ]


def rule_cloudwatch_logs(resources: list[dict[str, Any]]) -> list[Advice]:
    groups = _of_type(resources, "AWS::Logs::LogGroup")
    if not groups:
        return []
    return [
        Advice(
            layer="observability",
            priority="gray",
            resource_type="AWS::Logs::LogGroup",
            title=f"CloudWatch Log Group ({len(groups)} 项)",
            detail="Log group 会创建，但历史日志不迁移。",
            options=["CloudWatch Logs subscription cross-region", "S3 Export（冷数据）"],
            commands=[
                "aws logs create-export-task --log-group-name <name> --destination <bucket> --destination-prefix logs/",
            ],
            resources=[g.get("PhysicalId", "") for g in groups],
        )
    ]


def rule_eventbridge(resources: list[dict[str, Any]]) -> list[Advice]:
    rules = _of_type(resources, "AWS::Events::Rule")
    if not rules:
        return []
    return [
        Advice(
            layer="integration",
            priority="gray",
            resource_type="AWS::Events::Rule",
            title=f"EventBridge Rule ({len(rules)} 项)",
            detail="跨账号 EventBridge bus 需 resource policy 手工确认。",
            options=["目标 bus put-permission 允许源账号"],
            commands=[
                "aws events put-permission --action events:PutEvents --principal <src-acct> --statement-id <id>",
            ],
            resources=[r.get("PhysicalId", "") for r in rules],
        )
    ]


def rule_acm(resources: list[dict[str, Any]]) -> list[Advice]:
    certs = _of_type(resources, "AWS::CertificateManager::Certificate")
    if not certs:
        return []
    return [
        Advice(
            layer="integration",
            priority="yellow",
            resource_type="AWS::CertificateManager::Certificate",
            title=f"ACM 证书 ({len(certs)} 项)",
            detail="ACM 证书是 region 专属，目标 region 需重新申请 + DNS 验证。",
            options=["目标 region request-certificate + DNS validation", "使用 ACM Private CA"],
            commands=[
                "aws acm request-certificate --domain-name <domain> --validation-method DNS --region <tgt>",
            ],
            resources=[c.get("PhysicalId", "") for c in certs],
        )
    ]


_RFC1918_RANGES: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_public_cidr(cidr: str) -> bool:
    """Return True if *cidr* is a non-RFC1918 IPv4 CIDR (treated as public).

    Skips IPv6, the unspecified ``0.0.0.0/0`` default route, and any CIDR that
    is a subnet of an RFC1918 range.
    """
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError):
        return False
    if isinstance(net, ipaddress.IPv6Network):
        return False
    if net.prefixlen == 0:
        return False
    return not any(net.subnet_of(rfc) for rfc in _RFC1918_RANGES)


def _collect_sg_cidrs(
    sg_resource: dict[str, Any],
) -> list[str]:
    """Return every CidrIp / CidrIpv6 value attached to a SecurityGroup entry.

    Accepts either the flattened former2 scan record (where ingress/egress
    live at the top level) or a nested ``Properties`` shape that mirrors the
    CFN structure.
    """
    cidrs: list[str] = []
    containers: list[Any] = []

    props = sg_resource.get("Properties")
    if isinstance(props, dict):
        containers.append(props)
    containers.append(sg_resource)

    for container in containers:
        for key in ("SecurityGroupIngress", "SecurityGroupEgress",
                    "IpPermissions", "IpPermissionsEgress"):
            rules = container.get(key)
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                for cidr_key in ("CidrIp", "CidrIpv6"):
                    val = rule.get(cidr_key)
                    if isinstance(val, str) and val:
                        cidrs.append(val)
                # former2 nested shape: IpRanges / Ipv6Ranges
                for list_key, range_key in (
                    ("IpRanges", "CidrIp"),
                    ("Ipv6Ranges", "CidrIpv6"),
                ):
                    entries = rule.get(list_key)
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if isinstance(entry, dict):
                            val = entry.get(range_key)
                            if isinstance(val, str) and val:
                                cidrs.append(val)
    return cidrs


def rule_prefix_list(resources: list[dict[str, Any]]) -> list[Advice]:
    """Emit PrefixList advice when 2+ SGs share at least one public CIDR.

    Private (RFC1918) CIDRs and the default route ``0.0.0.0/0`` are ignored;
    the optimization only pays off for public ranges that would otherwise be
    repeated literal allow-lists across multiple SGs.
    """
    sgs = _of_type(resources, "AWS::EC2::SecurityGroup")
    if len(sgs) < 2:
        return []

    cidr_to_sgs: dict[str, list[str]] = {}
    for sg in sgs:
        sg_name = sg.get("PhysicalId") or sg.get("LogicalId") or "unknown-sg"
        seen: set[str] = set()
        for cidr in _collect_sg_cidrs(sg):
            if cidr in seen or not _is_public_cidr(cidr):
                continue
            seen.add(cidr)
            cidr_to_sgs.setdefault(cidr, []).append(str(sg_name))

    shared = {
        cidr: sorted(set(owners))
        for cidr, owners in cidr_to_sgs.items()
        if len(set(owners)) >= 2
    }
    if not shared:
        return []

    detail_lines = [
        f"Duplicate public CIDRs detected across {len({sg for owners in shared.values() for sg in owners})} security groups."
    ]
    for cidr in sorted(shared):
        detail_lines.append(
            f"- {cidr} appears in: {', '.join(shared[cidr])}"
        )
    detail_lines.append(
        "Recommended: create a Customer-Managed Prefix List (pl-xxx) and "
        "reference it via SourcePrefixListId on each rule."
    )

    return [
        Advice(
            layer="network",
            priority="yellow",
            resource_type="AWS::EC2::SecurityGroup",
            title=(
                f"PrefixList 优化建议 — {len(shared)} 个公网 CIDR 在 "
                f"{len({sg for owners in shared.values() for sg in owners})} 个 SG 中重复"
            ),
            detail="\n".join(detail_lines),
            options=[
                "⭐ 创建 Customer-Managed Prefix List 集中维护公网 CIDR 列表",
                "在每条 SG 规则用 SourcePrefixListId 引用 PrefixList",
                "保留现状（若 CIDR 条目 < 5 且不会扩展）",
            ],
            commands=[
                "aws ec2 create-managed-prefix-list --prefix-list-name <name> "
                "--address-family IPv4 --max-entries <N> "
                "--entries Cidr=<cidr>,Description=<desc>",
                "aws ec2 modify-managed-prefix-list --prefix-list-id <pl-xxx> "
                "--add-entries Cidr=<cidr>,Description=<desc>",
            ],
            resources=sorted(shared),
        )
    ]


def _get_props(resource: dict[str, Any]) -> dict[str, Any]:
    """Return the Properties dict for a raw-scan resource, tolerating flat layouts."""
    props = resource.get("Properties")
    if isinstance(props, dict):
        return props
    return resource


def _collect_cross_account_arns(
    text_values: list[str], source_account: str
) -> list[dict[str, Any]]:
    """Scan *text_values* and return records for ARNs outside *source_account*."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in text_values:
        if not isinstance(value, str):
            continue
        for match in find_arns(value):
            if not match.get("is_known_service"):
                continue
            account = match.get("account") or ""
            if not account or account == source_account:
                continue
            arn = match["arn"]
            if arn in seen:
                continue
            seen.add(arn)
            out.append({
                "arn": arn,
                "service": match.get("service", ""),
                "account": account,
            })
    return out


def _extract_stepfn_definition_strings(
    resource: dict[str, Any],
) -> list[str]:
    """Return candidate definition strings for a Step Functions state machine."""
    props = _get_props(resource)
    values: list[str] = []
    for key in ("DefinitionString", "Definition"):
        val = props.get(key)
        if isinstance(val, str):
            values.append(val)
        elif isinstance(val, dict):
            values.append(json.dumps(val))
    return values


def _extract_lambda_env_values(resource: dict[str, Any]) -> list[str]:
    """Return every Environment.Variables.* string value for a Lambda function."""
    props = _get_props(resource)
    env = props.get("Environment") or {}
    if not isinstance(env, dict):
        return []
    variables = env.get("Variables")
    if not isinstance(variables, dict):
        return []
    return [v for v in variables.values() if isinstance(v, str)]


def rule_cross_account_step_functions(
    resources: list[dict[str, Any]], source_account: str = ""
) -> list[Advice]:
    """Emit advice when Step Functions reference Lambda/SQS from other accounts."""
    if not source_account:
        return []
    machines = _of_type(resources, "AWS::StepFunctions::StateMachine")
    if not machines:
        return []

    hits: list[dict[str, Any]] = []
    for sm in machines:
        defs = _extract_stepfn_definition_strings(sm)
        arns = _collect_cross_account_arns(defs, source_account)
        if not arns:
            continue
        sm_id = sm.get("PhysicalId") or sm.get("LogicalId") or "unknown-state-machine"
        for record in arns:
            hits.append({**record, "state_machine": str(sm_id)})

    if not hits:
        return []

    accounts = sorted({h["account"] for h in hits})
    detail_lines = [
        (
            f"State machines reference Lambda/SQS/etc. from "
            f"{len(accounts)} external account(s): {', '.join(accounts)}. "
            "Verify target-account resource policies before deploying."
        )
    ]
    service_counter = Counter(h["service"] for h in hits)
    for service, count in sorted(service_counter.items()):
        detail_lines.append(f"- {service}: {count} ARN(s)")
    for h in hits[:5]:
        detail_lines.append(
            f"- {h['service']}: `{h['arn']}` (from {h['state_machine']})"
        )
    if len(hits) > 5:
        detail_lines.append(f"- ... {len(hits) - 5} more")

    return [
        Advice(
            layer="cross-account",
            priority="yellow",
            resource_type="AWS::StepFunctions::StateMachine",
            title=f"Step Functions 跨账号引用 ({len(hits)} 个)",
            detail="\n".join(detail_lines),
            options=[
                "⭐ 在目标账号为每个被调用资源授予 invoke/send 权限",
                "使用 shared-services account 集中托管跨账号 Lambda",
                "改走 EventBridge + cross-account bus（解耦）",
            ],
            commands=[
                "aws lambda add-permission --function-name <name> "
                "--statement-id states-cross-account --action lambda:InvokeFunction "
                "--principal states.amazonaws.com --source-account <src-acct>",
                "aws sqs add-permission --queue-url <url> --label states-cross-account "
                "--aws-account-ids <src-acct> --actions SendMessage",
            ],
            resources=sorted({h["arn"] for h in hits}),
        )
    ]


def rule_cross_account_lambda_env(
    resources: list[dict[str, Any]], source_account: str = ""
) -> list[Advice]:
    """Emit advice when Lambda Environment.Variables carry cross-account ARNs."""
    if not source_account:
        return []
    funcs = _of_type(resources, "AWS::Lambda::Function")
    if not funcs:
        return []

    hits: list[dict[str, Any]] = []
    for fn in funcs:
        values = _extract_lambda_env_values(fn)
        arns = _collect_cross_account_arns(values, source_account)
        if not arns:
            continue
        fn_id = fn.get("PhysicalId") or fn.get("LogicalId") or "unknown-function"
        for record in arns:
            hits.append({**record, "function": str(fn_id)})

    if not hits:
        return []

    accounts = sorted({h["account"] for h in hits})
    detail_lines = [
        (
            f"Lambda Environment.Variables carry ARNs owned by "
            f"{len(accounts)} external account(s): {', '.join(accounts)}. "
            "Supply the target-environment equivalents before deploy."
        )
    ]
    for h in hits[:5]:
        detail_lines.append(
            f"- {h['service']}: `{h['arn']}` (from {h['function']})"
        )
    if len(hits) > 5:
        detail_lines.append(f"- ... {len(hits) - 5} more")

    return [
        Advice(
            layer="cross-account",
            priority="yellow",
            resource_type="AWS::Lambda::Function",
            title=f"Lambda Env cross-account ARN ({len(hits)} 个)",
            detail="\n".join(detail_lines),
            options=[
                "⭐ 在目标账号提供同名替代资源，走 Parameter 填值",
                "改用 Secrets Manager / SSM Parameter 中心化下发",
                "保留并请求目标账号授予跨账号访问权限（最后手段）",
            ],
            commands=[
                "aws lambda update-function-configuration --function-name <name> "
                "--environment 'Variables={KEY=<target-arn>}'",
            ],
            resources=sorted({h["arn"] for h in hits}),
        )
    ]


ALL_RULES: list[RuleFn] = [
    rule_rds,
    rule_dynamodb,
    rule_s3,
    rule_efs,
    rule_secrets,
    rule_ami,
    rule_ecr,
    rule_vpc_peering,
    rule_tgw,
    rule_route53,
    rule_prefix_list,
    rule_iam_trust,
    rule_kms_cross,
    rule_eks,
    rule_cloudwatch_logs,
    rule_eventbridge,
    rule_acm,
]


def _integration_cross_account_rules(
    resources: list[dict[str, Any]], source_account: str
) -> list[Advice]:
    """Run the source-account-aware cross-account ARN rules."""
    return [
        *rule_cross_account_step_functions(resources, source_account),
        *rule_cross_account_lambda_env(resources, source_account),
    ]


def generate_advice(
    resources: list[dict[str, Any]], source_account: str = ""
) -> list[Advice]:
    """Run all rules and return the collected :class:`Advice` list.

    When *source_account* is supplied, the cross-account ARN rules
    (Step Functions Definition + Lambda Env) also run and contribute to the
    integration layer. Omitted for backward compatibility with callers that
    cannot determine the source account.
    """
    out: list[Advice] = []
    for rule in ALL_RULES:
        out.extend(rule(resources))
    if source_account:
        out.extend(_integration_cross_account_rules(resources, source_account))
    return out


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def group_by_layer(advice_list: list[Advice]) -> dict[str, list[Advice]]:
    groups: dict[str, list[Advice]] = {}
    for a in advice_list:
        groups.setdefault(a.layer, []).append(a)
    return groups


def render_markdown(advice_list: list[Advice], stack_name: str = "") -> str:
    lines = [
        f"# 后续部署建议 — {stack_name or 'unnamed-stack'}",
        "",
        f"本次扫描共识别 {sum(len(a.resources) for a in advice_list)} 个需要额外处理的资源，分 {len(group_by_layer(advice_list))} 层：",
        "",
    ]
    for layer, group in group_by_layer(advice_list).items():
        label = LAYER_LABEL.get(layer, layer)
        pri_icon = PRIORITY_LABEL.get(group[0].priority if group else "gray", "⚪")
        lines.append(f"## {pri_icon} {label}（{len(group)} 项）")
        lines.append("")
        for a in group:
            lines.append(f"### {a.resource_type} — {a.title}")
            lines.append(a.detail)
            if a.options:
                lines.append("")
                lines.append("**推荐方案**:")
                for o in a.options:
                    lines.append(f"- {o}")
            if a.commands:
                lines.append("")
                lines.append("**命令模板**:")
                lines.append("```bash")
                lines.extend(a.commands)
                lines.append("```")
            if a.resources:
                lines.append("")
                lines.append(
                    f"**涉及资源** ({len(a.resources)}): "
                    + ", ".join(a.resources[:5])
                    + (" ..." if len(a.resources) > 5 else "")
                )
            lines.append("")
    return "\n".join(lines)


def render_checklist(advice_list: list[Advice]) -> str:
    lines = ["# 后续部署 Checklist", ""]
    for a in advice_list:
        lines.append(f"- [ ] **{a.title}** — {a.detail}")
        for o in a.options[:1]:
            lines.append(f"      推荐: {o}")
    return "\n".join(lines)


def render_json(advice_list: list[Advice]) -> str:
    return json.dumps(
        [
            {
                "layer": a.layer,
                "priority": a.priority,
                "resource_type": a.resource_type,
                "title": a.title,
                "detail": a.detail,
                "options": a.options,
                "commands": a.commands,
                "resources": a.resources,
            }
            for a in advice_list
        ],
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate post-deploy advice from raw.json (PRD §10 rules)."
    )
    parser.add_argument("--input", required=True, help="Path to raw.json")
    parser.add_argument("--output", default="", help="Output path (default: stdout)")
    parser.add_argument(
        "--format",
        choices=["md", "checklist", "json"],
        default="md",
    )
    parser.add_argument("--stack-name", default="")
    parser.add_argument(
        "--source-account",
        default="",
        help=(
            "Optional 12-digit source account. Enables cross-account-ARN "
            "advice for Step Functions Definition + Lambda Env."
        ),
    )
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"ERROR: input not found: {path}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    resources = data.get("resources", []) if isinstance(data, dict) else data

    advice = generate_advice(resources, source_account=args.source_account)

    if args.format == "md":
        text = render_markdown(advice, args.stack_name)
    elif args.format == "checklist":
        text = render_checklist(advice)
    else:
        text = render_json(advice)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote advice to {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
