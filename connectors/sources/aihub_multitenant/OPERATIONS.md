# Liferay AI Hub Connector — Hosting & Operationalization Guide

This guide covers everything needed to take the `aihub_multitenant` connector from a local test into a production-grade deployment serving multiple Liferay enterprise customers.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Liferay Setup](#3-liferay-setup)
4. [Elastic Cloud Setup](#4-elastic-cloud-setup)
5. [Installing the Connector Service](#5-installing-the-connector-service)
6. [Configuration](#6-configuration)
7. [Running the Connector](#7-running-the-connector)
8. [Production Hosting Options](#8-production-hosting-options)
9. [Multi-Tenant Operations](#9-multi-tenant-operations)
10. [Monitoring & Alerting](#10-monitoring--alerting)
11. [Sync Scheduling](#11-sync-scheduling)
12. [Troubleshooting](#12-troubleshooting)
13. [Security Checklist](#13-security-checklist)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Liferay AI Hub                               │
│                                                                     │
│  Tenant A (cust0042)          Tenant B (cust0099)                  │
│  ┌────────────────────┐       ┌────────────────────┐               │
│  │  Liferay DXP       │       │  Liferay DXP        │              │
│  │  - Web Content     │       │  - Web Content      │              │
│  │  - Documents       │       │  - Blog Posts       │              │
│  │  - Blog Posts      │       │  - External DB      │              │
│  └────────┬───────────┘       └──────────┬──────────┘              │
│           │  OAuth2 / HTTPS              │                          │
└───────────┼──────────────────────────────┼──────────────────────────┘
            │                              │
            ▼                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│               Connector Service Host (VM / Docker / K8s)            │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              elastic-ingest process                          │   │
│  │                                                              │   │
│  │   aihub_multitenant connector (one instance per tenant DS)  │   │
│  │   - Stamps every doc: tenant_id + datasource_id             │   │
│  │   - Handles: FILE | URL | DB | LIFERAY source types         │   │
│  └──────────────────┬───────────────────────────────────────────┘   │
│                     │  Elasticsearch Bulk API (HTTPS)               │
└─────────────────────┼───────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Elastic Cloud 9.2.x                              │
│                                                                     │
│   aihub-cust0042-liferay-main   ← Tenant A Web Content             │
│   aihub-cust0042-docs-prod      ← Tenant A Documents               │
│   aihub-cust0099-liferay-main   ← Tenant B Web Content             │
│                                                                     │
│   Vertex AI inference endpoint → semantic_text embeddings           │
└─────────────────────────────────────────────────────────────────────┘
```

**Key principle:** One connector instance in Kibana = one (tenant, data source) pair. Each instance writes to its own dedicated index. The connector service runs on your own infrastructure — Elastic Cloud does not host it.

---

## 2. Prerequisites

### Elastic Cloud
- Elastic Cloud deployment running **9.2.x** (tested on 9.2.2)
- Deployment Admin API key
- Kibana access

### Connector Host Machine
| Resource | Minimum (testing) | Recommended (production) |
|---|---|---|
| CPU | 1 vCPU | 2+ vCPUs |
| RAM | 1 GB | 4 GB |
| Disk | 10 GB | 50 GB |
| OS | macOS / Linux | Ubuntu 22.04 LTS / RHEL 9 |
| Python | 3.10 or 3.11 | 3.11 |
| Network | Outbound HTTPS to Elastic Cloud + Liferay | Same |

### Liferay DXP
- Liferay DXP 7.4+ (for Headless Delivery API v1.0)
- Admin access to create OAuth2 applications
- Headless Delivery API enabled (enabled by default in 7.4+)

---

## 3. Liferay Setup

### 3.1 Create an OAuth2 Application

This step must be done **once per Liferay instance** (not per tenant/site).

1. Log into Liferay as a Portal Administrator
2. Go to **Control Panel → Security → OAuth 2 Administration**
3. Click **Add** (+ button)
4. Fill in:
   - **Application Name:** `Elastic AI Hub Connector`
   - **Client Profile:** `Headless Server`
   - **Allowed Authorization Types:** `Client Credentials`
5. Click **Save**
6. On the next screen, note the **Client ID** and **Client Secret**
7. Click **Scopes** tab → Add:
   - `Liferay.Headless.Delivery.everything`
   - `Liferay.Headless.Admin.Content.everything` *(optional, for admin-level access)*
8. **Save**

### 3.2 Get an Access Token

```bash
curl -X POST https://YOUR-LIFERAY-HOST/o/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET"
```

Response:
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5c...",
  "token_type": "Bearer",
  "expires_in": 600
}
```

Copy the `access_token` — this is your **Liferay OAuth2 Bearer Token**.

> **Token expiry:** Liferay tokens expire (default 10 minutes). For production, implement a token refresh mechanism or increase the token lifetime in **Control Panel → Security → OAuth 2 Administration → Edit Application → Token Lifespan**.
> Setting it to `86400` (24 hours) or `2592000` (30 days) is practical for a connector that runs on a schedule.

### 3.3 Find Your Site ID

1. Go to **Control Panel → Sites**
2. Click on the site you want to sync
3. Go to **Site Menu → Configuration → Site Settings → Site Configuration**
4. The **Site ID** (also called Group ID) is visible in the URL: `...groupId=20122`

Alternatively, call the API:
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "https://YOUR-LIFERAY-HOST/o/headless-delivery/v1.0/sites/mine"
```

### 3.4 Find Your Documents & Media Folder ID (for FILE source type)

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "https://YOUR-LIFERAY-HOST/o/headless-delivery/v1.0/sites/YOUR_SITE_ID/document-folders"
```

Each folder in the response has an `id` — use that as `folder_id`.

---

## 4. Elastic Cloud Setup

### 4.1 Create an API Key

In Kibana → **Stack Management → API Keys → Create API key**

- Name: `aihub-connector-key`
- Privileges: `Superuser` (for initial setup) or custom role with:
  - `indices: [".elastic-connectors*"]` — read/write
  - `indices: ["aihub-*"]` — read/write/create index

### 4.2 Create the Target Index (per tenant data source)

Run this once per connector instance before the first sync:

```bash
PUT aihub-{tenant_id}-{datasource_id}
{
  "mappings": {
    "properties": {
      "tenant_id":     { "type": "keyword" },
      "datasource_id": { "type": "keyword" },
      "title":         { "type": "text" },
      "body": {
        "type": "text",
        "copy_to": "body_embedding"
      },
      "body_embedding": {
        "type": "semantic_text",
        "inference_id": "vertex-ai-embeddings"
      },
      "timestamp":     { "type": "date" },
      "url":           { "type": "keyword" },
      "content_type":  { "type": "keyword" },
      "creator":       { "type": "keyword" }
    }
  }
}
```

> Replace `vertex-ai-embeddings` with your Elastic inference endpoint ID. Set this up under **Kibana → Search → Inference Endpoints** pointing to your Vertex AI model.

### 4.3 Create a Connector in Kibana

For **each** (tenant, data source) pair:

1. Kibana → **Search → Connectors → Create connector**
2. Select **Custom connector**
3. Choose **Run from source** (self-managed)
4. Note the generated **Connector ID** and **Connector API Key** — you will need both for `config.yml`

---

## 5. Installing the Connector Service

### Clone the repository

```bash
git clone https://github.com/shankargithubrep/elastic-crawler-connector-liferay.git
cd elastic-crawler-connector-liferay
git checkout feat/aihub-v9.2.7
```

### Install Python dependencies

```bash
# Install the SDK first (local package, not on PyPI)
pip3 install -e libs/connectors_sdk

# Install the connector service
pip3 install -e .
```

Or use the Makefile (creates an isolated venv):

```bash
make install
```

### Verify the connector loads correctly

```bash
python3 -c "
from connectors.sources.aihub_multitenant import AiHubMultiTenantDataSource
print('Connector loaded:', AiHubMultiTenantDataSource.service_type)
"
# Expected: Connector loaded: aihub_multitenant
```

---

## 6. Configuration

### 6.1 Create `config.yml`

Create `config.yml` in the repo root (this file is gitignored — never commit it):

```yaml
elasticsearch:
  host: https://YOUR-DEPLOYMENT-ID.es.YOUR-REGION.gcp.cloud.es.io:443
  api_key: YOUR_ELASTIC_API_KEY
  ssl: true
  verify_certs: true

service:
  log_level: INFO
  idling: 30           # seconds between polling for new sync jobs

sources:
  aihub_multitenant: connectors.sources.aihub_multitenant:AiHubMultiTenantDataSource

# One entry per connector instance created in Kibana
connectors:
  - connector_id: "CONNECTOR_ID_FROM_KIBANA"
    service_type: aihub_multitenant
    api_key: "CONNECTOR_API_KEY_FROM_KIBANA"

  # Add more connectors here for additional tenants/data sources:
  # - connector_id: "ANOTHER_CONNECTOR_ID"
  #   service_type: aihub_multitenant
  #   api_key: "ANOTHER_CONNECTOR_API_KEY"
```

### 6.2 Configure Each Connector in Kibana

For each connector instance, fill in the **Configuration** tab in Kibana:

**Common fields (all source types):**

| Field | Value | Notes |
|---|---|---|
| Tenant ID | `cust0042` | Your customer identifier — used in index name |
| Data Source ID | `liferay-main` | Unique per data source — used in index name |
| Source Type | (dropdown) | Select one of: FILE, URL, DB, LIFERAY |

**LIFERAY source type:**

| Field | Value |
|---|---|
| Liferay Base URL | `https://liferay.yourcompany.com` |
| Liferay OAuth2 Bearer Token | Token from Step 3.2 |
| Liferay Site ID | Site ID from Step 3.3 |

**FILE source type:**

| Field | Value |
|---|---|
| Liferay Base URL | `https://liferay.yourcompany.com` |
| Liferay OAuth2 Bearer Token | Token from Step 3.2 |
| Documents & Media Folder ID | Folder ID from Step 3.4 |

**URL source type:**

| Field | Example |
|---|---|
| Seed URLs | `https://docs.yourcompany.com, https://support.yourcompany.com` |
| Maximum Crawl Depth | `2` |
| Allowed Domains | `yourcompany.com` |

**DB source type:**

| Field | Example |
|---|---|
| Connection String | `postgresql+psycopg2://user:pass@host:5432/dbname` |
| SQL Query | `SELECT id, title, body, updated_at FROM knowledge_articles WHERE published = true` |

---

## 7. Running the Connector

### One-time / manual run

```bash
elastic-ingest -c config.yml
```

The service will:
1. Connect to Elastic Cloud (preflight check)
2. Register all connectors listed in `config.yml`
3. Poll every 30 seconds for pending sync jobs
4. Execute syncs when triggered from Kibana or on schedule
5. Log progress to stdout

### Trigger a sync from Kibana

Kibana → Search → Connectors → select your connector → **Sync → Full sync**

### Trigger a sync via API

```bash
curl -X POST \
  -H "Authorization: ApiKey YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  "https://YOUR-ES-HOST/_connector/CONNECTOR_ID/_sync_job" \
  -d '{"job_type": "full"}'
```

---

## 8. Production Hosting Options

### Option A — Systemd service (Linux VM, recommended for simplicity)

Create `/etc/systemd/system/aihub-connector.service`:

```ini
[Unit]
Description=Liferay AI Hub Connector Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=connector
WorkingDirectory=/opt/elastic-connector
ExecStart=/usr/local/bin/elastic-ingest -c /opt/elastic-connector/config.yml
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable aihub-connector
sudo systemctl start aihub-connector
sudo journalctl -u aihub-connector -f   # follow logs
```

### Option B — Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install connector
COPY . .
RUN pip install --no-cache-dir -e libs/connectors_sdk && \
    pip install --no-cache-dir -e .

# config.yml is mounted at runtime (never bake credentials into image)
CMD ["elastic-ingest", "-c", "/config/config.yml"]
```

```bash
docker build -t aihub-connector:latest .

docker run -d \
  --name aihub-connector \
  --restart unless-stopped \
  -v /path/to/your/config.yml:/config/config.yml:ro \
  aihub-connector:latest
```

### Option C — Kubernetes (for scale)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aihub-connector
  namespace: elastic
spec:
  replicas: 1         # Keep at 1 — the service handles concurrency internally
  selector:
    matchLabels:
      app: aihub-connector
  template:
    metadata:
      labels:
        app: aihub-connector
    spec:
      containers:
        - name: connector
          image: your-registry/aihub-connector:latest
          resources:
            requests:
              memory: "512Mi"
              cpu: "250m"
            limits:
              memory: "2Gi"
              cpu: "1000m"
          volumeMounts:
            - name: config
              mountPath: /config
              readOnly: true
      volumes:
        - name: config
          secret:
            secretName: aihub-connector-config
---
# Store config.yml as a Kubernetes Secret
# kubectl create secret generic aihub-connector-config \
#   --from-file=config.yml=./config.yml
```

---

## 9. Multi-Tenant Operations

### Adding a new tenant or data source

For each new (tenant, data source) pair:

1. **Create a connector in Kibana** → note the Connector ID and API Key
2. **Create the target index** in Elasticsearch (see Section 4.2)
3. **Add an entry** to `config.yml`:
   ```yaml
   connectors:
     - connector_id: "NEW_CONNECTOR_ID"
       service_type: aihub_multitenant
       api_key: "NEW_CONNECTOR_API_KEY"
   ```
4. **Configure the connector** in Kibana with the tenant's Liferay details
5. **Restart** the connector service (or send SIGHUP for a live reload)

> One connector service process can manage **multiple connectors** simultaneously — all entries in `config.yml` are handled by the same process.

### Index naming convention

| Tenant ID | Data Source ID | Index Name |
|---|---|---|
| `cust0042` | `liferay-main` | `aihub-cust0042-liferay-main` |
| `cust0042` | `docs-prod` | `aihub-cust0042-docs-prod` |
| `cust0099` | `liferay-main` | `aihub-cust0099-liferay-main` |

### Querying with tenant isolation

Every RAG query from the AI Hub API gateway **must** include a `tenant_id` filter:

```json
POST /aihub-cust0042-*/_search
{
  "query": {
    "bool": {
      "filter": [
        { "term": { "tenant_id": "cust0042" } }
      ],
      "must": [
        {
          "semantic": {
            "field": "body_embedding",
            "query": "how do I reset my password?"
          }
        }
      ]
    }
  }
}
```

---

## 10. Monitoring & Alerting

### Check connector sync status via API

```bash
# List all connectors and their last sync status
curl -H "Authorization: ApiKey YOUR_KEY" \
  "https://YOUR-ES-HOST/_connector?pretty"

# Get last sync job for a specific connector
curl -H "Authorization: ApiKey YOUR_KEY" \
  "https://YOUR-ES-HOST/_connector/CONNECTOR_ID/_sync_job?pretty"
```

### Key metrics to monitor

| Metric | How to check | Alert threshold |
|---|---|---|
| Last sync time | `_connector` API `last_synced` field | > 25 hours (for daily syncs) |
| Sync status | `_connector` API `last_sync_status` | `error` or `canceled` |
| Document count | `_cat/indices/aihub-*` API | Drops by >10% between syncs |
| Connector service process | systemd / Docker health | Process not running |
| Elasticsearch connectivity | Connector logs | `ERROR Could not connect` |

### Log locations

| Deployment type | Log location |
|---|---|
| Systemd | `journalctl -u aihub-connector` |
| Docker | `docker logs aihub-connector` |
| Kubernetes | `kubectl logs -n elastic deploy/aihub-connector` |
| Local / manual | stdout (pipe to file with `elastic-ingest -c config.yml >> connector.log 2>&1`) |

---

## 11. Sync Scheduling

Syncs can be scheduled directly in Kibana:

1. Kibana → Search → Connectors → select connector
2. **Scheduling** tab → enable **Sync schedule**
3. Set a cron expression, e.g.:
   - `0 2 * * *` — daily at 2 AM
   - `0 */6 * * *` — every 6 hours
   - `0 * * * *` — hourly

The connector service must be **running** when the scheduled sync fires. It polls Elasticsearch every 30 seconds (configurable via `service.idling`) and picks up the job automatically.

### Incremental vs Full sync

| Type | When to use |
|---|---|
| **Full sync** | First run, after config changes, weekly baseline |
| **Incremental sync** | Not yet implemented in v1 — all syncs are full |

> Incremental sync (only re-index changed documents) is a planned enhancement. It would require Liferay webhook integration or polling `dateModified` timestamps.

---

## 12. Troubleshooting

### Connector not appearing in Kibana / "Waiting for connector"

**Cause:** The connector service isn't running or can't reach Elasticsearch.

**Fix:**
```bash
# Check the service is running
elastic-ingest -c config.yml

# Verify Elasticsearch connectivity
curl -H "Authorization: ApiKey YOUR_KEY" "https://YOUR-ES-HOST/"
# Should return 200 with cluster info
```

### Version incompatibility error

```
CRITICAL Elasticsearch 9.2.2 and Connectors 9.4.0 are incompatible
```

**Fix:** Check out the matching version tag:
```bash
git checkout v9.2.7   # use latest patch of your ES minor version
make install
```

### `ModuleNotFoundError: No module named 'connectors_sdk'`

**Fix:** Install the SDK from source first:
```bash
pip3 install -e libs/connectors_sdk
pip3 install -e .
```

### Liferay 401 Unauthorized

**Cause:** OAuth2 token has expired (default lifetime is 10 minutes).

**Fix:** Generate a new token (see Section 3.2) and update it in the Kibana connector configuration. Consider increasing token lifetime in Liferay OAuth2 settings.

### No documents indexed after sync

Check these in order:
1. Connector configuration saved in Kibana (Configuration tab → shows all fields filled)
2. Target index exists in Elasticsearch (`GET aihub-*`)
3. Connector logs for errors: `ERROR` or `WARNING` lines
4. Liferay API accessible: `curl -H "Authorization: Bearer TOKEN" "LIFERAY_URL/o/headless-delivery/v1.0/sites/SITE_ID/structured-contents"`
5. Verify `folder_id` or `site_id` returns actual content from the Liferay API

### SQL query errors (DB source type)

**Fix:** Test the query directly against the database before configuring the connector. Ensure the DB driver is installed:
```bash
pip3 install psycopg2-binary   # PostgreSQL
pip3 install pymysql           # MySQL
pip3 install python-tds        # MSSQL
```

---

## 13. Security Checklist

Before going to production, verify the following:

- [ ] `config.yml` is **not committed to git** (it's in `.gitignore`)
- [ ] OAuth2 token lifetime is set appropriately in Liferay (or a refresh mechanism is in place)
- [ ] `connection_string` for DB sources does not use a superuser account — use a read-only DB user
- [ ] `allowed_domains` is set for URL crawlers to prevent unintended crawling of internal networks
- [ ] The Elastic API key used by the connector service has **minimum required privileges** (not superuser in production)
- [ ] The connector host machine has **outbound-only** network access to Elastic Cloud (no inbound ports required)
- [ ] All RAG queries from the AI Hub gateway enforce the `tenant_id` filter — this is the **primary data isolation mechanism**
- [ ] Index names follow the `aihub-{tenant_id}-{datasource_id}` pattern and are not shared across tenants
- [ ] Elasticsearch index aliases or index-level API key restrictions are in place to prevent cross-tenant index access at the API level

---

## Summary — Getting to Production in 5 Steps

| Step | Who | Action |
|---|---|---|
| 1 | Liferay Admin | Create OAuth2 app, generate access token |
| 2 | Elastic Admin | Create Elastic Cloud index + inference endpoint + Kibana connector |
| 3 | DevOps | Deploy connector service (systemd / Docker / K8s) with `config.yml` |
| 4 | Elastic Admin | Configure connector in Kibana UI, trigger first full sync |
| 5 | AI Hub Dev | Build RAG query API that enforces `tenant_id` filter on every request |

---

*For questions or issues, refer to the connector source code in `connectors/sources/aihub_multitenant/` or open an issue in the repository.*
