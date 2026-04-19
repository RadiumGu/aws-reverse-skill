"""Tests for references/former2-cfn.yaml — structure + security properties."""

import shutil
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

TEMPLATE_PATH = (
    Path(__file__).parent.parent / "references" / "former2-cfn.yaml"
)


@pytest.fixture(scope="module")
def template() -> dict:
    yaml = YAML()
    yaml.preserve_quotes = True
    with TEMPLATE_PATH.open(encoding="utf-8") as fh:
        return yaml.load(fh)


def test_template_has_parameters_resources_outputs(template):
    assert "Parameters" in template and template["Parameters"]
    assert "Resources" in template and template["Resources"]
    assert "Outputs" in template and template["Outputs"]
    for p in ("VpcId", "SubnetId", "InstanceType", "StackTag", "CreateEndpoints"):
        assert p in template["Parameters"], f"missing parameter {p}"
    for o in ("InstanceId", "PortForwardCommand", "TearDownCommand"):
        assert o in template["Outputs"], f"missing output {o}"


def test_instance_security_group_has_no_ingress(template):
    sg = template["Resources"]["InstanceSecurityGroup"]
    props = sg["Properties"]
    assert "SecurityGroupIngress" not in props or not props["SecurityGroupIngress"]
    assert props.get("SecurityGroupEgress"), "egress rules required"


def test_ec2_instance_security_and_iam(template):
    instance = template["Resources"]["Former2Instance"]
    props = instance["Properties"]
    # No public IP association
    assert props.get("AssociatePublicIpAddress") is not True
    assert "IamInstanceProfile" in props
    # IMDSv2 required
    meta = props.get("MetadataOptions", {})
    assert meta.get("HttpTokens") == "required"

    role = template["Resources"]["InstanceRole"]
    managed = role["Properties"]["ManagedPolicyArns"]
    joined = " ".join(managed)
    assert "AmazonSSMManagedInstanceCore" in joined
    assert "SecurityAudit" in joined
    assert "ReadOnlyAccess" not in joined

    policies = role["Properties"].get("Policies", [])
    policy_names = [p["PolicyName"] for p in policies]
    assert "DenySecretsAndParameters" in policy_names
    deny_policy = next(p for p in policies if p["PolicyName"] == "DenySecretsAndParameters")
    statements = deny_policy["PolicyDocument"]["Statement"]
    deny_actions = set()
    for stmt in statements:
        if stmt["Effect"] == "Deny":
            actions = stmt["Action"]
            if isinstance(actions, str):
                actions = [actions]
            deny_actions.update(actions)
    assert "secretsmanager:GetSecretValue" in deny_actions
    assert "ssm:GetParametersByPath" in deny_actions
    assert any(a.startswith("ssm:GetParameter") for a in deny_actions)


def test_cfn_lint_passes():
    if shutil.which("cfn-lint") is None:
        pytest.skip("cfn-lint not installed")
    result = subprocess.run(
        ["cfn-lint", str(TEMPLATE_PATH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"cfn-lint failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
