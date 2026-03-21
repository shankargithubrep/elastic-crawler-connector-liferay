#
# Liferay AI Hub — Multi-Tenant Connector
#
# One connector instance handles all 4 source types (FILE, URL, DB, LIFERAY).
# Every document is stamped with tenant_id + datasource_id — this is the
# primary data-isolation mechanism across the shared Elastic Cloud cluster.
#
# Index naming convention (set externally by the AI Hub orchestration layer):
#   aihub-{tenant_id}-{datasource_id}
#
# Embeddings: Vertex AI inference endpoint via semantic_text field mapping
# (configured at index creation time, not inside this connector).
#

from connectors.source import BaseDataSource, ConfigurableFieldValueError

from connectors.sources.aihub_multitenant.clients import (
    DbClient,
    LiferayClient,
    UrlCrawlerClient,
)

# Source type constants — must match the dropdown option values below
SOURCE_TYPE_FILE = "FILE"
SOURCE_TYPE_URL = "URL"
SOURCE_TYPE_DB = "DB"
SOURCE_TYPE_LIFERAY = "LIFERAY"

ALL_SOURCE_TYPES = (SOURCE_TYPE_FILE, SOURCE_TYPE_URL, SOURCE_TYPE_DB, SOURCE_TYPE_LIFERAY)

# Fields required per source type (validated in validate_config)
_REQUIRED_BY_TYPE = {
    SOURCE_TYPE_FILE: ["liferay_base_url", "oauth2_token", "folder_id"],
    SOURCE_TYPE_URL: ["seed_urls"],
    SOURCE_TYPE_DB: ["connection_string", "sql_query"],
    SOURCE_TYPE_LIFERAY: ["liferay_base_url", "oauth2_token", "site_id"],
}


