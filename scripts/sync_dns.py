#!/usr/bin/env python3
"""
Synchronize a subset of DNS records between Cloudflare and deSEC.

Usage:
    python scripts/sync_dns.py --config config/zone.example.com.yml
"""

from __future__ import annotations

import argparse
import sys

from common import (
    CloudflareClient,
    DeSecClient,
    ConfigError,
    ProviderError,
    load_zone_config,
    get_env_or_raise,
)


def sync_zone(config_path: str) -> None:
    domain, records = load_zone_config(config_path)

    cf_token = get_env_or_raise("CF_API_TOKEN")
    desec_token = get_env_or_raise("DESEC_API_TOKEN")

    cf = CloudflareClient(cf_token)
    desec = DeSecClient(desec_token)

    print(f"[INFO] Loading zone config for domain {domain} from {config_path}")

    zone_id = cf.get_zone_id(domain)
    print(f"[INFO] Cloudflare zone ID for {domain}: {zone_id}")

    for rec in records:
        fqdn = rec.fqdn
        value = rec.values[0]

        print(
            f"[INFO] Upserting {rec.type} {fqdn} -> {value} (TTL {rec.ttl}) "
            "in Cloudflare and deSEC"
        )

        # Cloudflare: single-value record
        cf.upsert_dns_record(
            zone_id=zone_id,
            name=fqdn,
            rtype=rec.type,
            content=value,
            ttl=rec.ttl,
        )

        # deSEC: RRset
        desec.upsert_rrset(
            domain=rec.domain,
            subname=rec.subname,
            rtype=rec.type,
            ttl=rec.ttl,
            records=rec.values,
        )

    print("[INFO] Sync completed successfully.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize DNS records between Cloudflare and deSEC."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to zone config YAML (e.g. config/zone.example.com.yml)",
    )

    args = parser.parse_args(argv)

    try:
        sync_zone(args.config)
    except (ConfigError, ProviderError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
