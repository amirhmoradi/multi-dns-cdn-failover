# multi-dns-failover

Multi-provider DNS + CDN failover with Cloudflare, deSEC, and a secondary CDN (e.g. Bunny).

This project lets you:

- Keep **Cloudflare DNS** and **deSEC DNS** in sync for a subset of records (non-destructive).
- Use a **router CNAME** (`web-router.example.com`) that can be switched between:
  - Cloudflare front (`cf-front.example.com`)
  - CDN2 front (`cdn2-front.example.com`)
- Automatically flip that router CNAME based on **health checks** against your primary CDN.
- Run the control scripts from **your own server**, with code mirrored from GitHub (for resilience).

The repo is designed to be **public**, so others can copy the pattern and adapt it.

> This project manages **only the records you declare** in the YAML config. It does **not** delete or modify other records in your zones.

---

## 1. Architecture overview

### 1.1 DNS and CDN layout

We use `example.com` as a reference domain (replace with your own).

At your registrar, you configure **both** Cloudflare and deSEC nameservers for `example.com`:

- `ns1.cloudflare.com`, `ns2.cloudflare.com`, …
- `ns1.desec.io`, `ns2.desec.io`, …

Both providers serve an **equivalent copy** of your zone for the records you manage with this project.

Key records (simplified):

- `www.example.com` → CNAME → `web-router.example.com`
- `web-router.example.com` → CNAME → either:
  - `cf-front.example.com` (primary = Cloudflare) or
  - `cdn2-front.example.com` (secondary = CDN2/Bunny)
- `cf-front.example.com` → CNAME → your Cloudflare front (e.g. proxied `example.com`)
- `cdn2-front.example.com` → CNAME → your CDN2 hostname (e.g. `example.b-cdn.net`)
- `origin.example.com` → A/AAAA → your origin IP / Traefik / Swarm / etc.

### 1.2 Behaviour

- **Normal**:
  - `www` → `web-router` → `cf-front` → Cloudflare → origin
- **Cloudflare proxy outage**:
  - Health check fails for Cloudflare
  - `web-router` is automatically updated in **both** Cloudflare and deSEC zones:
    - `web-router` → `cdn2-front` → CDN2 → origin
- **Cloudflare DNS outage**:
  - Recursive resolvers fall back to deSEC NS
  - deSEC still serves your records → traffic continues via current router target
- **Total Cloudflare meltdown**:
  - deSEC still answers
  - `web-router` can stay on `cdn2-front` or be manually pointed to `origin`

---

## 2. Requirements

- Python 3.10+
- A machine (e.g. `infra01`) where you run the scripts on a schedule (cron or systemd timer)
- A GitHub repo (optional but recommended)
- Accounts and API tokens for:
  - **Cloudflare**
  - **deSEC**
  - **CDN2** (e.g. Bunny) – not accessed via API here, only via DNS CNAME

---

## 3. Installation

Clone this repo (or copy its contents):

```bash
git clone git@github.com:amirhmoradi/multi-dns-failover.git
cd multi-dns-failover
```

Create and activate a virtualenv (recommended):

```bash
python -m venv .venv
source .venv/bin/activate  # Linux / macOS
# .venv\Scripts\activate   # Windows PowerShell
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## 4. Configuration

### 4.1 Environment variables

Set the following environment variables on the machine where the scripts will run:

```bash
export CF_API_TOKEN="your-cloudflare-api-token"
export DESEC_API_TOKEN="your-desec-api-token"
```

Recommended token scopes:

* Cloudflare: DNS Write for the relevant zone.
* deSEC: token with RRset management permissions for your domain.

You can also put these into a `.env` file and load them using `python-dotenv` if you wish; the scripts currently read from the environment directly for simplicity.

### 4.2 Zone config (`config/zone.example.com.yml`)

This file describes the **subset** of records to manage in both providers.

Copy it and adapt it:

```bash
cp config/zone.example.com.yml config/zone.yourdomain.yml
```

Edit:

```yaml
domain: example.com

records:
  - name: "www"
    type: "CNAME"
    ttl: 60
    values:
      - "web-router.example.com."

  - name: "web-router"
    type: "CNAME"
    ttl: 60
    values:
      - "cf-front.example.com."

  - name: "cf-front"
    type: "CNAME"
    ttl: 300
    values:
      - "example.com."        # Your Cloudflare front, often apex or a proxied host

  - name: "cdn2-front"
    type: "CNAME"
    ttl: 300
    values:
      - "example.b-cdn.net."  # Your CDN2 hostname (e.g. Bunny)

  - name: "origin"
    type: "A"
    ttl: 300
    values:
      - "203.0.113.10"        # Your origin IP (Traefik, Swarm, etc.)
```

Notes:

* `domain` is the zone/apex (e.g. `example.com`).
* `name` can be:

  * Just the left-hand label (`www`, `web-router`), or
  * The full FQDN (`www.example.com.`). Both are accepted.
* `values` is a list, but the current implementation supports **only one value per record** for Cloudflare (multi-value RRsets are supported on deSEC but not fully modelled on Cloudflare in this script). For CNAMEs, that's what you want anyway.

### 4.3 Failover config (`config/failover.example.com.yml`)

This file tells the failover script:

* Which CNAME to switch
* Which targets to use
* Where to perform health checks

Copy and adapt:

```bash
cp config/failover.example.com.yml config/failover.yourdomain.yml
```

Edit:

```yaml
domain: example.com