class AiHubMultiTenantDataSource(BaseDataSource):
    """Liferay AI Hub — Multi-Tenant connector.

    Supports four source types in a single connector instance:
    - FILE    : Liferay Documents & Media folder
    - URL     : BFS web crawler (any HTTP/HTTPS site)
    - DB      : Any SQLAlchemy-compatible database via raw SQL
    - LIFERAY : Liferay Web Content articles + Blog posts

    Every document produced is stamped with `tenant_id` and `datasource_id`
    to enforce data isolation at query time on the shared cluster.
    """

    name = "Liferay AI Hub Multi-Tenant"
    service_type = "aihub_multitenant"

    # Advanced rules / DLS not needed — isolation is by tenant index + field filter
    basic_rules_enabled = False
    advanced_rules_enabled = False
    dls_enabled = False

    def __init__(self, configuration):
        super().__init__(configuration=configuration)
        self.tenant_id = self.configuration["tenant_id"]
        self.datasource_id = self.configuration["datasource_id"]
        self.source_type = self.configuration["source_type"]

        # Clients are created lazily on first use
        self._liferay_client = None
        self._url_client = None
        self._db_client = None

    # ------------------------------------------------------------------ #
    # Configuration                                                        #
    # ------------------------------------------------------------------ #

    @classmethod
    def get_default_configuration(cls):
        return {
            # ── Tenant identity (always required) ──────────────────────
            "tenant_id": {
                "label": "Tenant ID",
                "order": 1,
                "tooltip": "The Liferay companyId for this customer tenant (e.g. cust0042). "
                           "Used as part of the index name and stamped on every document.",
                "type": "str",
            },
            "datasource_id": {
                "label": "Data Source ID",
                "order": 2,
                "tooltip": "Unique identifier for this data source within the tenant. "
                           "Used as part of the index name (aihub-{tenant_id}-{datasource_id}).",
                "type": "str",
            },
            # ── Source type selector ───────────────────────────────────
            "source_type": {
                "display": "dropdown",
                "label": "Source Type",
                "options": [
                    {"label": "Liferay Documents & Media (FILE)", "value": SOURCE_TYPE_FILE},
                    {"label": "Web / URL Crawler (URL)", "value": SOURCE_TYPE_URL},
                    {"label": "External Database (DB)", "value": SOURCE_TYPE_DB},
                    {"label": "Liferay Web Content & Blogs (LIFERAY)", "value": SOURCE_TYPE_LIFERAY},
                ],
                "order": 3,
                "tooltip": "The type of data source this connector instance will sync.",
                "type": "str",
                "value": SOURCE_TYPE_LIFERAY,
            },
            # ── Liferay connection (shared by FILE + LIFERAY) ──────────
            # Not gated by depends_on because both FILE and LIFERAY need them.
            # They are marked required=False here; validate_config enforces them
            # conditionally based on source_type.
            "liferay_base_url": {
                "label": "Liferay Base URL",
                "order": 4,
                "required": False,
                "tooltip": "Base URL of the Liferay instance (e.g. https://liferay.example.com). "
                           "Required for FILE and LIFERAY source types.",
                "type": "str",
            },
            "oauth2_token": {
                "label": "Liferay OAuth2 Bearer Token",
                "order": 5,
                "required": False,
                "sensitive": True,
                "tooltip": "OAuth2 access token for the Liferay Headless Delivery API. "
                           "Required for FILE and LIFERAY source types.",
                "type": "str",
            },
            # ── FILE-specific ──────────────────────────────────────────
            "folder_id": {
                "depends_on": [{"field": "source_type", "value": SOURCE_TYPE_FILE}],
                "label": "Documents & Media Folder ID",
                "order": 6,
                "required": False,
                "tooltip": "Liferay Documents & Media folder ID to sync (numeric).",
                "type": "str",
            },
            # ── URL-specific ───────────────────────────────────────────
            "seed_urls": {
                "depends_on": [{"field": "source_type", "value": SOURCE_TYPE_URL}],
                "label": "Seed URLs",
                "order": 7,
                "required": False,
                "tooltip": "Comma-separated list of URLs to start crawling from.",
                "type": "str",
            },
            "max_depth": {
                "default_value": 2,
                "depends_on": [{"field": "source_type", "value": SOURCE_TYPE_URL}],
                "display": "numeric",
                "label": "Maximum Crawl Depth",
                "order": 8,
                "required": False,
                "tooltip": "How many link-hops from the seed URLs to follow. 0 = seeds only.",
                "type": "int",
                "validations": [{"type": "greater_than", "constraint": -1}],
            },
            "allowed_domains": {
                "depends_on": [{"field": "source_type", "value": SOURCE_TYPE_URL}],
                "label": "Allowed Domains",
                "order": 9,
                "required": False,
                "tooltip": "Comma-separated domains to restrict crawling to (e.g. example.com). "
                           "Leave empty to allow all domains reachable from seed URLs.",
                "type": "str",
            },
            # ── DB-specific ────────────────────────────────────────────
            "connection_string": {
                "depends_on": [{"field": "source_type", "value": SOURCE_TYPE_DB}],
                "label": "Database Connection String",
                "order": 10,
                "required": False,
                "sensitive": True,
                "tooltip": "SQLAlchemy connection URL, e.g. postgresql+psycopg2://user:pass@host/db",
                "type": "str",
            },
            "sql_query": {
                "depends_on": [{"field": "source_type", "value": SOURCE_TYPE_DB}],
                "display": "textarea",
                "label": "SQL Query",
                "order": 11,
                "required": False,
                "tooltip": "SELECT query whose result rows will be indexed as documents. "
                           "Each row becomes one Elasticsearch document.",
                "type": "str",
            },
            # ── LIFERAY-specific ───────────────────────────────────────
            "site_id": {
                "depends_on": [{"field": "source_type", "value": SOURCE_TYPE_LIFERAY}],
                "label": "Liferay Site ID",
                "order": 12,
                "required": False,
                "tooltip": "Numeric Liferay site ID whose Web Content and Blog posts will be synced.",
                "type": "str",
            },
        }

    # ------------------------------------------------------------------ #
    # Validation                                                           #
    # ------------------------------------------------------------------ #

    async def validate_config(self):
        """Enforce that required fields for the chosen source_type are present."""
        self.configuration.check_valid()

        source_type = self.configuration["source_type"]
        if source_type not in ALL_SOURCE_TYPES:
            raise ConfigurableFieldValueError(
                f"source_type must be one of {ALL_SOURCE_TYPES}, got '{source_type}'."
            )

        missing = [
            field
            for field in _REQUIRED_BY_TYPE[source_type]
            if not self.configuration.get(field)
        ]
        if missing:
            raise ConfigurableFieldValueError(
                f"source_type '{source_type}' requires these fields to be set: "
                + ", ".join(missing)
            )

    # ------------------------------------------------------------------ #
    # Connectivity check                                                   #
    # ------------------------------------------------------------------ #

    async def ping(self):
        source_type = self.configuration["source_type"]
        self._logger.info(
            f"[{self.tenant_id}/{self.datasource_id}] Pinging source_type={source_type}"
        )
        if source_type in (SOURCE_TYPE_FILE, SOURCE_TYPE_LIFERAY):
            await self._get_liferay_client().ping()
        elif source_type == SOURCE_TYPE_DB:
            await self._get_db_client().ping()
        elif source_type == SOURCE_TYPE_URL:
            # For URL, just verify at least one seed URL is reachable
            client = self._get_url_client()
            async for _ in client.crawl():
                break   # one successful fetch is enough
        self._logger.info(
            f"[{self.tenant_id}/{self.datasource_id}] Ping OK for source_type={source_type}"
        )

    # ------------------------------------------------------------------ #
    # Cleanup                                                              #
    # ------------------------------------------------------------------ #

    async def close(self):
        if self._liferay_client is not None:
            await self._liferay_client.close()
        if self._db_client is not None:
            self._db_client.close()

    def _set_internal_logger(self):
        if self._liferay_client is not None:
            self._liferay_client.set_logger(self._logger)
        if self._url_client is not None:
            self._url_client.set_logger(self._logger)
        if self._db_client is not None:
            self._db_client.set_logger(self._logger)

    # ------------------------------------------------------------------ #
    # Main sync entrypoint                                                 #
    # ------------------------------------------------------------------ #

    async def get_docs(self, filtering=None):
        """Yield (doc, None) tuples for all documents in the configured source.

        Every document is guaranteed to contain:
          - id           : unique within this index
          - tenant_id    : isolates this tenant's data at query time
          - datasource_id: identifies which data source within the tenant
          - body / title : the primary text content for RAG embedding
          - timestamp    : ISO 8601 modification time (where available)
        """
        source_type = self.configuration["source_type"]
        self._logger.info(
            f"[{self.tenant_id}/{self.datasource_id}] Starting sync for source_type={source_type}"
        )

        count = 0
        try:
            match source_type:
                case "FILE":
                    async for doc, dl in self._get_file_docs():
                        yield doc, dl
                        count += 1
                case "URL":
                    async for doc, dl in self._get_url_docs():
                        yield doc, dl
                        count += 1
                case "DB":
                    async for doc, dl in self._get_db_docs():
                        yield doc, dl
                        count += 1
                case "LIFERAY":
                    async for doc, dl in self._get_liferay_docs():
                        yield doc, dl
                        count += 1
                case _:
                    raise ConfigurableFieldValueError(
                        f"Unknown source_type: '{source_type}'"
                    )
        finally:
            self._logger.info(
                f"[{self.tenant_id}/{self.datasource_id}] Sync complete. "
                f"Documents yielded: {count}"
            )

    # ------------------------------------------------------------------ #
    # Per-type doc generators                                              #
    # ------------------------------------------------------------------ #

    async def _get_file_docs(self):
        client = self._get_liferay_client()
        folder_id = self.configuration["folder_id"]
        async for doc in client.get_documents(folder_id):
            yield self._stamp(doc), None

    async def _get_url_docs(self):
        client = self._get_url_client()
        async for doc in client.crawl():
            yield self._stamp(doc), None

    async def _get_db_docs(self):
        client = self._get_db_client()
        async for doc in client.query():
            yield self._stamp(doc), None

    async def _get_liferay_docs(self):
        client = self._get_liferay_client()
        site_id = self.configuration["site_id"]
        async for doc in client.get_web_content(site_id):
            yield self._stamp(doc), None
        async for doc in client.get_blog_posts(site_id):
            yield self._stamp(doc), None

    # ------------------------------------------------------------------ #
    # Tenant isolation stamp — applied to EVERY document                  #
    # ------------------------------------------------------------------ #

    def _stamp(self, doc):
        """Inject mandatory tenant isolation fields.

        These fields must never be omitted or overridden by source data.
        They are the only mechanism preventing cross-tenant data leakage
        when the shared cluster is queried via the AI Hub API gateway.
        """
        doc["tenant_id"] = self.tenant_id
        doc["datasource_id"] = self.datasource_id
        return doc

    # ------------------------------------------------------------------ #
    # Lazy client factories                                                #
    # ------------------------------------------------------------------ #

    def _get_liferay_client(self):
        if self._liferay_client is None:
            self._liferay_client = LiferayClient(
                base_url=self.configuration["liferay_base_url"],
                oauth2_token=self.configuration["oauth2_token"],
            )
            self._liferay_client.set_logger(self._logger)
        return self._liferay_client

    def _get_url_client(self):
        if self._url_client is None:
            raw_seeds = self.configuration.get("seed_urls", "") or ""
            seed_urls = [u.strip() for u in raw_seeds.split(",") if u.strip()]

            raw_domains = self.configuration.get("allowed_domains", "") or ""
            allowed_domains = [d.strip() for d in raw_domains.split(",") if d.strip()]

            max_depth = self.configuration.get("max_depth", 2) or 2

            self._url_client = UrlCrawlerClient(
                seed_urls=seed_urls,
                max_depth=int(max_depth),
                allowed_domains=allowed_domains,
            )
            self._url_client.set_logger(self._logger)
        return self._url_client

    def _get_db_client(self):
        if self._db_client is None:
            self._db_client = DbClient(
                connection_string=self.configuration["connection_string"],
                sql_query=self.configuration["sql_query"],
            )
            self._db_client.set_logger(self._logger)
        return self._db_client
