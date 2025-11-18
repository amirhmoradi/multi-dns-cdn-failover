#!/usr/bin/env python3
"""
Common utilities and API clients for Cloudflare and deSEC.

This module is intentionally minimal and dependency-light.
"""

from __future__ import annotations

import dataclasses
import os
from typing import List, Dict, Any, Optional, Tuple

import httpx
import yaml


class ConfigError(Exception):
    pass


class ProviderError(Exception):
    pass


@dataclasses.dataclass
class DnsRecordConfig:
    domain: str
    name: str          # relative ("www") or absolute ("www.example.com.")
    type: str          # "A", "CNAME", etc.
    ttl: int
    values: List[str]

    @property
    def fqdn(self) -> str:
        if self.name.endswith("."):
            return self.name.rstrip(".")
        if self.name == self.domain:
            return self.domain
        if self.name.endswith("." + self.domain):
            return self.name
        return f"{self.name}.{self.domain}"

    @property
    def subname(self) -> str:
        """
        deSEC "subname": left-hand label relative to the zone apex.
        Special case: apex/root uses "@"
        """
        if self.fqdn == self.domain:
            return "@"
        suffix = "." + self.domain
        if self.fqdn.endswith(suffix):
            return self.fqdn[: -len(suffix)]
        raise ConfigError(
            f"Record {self.name} does not belong to domain {self.domain}"
        )


def load_zone_config(path: str) -> Tuple[str, List[DnsRecordConfig]]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ConfigError("Zone config must be a YAML mapping")

    domain = data.get("domain")
    if not domain:
        raise ConfigError("Zone config must have a 'domain' key")

    records_data = data.get("records") or []
    if not isinstance(records_data, list):
        raise ConfigError("'records' must be a list")

    records: List[DnsRecordConfig] = []
    for item in records_data:
        if not isinstance(item, dict):
            raise ConfigError("Each record must be a mapping")
        name = item.get("name")
        rtype = str(item.get("type", "")).upper()
        ttl = int(item.get("ttl", 300))
        values = item.get("values") or []
        if not name or not rtype or not values:
            raise ConfigError(f"Invalid record entry: {item}")
        if len(values) != 1:
            # For simplicity, we support one value per record for Cloudflare.
            raise ConfigError(
                f"Record {name} {rtype} must have exactly one value for now."
            )
        records.append(
            DnsRecordConfig(
                domain=domain,
                name=name,
                type=rtype,
                ttl=ttl,
                values=[str(values[0])],
            )
        )

    return domain, records


def load_failover_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ConfigError("Failover config must be a YAML mapping")

    required_keys = [
        "domain",
        "router_record",
        "primary_target",
        "secondary_target",
        "primary_check_url",
        "secondary_check_url",
        "expected_status",
    ]
    for key in required_keys:
        if key not in data:
            raise ConfigError(f"Missing required key in failover config: {key}")

    data.setdefault("timeout_seconds", 5)
    return data


class CloudflareClient:
    def __init__(self, api_token: str, base_url: str = "https://api.cloudflare.com/client/v4"):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    def get_zone_id(self, zone_name: str) -> str:
        resp = self._http.get("/zones", params={"name": zone_name})
        if resp.status_code != 200:
            raise ProviderError(f"Cloudflare get_zone_id failed: {resp.text}")
        result = resp.json()
        zones = result.get("result") or []
        if not zones:
            raise ProviderError(f"No Cloudflare zone found for {zone_name}")
        return zones[0]["id"]

    def get_dns_record(
        self, zone_id: str, name: str, rtype: str
    ) -> Optional[Dict[str, Any]]:
        resp = self._http.get(
            f"/zones/{zone_id}/dns_records",
            params={"type": rtype, "name": name},
        )
        if resp.status_code != 200:
            raise ProviderError(f"Cloudflare get_dns_record failed: {resp.text}")
        result = resp.json()
        records = result.get("result") or []
        if not records:
            return None
        return records[0]

    def upsert_dns_record(
        self,
        zone_id: str,
        name: str,
        rtype: str,
        content: str,
        ttl: int,
        proxied: Optional[bool] = None,
    ) -> None:
        existing = self.get_dns_record(zone_id, name, rtype)
        payload: Dict[str, Any] = {
            "type": rtype,
            "name": name,
            "content": content,
            "ttl": ttl,
        }
        if proxied is not None and rtype in ("A", "AAAA", "CNAME"):
            payload["proxied"] = proxied

        if existing:
            record_id = existing["id"]
            resp = self._http.put(
                f"/zones/{zone_id}/dns_records/{record_id}",
                json=payload,
            )
            if resp.status_code not in (200, 201):
                raise ProviderError(f"Cloudflare update failed: {resp.text}")
        else:
            resp = self._http.post(
                f"/zones/{zone_id}/dns_records",
                json=payload,
            )
            if resp.status_code not in (200, 201):
                raise ProviderError(f"Cloudflare create failed: {resp.text}")


class DeSecClient:
    def __init__(self, api_token: str, base_url: str = "https://desec.io/api/v1"):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Token {self.api_token}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )

    def upsert_rrset(
        self,
        domain: str,
        subname: str,
        rtype: str,
        ttl: int,
        records: List[str],
    ) -> None:
        # Use PUT on specific RRset endpoint
        # Note: '@' is used for zone apex, see deSEC docs.
        url = f"/domains/{domain}/rrsets/{subname}/{rtype}/"
        payload = {
            "subname": subname,
            "type": rtype,
            "ttl": ttl,
            "records": records,
        }
        resp = self._http.put(url, json=payload)
        if resp.status_code not in (200, 201):
            raise ProviderError(
                f"deSEC upsert_rrset failed for {domain} {subname} {rtype}: "
                f"{resp.status_code} {resp.text}"
            )


def get_env_or_raise(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise ConfigError(f"Missing required environment variable: {key}")
    return val