router_record: "web-router"       # without domain, script will add .example.com
primary_target: "cf-front"        # without domain
secondary_target: "cdn2-front"    # without domain

primary_check_url: "https://www.example.com/health"
secondary_check_url: "https://www.example.com/health"  # through CDN2, CNAME resolves

expected_status: 200

timeout_seconds: 5
```

You should expose a very cheap `/health` endpoint on your app that returns `200 OK` when everything is fine.

---

## 5. Usage

### 5.1 One-time DNS sync

After configuring your zone YAML and environment variables:

```bash
python scripts/sync_dns.py --config config/zone.example.com.yml
```

What this does:

* For each record in the config:

  * Upserts the record in **Cloudflare** (create/update, no delete of others).
  * Upserts the corresponding RRset in **deSEC**.

You can run this:

* Manually whenever you change the zone config.
* Or from a CI pipeline (GitHub Actions) after merging changes to the config.

### 5.2 Periodic health-based failover

Run the failover script with your failover config:

```bash
python scripts/failover.py --config config/failover.example.com.yml
```

Behaviour:

* Performs an HTTP GET on `primary_check_url`:

  * If it returns `expected_status`, ensures `router_record` → `primary_target`.
* If primary **fails**:

  * Tries `secondary_check_url`:

    * If OK → ensures `router_record` → `secondary_target`.
    * If not OK → does nothing (leaves current state unchanged, logs error).

This script is **idempotent**: if `web-router` already points to the desired target, no change is made.

### 5.3 Automating failover via cron or systemd

Example cron entry (run every 2 minutes):

```bash
*/2 * * * * cd /opt/multi-dns-failover && \
  /opt/multi-dns-failover/.venv/bin/python scripts/failover.py \
  --config config/failover.example.com.yml >> /var/log/multi-dns-failover.log 2>&1
```

For production, a systemd service + timer gives you better control and logging.

---

## 6. Safety and limitations

* The scripts **never delete** any DNS records.
* They only upsert the records you define in the YAML config.
* Cloudflare support:

  * One value per record (sufficient for CNAMEs and simple A records).
* deSEC:

  * Full RRset semantics, but we keep them aligned with the Cloudflare model.

You are responsible for:

* Correctly configuring Cloudflare and deSEC as nameservers at your registrar.
* Correctly setting up CDN2 (e.g. Bunny) to proxy your `origin.example.com`.

---

## 7. Extending the project

Ideas:

* Add support for additional DNS providers (Route53, NS1, RcodeZero).
* Manage more record types (MX, TXT, AAAA) in the same pattern.
* Add status notifications (Slack, email, etc.) on failover events.
* Add CLI packaging (`pip install .` with entry points).

---

## 8. GitHub Actions: DNS sync pipeline

This repo includes a ready-to-use GitHub Actions workflow:

* `.github/workflows/sync-dns.yml`

What it does:

* Triggers on:

  * pushes to `main` that touch:

    * `config/zone*.yml`
    * `scripts/sync_dns.py`
    * `scripts/common.py`
    * the workflow file itself
  * manual `workflow_dispatch`
* Sets up Python
* Installs dependencies
* Runs:

  ```bash
  python scripts/sync_dns.py --config config/zone.example.com.yml
  ```

### 8.1 Setup

1. Go to your repo on GitHub → **Settings → Secrets and variables → Actions → New repository secret**.
2. Add:

   * `CF_API_TOKEN` – Cloudflare API token with DNS write permissions for your zone.
   * `DESEC_API_TOKEN` – deSEC API token with RRset write permissions for your domain(s).
3. Push the workflow file (if you haven't already).

To test it manually:

* Go to **Actions → Sync DNS (Cloudflare + deSEC)** → **Run workflow**.

You should see logs like:

* Cloudflare zone lookup
* "Upserting A/CNAME in Cloudflare and deSEC"
* "Sync completed successfully"

---

## 9. systemd integration (recommended for failover)

For **health-based failover**, you likely want the script to run from your infra box (where it can see your real external surface reliably).

### 9.1 Install the project

On your infra host (e.g. `infra01`):

```bash
sudo mkdir -p /opt/multi-dns-failover
sudo chown "$USER":"$USER" /opt/multi-dns-failover

cd /opt/multi-dns-failover
git clone https://github.com/amirhmoradi/multi-dns-failover.git .
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Adjust `config/zone.example.com.yml` and `config/failover.example.com.yml` to your domains if you haven't done it already.

### 9.2 Install systemd unit and timer

Copy the unit and timer into systemd's directory:

```bash
sudo cp systemd/multi-dns-failover.service /etc/systemd/system/
sudo cp systemd/multi-dns-failover.timer /etc/systemd/system/
```

Edit `/etc/systemd/system/multi-dns-failover.service`:

* Replace `YOUR_CLOUDFLARE_API_TOKEN` and `YOUR_DESEC_API_TOKEN` with real tokens
  or remove the `Environment=` lines and manage them via an `EnvironmentFile` or `/etc/environment`.

Check syntax:

```bash
sudo systemctl daemon-reload
```

Enable and start the timer:

```bash
sudo systemctl enable --now multi-dns-failover.timer
```

Check status:

```bash
systemctl status multi-dns-failover.timer
systemctl status multi-dns-failover.service
journalctl -u multi-dns-failover.service -n 50
```

You should see logs for:

* Health checks on primary/secondary
* Current router target
* Any DNS updates applied

---

## 10. License

This project is released under the MIT License. See [`LICENSE`](LICENSE).
