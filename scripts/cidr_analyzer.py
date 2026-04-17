#!/usr/bin/env python3
"""cidr_analyzer.py — Classify a CIDR against a set of source VPC CIDRs.

Used by R11/R12 (VPC resource / SG CIDR rewriting) to decide how each CIDR
reference in the source template should be handled in the target environment:

  * ``vpc-internal``       — subset of (or equal to) a known source VPC CIDR.
                             Target env will need an equivalent VPC range;
                             rewriter can point at ``!GetAtt TargetVpc.CidrBlock``
                             or a matching Parameter.
  * ``rfc1918-external``   — RFC1918 space but not inside any source VPC.
                             Typical for on-prem / peer / shared-services ranges.
                             Surface to the reviewer as "confirm target network".
  * ``aws-public-range``   — Public ranges commonly used by AWS services
                             (simplified heuristic: first octet ∈ {13,52,54}
                             and not RFC1918). Suggest PrefixList.
  * ``public``             — Everything else (incl. ``0.0.0.0/0``). Keep as-is.

Only ``ipaddress`` from the stdlib is used.

CLI:
    python scripts/cidr_analyzer.py \\
        --cidr 10.0.1.0/24 --source-vpc-cidrs 10.0.0.0/16,172.31.0.0/16
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from typing import Any

_RFC1918_RANGES: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

#: Simplified AWS public first-octet heuristic. A richer implementation would
#: consult ip-ranges.json; for classification purposes (advice only) the octet
#: check is sufficient and avoids a network dependency.
_AWS_PUBLIC_FIRST_OCTETS: frozenset[int] = frozenset({13, 52, 54})


def _is_rfc1918(net: ipaddress.IPv4Network) -> bool:
    """Return True if *net* is fully contained in any RFC1918 range."""
    return any(net.subnet_of(rfc) for rfc in _RFC1918_RANGES)


def _first_octet(net: ipaddress.IPv4Network) -> int:
    """Return the first octet of the network address."""
    return int(net.network_address.packed[0])


def classify_cidr(cidr: str, source_vpc_cidrs: list[str]) -> dict[str, Any]:
    """Classify *cidr* relative to a list of source VPC CIDRs.

    Args:
        cidr: The CIDR to classify (e.g. ``"10.0.1.0/24"``).
        source_vpc_cidrs: Source VPC CIDR blocks (e.g. ``["10.0.0.0/16"]``).
            Invalid entries are silently skipped.

    Returns:
        Dict with keys:

        * ``category``         — one of ``vpc-internal`` / ``rfc1918-external``
                                 / ``public`` / ``aws-public-range``.
        * ``matched_vpc_cidr`` — the source VPC CIDR that contains *cidr*,
                                 or ``None``.
        * ``is_rfc1918``       — whether *cidr* is in RFC1918 space.
        * ``notes``            — short human-readable reason.

    The special CIDR ``0.0.0.0/0`` is always classified as ``public``.
    """
    net = ipaddress.ip_network(cidr, strict=False)

    # 0.0.0.0/0 → always public (any-route)
    if net.prefixlen == 0:
        return {
            "category": "public",
            "matched_vpc_cidr": None,
            "is_rfc1918": False,
            "notes": "Default route (0.0.0.0/0) — treat as public / internet-bound.",
        }

    # IPv6 — treat as public by default; full IPv6 support is out of scope.
    if isinstance(net, ipaddress.IPv6Network):
        return {
            "category": "public",
            "matched_vpc_cidr": None,
            "is_rfc1918": False,
            "notes": "IPv6 CIDR — classifier does not inspect IPv6 VPC membership.",
        }

    is_rfc1918 = _is_rfc1918(net)

    for src in source_vpc_cidrs:
        try:
            src_net = ipaddress.ip_network(src, strict=False)
        except ValueError:
            continue
        if isinstance(src_net, ipaddress.IPv6Network):
            continue
        if net.subnet_of(src_net):
            return {
                "category": "vpc-internal",
                "matched_vpc_cidr": str(src_net),
                "is_rfc1918": is_rfc1918,
                "notes": (
                    f"CIDR {net} is inside source VPC range {src_net}; "
                    "remap to target VPC CIDR."
                ),
            }

    if is_rfc1918:
        return {
            "category": "rfc1918-external",
            "matched_vpc_cidr": None,
            "is_rfc1918": True,
            "notes": (
                "RFC1918 range outside the known source VPC(s) — likely "
                "on-prem / peer network. Confirm with target environment."
            ),
        }

    if _first_octet(net) in _AWS_PUBLIC_FIRST_OCTETS:
        return {
            "category": "aws-public-range",
            "matched_vpc_cidr": None,
            "is_rfc1918": False,
            "notes": (
                "Range falls inside an AWS-owned public block (heuristic on "
                "first octet). Consider replacing with a managed PrefixList."
            ),
        }

    return {
        "category": "public",
        "matched_vpc_cidr": None,
        "is_rfc1918": False,
        "notes": "Public (non-RFC1918) CIDR — leave as-is or audit manually.",
    }


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Classify a CIDR against a set of source VPC CIDRs.",
    )
    parser.add_argument("--cidr", required=True, help="CIDR to classify.")
    parser.add_argument(
        "--source-vpc-cidrs",
        default="",
        help="Comma-separated list of source VPC CIDRs (e.g. 10.0.0.0/16,10.1.0.0/16).",
    )
    args = parser.parse_args()

    sources = [c.strip() for c in args.source_vpc_cidrs.split(",") if c.strip()]
    result = classify_cidr(args.cidr, sources)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
