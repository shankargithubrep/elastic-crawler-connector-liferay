# Liferay AI Hub — Multi-Tenant Connector (`aihub_multitenant`)

A single Elastic connector that powers the **Liferay AI Hub RAG platform** by syncing data from four different source types into a shared Elastic Cloud cluster — with hard per-tenant data isolation enforced at the document level.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Source Types](#source-types)
- [Configuration Reference](#configuration-reference)
- [Data Isolation Model](#data-isolation-model)
- [Index & Mapping Setup](#index--mapping-setup)
- [Getting Started](#getting-started)
- [File Structure](#file-structure)
- [Dependencies](#dependencies)
- [Security Considerations](#security-considerations)

---

## Overview

Liferay's enterprise customers (tenants) each have their own data sources they want to connect to a RAG (Retrieval-Augmented Generation) pipeline. Instead of deploying one connector per source per tenant, this connector handles all four source types in a single deployable unit.

| Concept | Value |
|---|---|
| `service_type` | `aihub_multitenant` |
| Connector class | `AiHubMultiTenantDataSource` |
| Index pattern | `aihub-{tenant_id}-{datasource_id}` |
| Embeddings | Vertex AI via `semantic_text` field (no Elastic ML node required) |
| Multi-tenancy | Field-level isolation — every doc contains `tenant_id` + `datasource_id` |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  Liferay AI Hub Platform                    │
│                                                             │
│  Customer A (cust0042)      Customer B (cust0099)           │
│  ┌──────────────────┐       ┌──────────────────┐           │
│  │  FILE datasource  │       │  URL datasource   │           │
│  │  DB datasource    │       │  LIFERAY datasource│          │
│  └────────┬─────────┘       └────────┬─────────┘           │
│           │                          │                       │
│           ▼                          ▼                       │
│  ┌─────────────────────────────────────────────┐            │
│  │        aihub_multitenant Connector          │            │
│  │                                             │            │
│  │  _stamp(doc):                               │            │
│  │    doc["tenant_id"]    = "cust0042"         │            │
│  │    doc["datasource_id"] = "ds-001"          │            │
│  └─────────────────┬───────────────────────────┘            │
│                    │                                         │
└────────────────────┼─────────────────────────────────────────┘
                     ▼
         ┌───────────────────────┐
         │  Elastic Cloud Cluster │
         │                       │
         │  aihub-cust0042-ds001  │  ← tenant A, source 1
         │  aihub-cust0042-ds002  │  ← tenant A, source 2
         │  aihub-cust0099-ds001  │  ← tenant B, source 1
         └───────────────────────┘
```

Each connector instance in Kibana corresponds to **one (tenant, datasource)** pair and syncs to its own dedicated index.

---

## Source Types

### `FILE` — Liferay Documents & Media

Syncs files from a Liferay Documents & Media folder via the Headless Delivery REST API.

- **API endpoint:** `GET /o/headless-delivery/v1.0/document-folders/{folder_id}/documents`
- **Text extraction:** Inline for `.txt`, `.md`, `.html`, `.json`, `.xml`, `.csv`. Binary files (PDF, DOCX, etc.) are skipped at fetch time — use Elastic's ingest pipeline attachment processor for those.
- **Pagination:** Automatic, page-by-page via Liferay's standard `totalCount` envelope.

**Fields indexed per document:**

| Field | Description |
|---|---|
| `id` | `file-{liferay_document_id}` |
| `title` | Document title |
| `body` | Extracted text content |
| `timestamp` | `dateModified` (ISO 8601) |
| `content_url` | Direct download URL |
| `file_extension` | e.g. `pdf`, `docx` |
| `size_in_bytes` | File size |
| `creator` | Author name |
| `folder_id` | Source folder |
| `tenant_id` | ✅ Isolation field |
| `datasource_id` | ✅ Isolation field |

---

### `URL` — Web / URL Crawler

BFS (breadth-first) crawler that indexes any HTTP/HTTPS site, including JS-rendered SPAs at the HTML level.

- **Concurrency:** Up to 5 parallel requests (configurable via `MAX_CONCURRENT_REQUESTS`)
- **Domain filtering:** Restricts crawling to `allowed_domains` to prevent runaway crawls
- **Body cap:** 1 MB per page to prevent memory spikes
- **HTML parsing:** stdlib `html.parser` — no external parser dependency

**Fields indexed per document:**

| Field | Description |
|---|---|
| `id` | `url-{md5(url)}` |
| `url` | Full URL of the page |
| `title` | Extracted `<title>` tag |
| `body` | Visible text (scripts/styles stripped) |
| `depth` | Link-hop distance from seed URL |
| `http_status` | HTTP response code |
| `tenant_id` | ✅ Isolation field |
| `datasource_id` | ✅ Isolation field |

---

### `DB` — External Database

Executes a user-supplied SQL `SELECT` query against any database supported by SQLAlchemy, and indexes each result row as a document.

- **Supported databases:** PostgreSQL, MySQL, MSSQL, Oracle, SQLite, and any other SQLAlchemy-compatible DB
- **Streaming:** Rows are fetched in batches of 500 (`FETCH_SIZE`) to avoid loading the full result set into memory
- **Thread safety:** Sync SQLAlchemy engine runs inside `asyncio.to_thread()` — no async driver required

**Fields indexed per document:**

| Field | Description |
|---|---|
| `id` | `db-{md5(row_content + index)}` |
| *(all columns)* | Every column from the SQL result set |
| `tenant_id` | ✅ Isolation field |
| `datasource_id` | ✅ Isolation field |

**Example connection strings:**

```
postgresql+psycopg2://user:password@host:5432/dbname
mysql+pymysql://user:password@host:3306/dbname
mssql+pytds://user:password@host/dbname
sqlite:///path/to/local.db
```

---

### `LIFERAY` — Liferay Web Content & Blog Posts

Syncs both Structured Web Content articles and Blog posts from a Liferay site via the Headless Delivery API.

- **Web content:** `GET /o/headless-delivery/v1.0/sites/{site_id}/structured-contents`
- **Blog posts:** `GET /o/headless-delivery/v1.0/sites/{site_id}/blog-postings`
- **Pagination:** Automatic for both endpoints

**Fields indexed per document:**

| Field | Description |
|---|---|
| `id` | `webcontent-{id}` or `blog-{id}` |
| `title` | Article/post title |
| `body` | Rendered content or field values |
| `description` | Short description (blog posts) |
| `timestamp` | `dateModified` (ISO 8601) |
| `content_type` | `web_content` or `blog_post` |
| `site_id` | Source site |
| `creator` | Author name |
| `friendly_url` | Liferay-friendly URL path |
| `tenant_id` | ✅ Isolation field |
| `datasource_id` | ✅ Isolation field |

---

## Configuration Reference

All fields are configured in the Kibana connector UI when creating a connector of type `Liferay AI Hub Multi-Tenant`.

### Always required

| Field | Type | Description |
|---|---|---|
| `tenant_id` | `str` | Liferay `companyId` for this customer (e.g. `cust0042`) |
| `datasource_id` | `str` | Unique ID for this data source within the tenant (e.g. `docs-prod`) |
| `source_type` | dropdown | One of: `FILE`, `URL`, `DB`, `LIFERAY` |

### Liferay connection (required for `FILE` and `LIFERAY`)

| Field | Type | Sensitive | Description |
|---|---|---|---|
| `liferay_base_url` | `str` | No | Base URL of the Liferay instance, e.g. `https://liferay.example.com` |
| `oauth2_token` | `str` | **Yes** | OAuth2 Bearer token for the Headless Delivery API |

### `FILE`-specific

| Field | Type | Description |
|---|---|---|
| `folder_id` | `str` | Numeric ID of the Documents & Media folder to sync |

### `URL`-specific

| Field | Type | Default | Description |
|---|---|---|---|
| `seed_urls` | `str` | — | Comma-separated list of URLs to start crawling from |
| `max_depth` | `int` | `2` | Maximum link-hop depth from seed URLs. `0` = seeds only |
| `allowed_domains` | `str` | *(all)* | Comma-separated domains to restrict crawling to |

### `DB`-specific

| Field | Type | Sensitive | Description |
|---|---|---|---|
| `connection_string` | `str` | **Yes** | SQLAlchemy connection URL |
| `sql_query` | `str` (textarea) | No | `SELECT` query — each row becomes one document |

### `LIFERAY`-specific

| Field | Type | Description |
|---|---|---|
| `site_id` | `str` | Numeric Liferay site ID |

---

## Data Isolation Model

This is a **hard security requirement** of the AI Hub platform.

Every document produced by this connector — regardless of source type — is stamped with two fields before being yielded to the Elastic indexing pipeline:

```python
doc["tenant_id"]    = self.tenant_id     # e.g. "cust0042"
doc["datasource_id"] = self.datasource_id  # e.g. "docs-prod"
```

The AI Hub API gateway **must** enforce a `term` filter on `tenant_id` for every query it forwards to Elasticsearch. This ensures that even on a shared cluster, Customer A can never retrieve Customer B's documents.

```json
{
  "query": {
    "bool": {
      "must": [
        { "term": { "tenant_id": "cust0042" } },
        { "semantic": { "field": "body_embedding", "query": "..." } }
      ]
    }
  }
}
```

**Never** route queries to the shared cluster without this filter.

---

## Index & Mapping Setup

Each `(tenant_id, datasource_id)` pair gets its own index: `aihub-{tenant_id}-{datasource_id}`.

Create the index with a `semantic_text` field for Vertex AI embeddings before the first sync:

```json
PUT aihub-cust0042-docs-prod
{
  "mappings": {
    "properties": {
      "tenant_id":     { "type": "keyword" },
      "datasource_id": { "type": "keyword" },
      "title":         { "type": "text" },
      "body":          { "type": "text" },
      "body_embedding": {
        "type": "semantic_text",
        "inference_id": "vertex-ai-embeddings"
      },
      "timestamp": { "type": "date" },
      "url":       { "type": "keyword" }
    }
  }
}
```

> The `inference_id` `vertex-ai-embeddings` must be pre-configured in your Elastic deployment pointing to your Vertex AI endpoint. No Elastic ML node is required.

---

## Getting Started

### 1. Install dependencies

From the `app/connectors_service` directory:

```bash
pip install -e ".[aihub]"
# or with all extras:
pip install -e ".[dev]"
```

Ensure the DB driver for your database type is installed separately, e.g.:

```bash
pip install psycopg2-binary    # PostgreSQL
pip install pymysql            # MySQL
pip install pytds              # MSSQL
```

### 2. Register the connector in Kibana

In Kibana → Search → Connectors → Create connector → select **Liferay AI Hub Multi-Tenant**.

### 3. Configure the connector

Fill in `tenant_id`, `datasource_id`, `source_type`, and the source-specific fields for your chosen type.

### 4. Create the target index

Create the index with the mapping shown in [Index & Mapping Setup](#index--mapping-setup) before triggering the first sync.

### 5. Trigger a sync

Either schedule it in Kibana or trigger it manually via the Connector API:

```bash
POST _connector/{connector_id}/_sync_job
```

---

## File Structure

```
aihub_multitenant/
├── __init__.py              # Package export
├── datasource.py            # AiHubMultiTenantDataSource — main connector class
├── README.md                # This file
└── clients/
    ├── __init__.py
    ├── liferay_client.py    # Liferay Headless Delivery API client
    ├── url_client.py        # Async BFS web crawler
    └── db_client.py         # SQLAlchemy database query runner
```

---

## Dependencies

| Package | Used by | Notes |
|---|---|---|
| `aiohttp` | `liferay_client`, `url_client` | Already a framework dependency |
| `sqlalchemy` | `db_client` | Already a framework dependency |
| `connectors_sdk` | `datasource` | Core framework |
| `html.parser` | `url_client` | Python stdlib — no extra install |

Database-specific drivers (e.g. `psycopg2`, `pymysql`) must be installed separately depending on the DB source type configured.

---

## Security Considerations

- **`oauth2_token`** and **`connection_string`** are marked `sensitive: True` in the configuration schema — they are encrypted at rest in the Kibana connector store and never logged.
- The URL crawler respects `allowed_domains` to prevent SSRF-style crawls to internal network addresses. For production, always set this field.
- SQL queries are passed as-is to SQLAlchemy's `text()`. The AI Hub platform should validate or allowlist queries before storing them in the connector configuration — this connector does not sanitize the SQL beyond what SQLAlchemy provides.
- `tenant_id` isolation relies on the AI Hub API gateway enforcing the `term` filter. This connector guarantees the field is present on every document; enforcement at query time is the gateway's responsibility.
