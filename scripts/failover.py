#!/usr/bin/env python3
"""
Health-based DNS failover between two CDN fronts by switching a router CNAME.

Usage:
    python scripts/failover.py --config config/failover.example.com.yml
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import httpx

from common import (
    CloudflareClient,
    DeSecClient,
    load_failover_config,
    get_env_or_raise,
    ConfigError,
    ProviderError,
)


def check_health(url: str, expected_status: int, timeout_seconds: int) -> bool:
    try:
        resp = httpx.get(url, timeout=timeout_seconds)
        return resp.status_code == expected_status
    except httpx.RequestError:
        return False


def fqdn(domain: str, name: str) -> str:
    if name.endswith("."):
        return name.rstrip(".")
    if name == domain:
        return domain
    if name.endswith("." + domain):
        return name
    return f"{name}.{domain}"


def current_target_info(
    cf: CloudflareClient,
    zone_id: str,
    router_fqdn: str,
) -> Optional[str]:
    """
    Returns the current CNAME target content for router_fqdn on Cloudflare, or None.
    """
    rec = cf.get_dns_record(zone_id, router_fqdn, "CNAME")
    if not rec:
        return None
    return rec.get("content")


def set_router_target(
    cf: CloudflareClient,
    desec: DeSecClient,
    zone_name: str,
    zone_id: str,
    router_name: str,
    router_fqdn: str,
    new_target_fqdn: str,
    ttl: int = 60,
) -> None:
    """
    Set router CNAME target in both providers.
    """
    print(f"[INFO] Setting router {router_fqdn} -> {new_target_fqdn}")

    # Cloudflare
    cf.upsert_dns_record(
        zone_id=zone_id,
        name=router_fqdn,
        rtype="CNAME",
        content=new_target_fqdn,
        ttl=ttl,
    )

    # deSEC: subname relative to apex
    from common import DnsRecordConfig

    rec_cfg = DnsRecordConfig(
        domain=zone_name,
        name=router_fqdn,
        type="CNAME",
        ttl=ttl,
        values=[new_target_fqdn + "." if not new_target_fqdn.endswith(".") else new_target_fqdn],
    )
    desec.upsert_rrset(
        domain=zone_name,
        subname=rec_cfg.subname,
        rtype="CNAME",
        ttl=ttl,
        records=rec_cfg.values,
    )


def run_failover(config_path: str) -> None:
    cfg = load_failover_config(config_path)

    domain = cfg["domain"]
    router_record = cfg["router_record"]
    primary_target = cfg["primary_target"]
    secondary_target = cfg["secondary_target"]

    primary_check_url = cfg["primary_check_url"]
    secondary_check_url = cfg["secondary_check_url"]
    expected_status = int(cfg["expected_status"])
    timeout_seconds = int(cfg.get("timeout_seconds", 5))

    router_fqdn = fqdn(domain, router_record)
    primary_fqdn = fqdn(domain, primary_target)
    secondary_fqdn = fqdn(domain, secondary_target)

    cf_token = get_env_or_raise("CF_API_TOKEN")
    desec_token = get_env_or_raise("DESEC_API_TOKEN")

    cf = CloudflareClient(cf_token)
    desec = DeSecClient(desec_token)

    zone_id = cf.get_zone_id(domain)

    print(f"[INFO] Running failover check for domain {domain}")
    print(f"[INFO] Router record: {router_fqdn}")
    print(f"[INFO] Primary target: {primary_fqdn}")
    print(f"[INFO] Secondary target: {secondary_fqdn}")

    primary_ok = check_health(primary_check_url, expected_status, timeout_seconds)
    print(f"[INFO] Primary health ({primary_check_url}) -> {primary_ok}")

    if primary_ok:
        desired_target = primary_fqdn
    else:
        secondary_ok = check_health(secondary_check_url, expected_status, timeout_seconds)
        print(f"[INFO] Secondary health ({secondary_check_url}) -> {secondary_ok}")
        if secondary_ok:
            desired_target = secondary_fqdn
        else:
            print("[WARN] Both primary and secondary appear unhealthy; no change will be made.")
            return

    current = current_target_info(cf, zone_id, router_fqdn)
    print(f"[INFO] Current router target (Cloudflare): {current}")

    if current == desired_target:
        print("[INFO] Router already points to desired target; no update needed.")
        return

    # Apply change in both providers
    set_router_target(
        cf=cf,
        desec=desec,
        zone_name=domain,
        zone_id=zone_id,
        router_name=router_record,
        router_fqdn=router_fqdn,
        new_target_fqdn=desired_target,
        ttl=60,
    )

    print("[INFO] Failover update completed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Health-based DNS failover between two CDN fronts."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to failover config YAML (e.g. config/failover.example.com.yml)",
    )

    args = parser.parse_args(argv)

    try:
        run_failover(args.config)
    except (ConfigError, ProviderError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
