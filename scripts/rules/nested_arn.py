"""rules.nested_arn — R13 (string-embedded ARN) and R14 (JSON-embedded ARN) rewriting."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar

sys.path.insert(0, str(Path(__file__).parent.parent))
from arn_rewriter import rewrite_arns_in_text  # noqa: E402

from .common import _Context
from .identity import _ingest_rewrite_result


# ---------------------------------------------------------------------------
# R13 helpers
# ---------------------------------------------------------------------------


def _text_to_yaml_scalar(text: str) -> Any:
    """Turn a rewritten text back into a CFN scalar."""
    if "${" in text:
        return TaggedScalar(value=text, tag="!Sub")
    return text


def _apply_r13_to_scalar(
    ctx: _Context,
    value: str,
    location: str,
) -> Any:
    result = rewrite_arns_in_text(
        value,
        source_account=ctx.account_id,
        source_region=ctx.source_region,
    )
    rewrites = result.get("rewrites", [])
    if not rewrites:
        return None
    changed = any(r.get("action") != "unchanged" for r in rewrites)
    if not changed:
        return None
    _ingest_rewrite_result(ctx, result, location)
    return _text_to_yaml_scalar(result["new_text"])


def _walk_r13(node: Any, ctx: _Context, path: str) -> Any:
    """Recursively scan *node*; replace ARN-bearing string scalars in place."""
    if isinstance(node, TaggedScalar):
        tag = getattr(node, "tag", None)
        tag_value = getattr(tag, "value", str(tag)) if tag is not None else ""
        inner = getattr(node, "value", None)
        if tag_value == "!Sub" and isinstance(inner, str) and "arn:aws" in inner:
            result = rewrite_arns_in_text(
                inner,
                source_account=ctx.account_id,
                source_region=ctx.source_region,
            )
            rewrites = result.get("rewrites", [])
            if rewrites and any(r.get("action") != "unchanged" for r in rewrites):
                _ingest_rewrite_result(ctx, result, path)
                node.value = result["new_text"]
        return node
    if isinstance(node, str):
        if not node or "arn:aws" not in node:
            return node
        replacement = _apply_r13_to_scalar(ctx, node, path)
        if replacement is not None:
            return replacement
        return node
    if isinstance(node, CommentedMap):
        for key in list(node.keys()):
            child_path = f"{path}.{key}" if path else str(key)
            node[key] = _walk_r13(node[key], ctx, child_path)
        return node
    if isinstance(node, (CommentedSeq, list)):
        for i, item in enumerate(node):
            child_path = f"{path}[{i}]"
            node[i] = _walk_r13(item, ctx, child_path)
        return node
    return node


def apply_nested_arn_string_rule(
    resources: CommentedMap, ctx: _Context
) -> None:
    """R13 — recursively scan every string scalar for embedded ARNs."""
    if "nested_arn_string" not in ctx.rules:
        return
    if not isinstance(resources, CommentedMap):
        return
    for logical_id, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        props = resource.get("Properties")
        if not isinstance(props, CommentedMap):
            continue
        path = f"Resources.{logical_id}.Properties"
        resource["Properties"] = _walk_r13(props, ctx, path)


# ---------------------------------------------------------------------------
# R14 helpers
# ---------------------------------------------------------------------------


def _walk_json_for_arns(
    node: Any,
    ctx: _Context,
    location: str,
) -> Any:
    """Walk a plain Python JSON structure and rewrite ARN strings in place."""
    if isinstance(node, dict):
        new: dict[str, Any] = {}
        for k, v in node.items():
            new[k] = _walk_json_for_arns(v, ctx, f"{location}.{k}")
        return new
    if isinstance(node, list):
        return [_walk_json_for_arns(v, ctx, f"{location}[{i}]") for i, v in enumerate(node)]
    if isinstance(node, str) and "arn:aws" in node:
        result = rewrite_arns_in_text(
            node,
            source_account=ctx.account_id,
            source_region=ctx.source_region,
        )
        rewrites = result.get("rewrites", [])
        if rewrites and any(r.get("action") != "unchanged" for r in rewrites):
            _ingest_rewrite_result(ctx, result, location)
            return result["new_text"]
        return node
    return node


def _rewrite_step_functions_definition_string(
    ctx: _Context,
    props: CommentedMap,
    logical_id: str,
) -> None:
    if not isinstance(props, CommentedMap):
        return
    defn = props.get("DefinitionString")
    if defn is None:
        return
    location = f"Resources.{logical_id}.Properties.DefinitionString"
    wrapped_sub = False
    raw_text: str | None = None
    if isinstance(defn, TaggedScalar):
        tag = getattr(defn, "tag", None)
        tag_value = getattr(tag, "value", str(tag)) if tag is not None else ""
        if tag_value == "!Sub" and isinstance(defn.value, str):
            raw_text = defn.value
            wrapped_sub = True
        else:
            ctx.nested_arn_warnings.append(
                f"R14: {location} is a CFN intrinsic ({tag_value}) — skipped."
            )
            return
    elif isinstance(defn, str):
        raw_text = defn
    else:
        return
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        ctx.nested_arn_warnings.append(
            f"R14: {location} is not valid JSON ({exc}) — skipped."
        )
        return
    rewritten = _walk_json_for_arns(parsed, ctx, location)
    new_text = json.dumps(rewritten)
    if "${" in new_text or wrapped_sub:
        props["DefinitionString"] = TaggedScalar(value=new_text, tag="!Sub")
    else:
        props["DefinitionString"] = new_text


def _rewrite_policy_document(
    ctx: _Context,
    props: CommentedMap,
    logical_id: str,
    key: str,
) -> None:
    if not isinstance(props, CommentedMap):
        return
    doc = props.get(key)
    if doc is None:
        return
    location = f"Resources.{logical_id}.Properties.{key}"
    if isinstance(doc, TaggedScalar):
        ctx.nested_arn_warnings.append(
            f"R14: {location} is a CFN intrinsic ({doc.tag}) — skipped."
        )
        return
    if isinstance(doc, str):
        try:
            parsed = json.loads(doc)
        except json.JSONDecodeError as exc:
            ctx.nested_arn_warnings.append(
                f"R14: {location} string is not valid JSON ({exc}) — skipped."
            )
            return
        rewritten = _walk_json_for_arns(parsed, ctx, location)
        new_text = json.dumps(rewritten)
        if "${" in new_text:
            props[key] = TaggedScalar(value=new_text, tag="!Sub")
        else:
            props[key] = new_text
        return
    if not isinstance(doc, CommentedMap):
        return

    def _walk_dict(node: Any, path: str) -> Any:
        if isinstance(node, TaggedScalar):
            return node
        if isinstance(node, str):
            if "arn:aws" not in node:
                return node
            result = rewrite_arns_in_text(
                node,
                source_account=ctx.account_id,
                source_region=ctx.source_region,
            )
            rewrites = result.get("rewrites", [])
            if rewrites and any(r.get("action") != "unchanged" for r in rewrites):
                _ingest_rewrite_result(ctx, result, path)
                return _text_to_yaml_scalar(result["new_text"])
            return node
        if isinstance(node, CommentedMap):
            for k in list(node.keys()):
                node[k] = _walk_dict(node[k], f"{path}.{k}")
            return node
        if isinstance(node, dict):
            for k in list(node.keys()):
                node[k] = _walk_dict(node[k], f"{path}.{k}")
            return node
        if isinstance(node, (CommentedSeq, list)):
            for i, item in enumerate(node):
                node[i] = _walk_dict(item, f"{path}[{i}]")
            return node
        return node

    _walk_dict(doc, location)


_R14_STEP_FUNCTIONS_TYPES: frozenset[str] = frozenset(
    {"AWS::StepFunctions::StateMachine"}
)
_R14_POLICY_TYPES: frozenset[str] = frozenset(
    {
        "AWS::IAM::Role",
        "AWS::IAM::Policy",
        "AWS::IAM::ManagedPolicy",
        "AWS::S3::BucketPolicy",
        "AWS::SNS::TopicPolicy",
        "AWS::SQS::QueuePolicy",
        "AWS::KMS::Key",
    }
)


def apply_nested_arn_json_rule(
    resources: CommentedMap, ctx: _Context
) -> None:
    """R14 — rewrite ARNs embedded in JSON-bearing CFN properties."""
    if "nested_arn_json" not in ctx.rules:
        return
    if not isinstance(resources, CommentedMap):
        return
    for logical_id, resource in resources.items():
        if not isinstance(resource, CommentedMap):
            continue
        rtype = resource.get("Type")
        props = resource.get("Properties")
        if not isinstance(props, CommentedMap):
            continue
        if rtype in _R14_STEP_FUNCTIONS_TYPES:
            _rewrite_step_functions_definition_string(ctx, props, str(logical_id))
        if rtype in _R14_POLICY_TYPES:
            for key in ("PolicyDocument", "AssumeRolePolicyDocument"):
                if key in props:
                    _rewrite_policy_document(ctx, props, str(logical_id), key)
