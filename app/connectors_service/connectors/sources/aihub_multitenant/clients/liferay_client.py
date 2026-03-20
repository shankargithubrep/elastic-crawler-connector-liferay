#
# Liferay AI Hub — Multi-Tenant Connector
# Liferay Headless Delivery API client (Documents & Media, Web Content, Blogs)
#

from functools import cached_property

import aiohttp
from connectors_sdk.logger import logger


PAGE_SIZE = 50
REQUEST_TIMEOUT = 60


class LiferayAuthError(Exception):
    pass


class LiferayClient:
    """Async client for Liferay Headless Delivery REST API.

    Handles paginated access to:
    - Documents & Media  (FILE source type)
    - Structured Web Content + Blog posts  (LIFERAY source type)
    """

    def __init__(self, base_url, oauth2_token):
        self.base_url = base_url.rstrip("/")
        self.oauth2_token = oauth2_token
        self._logger = logger

    def set_logger(self, logger_):
        self._logger = logger_

    @cached_property
    def _session(self):
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        headers = {
            "Authorization": f"Bearer {self.oauth2_token}",
            "Accept": "application/json",
        }
        return aiohttp.ClientSession(
            headers=headers,
            timeout=timeout,
            raise_for_status=True,
        )

    async def close(self):
        if "_session" in self.__dict__:
            await self._session.close()

    async def ping(self):
        """Verify connectivity and auth by hitting the sites endpoint."""
        url = f"{self.base_url}/o/headless-delivery/v1.0/sites/0"
        # A 404 is fine — it means auth passed. Anything else raises.
        try:
            async with self._session.get(url) as resp:
                return resp.status < 500
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return True
            if e.status in (401, 403):
                raise LiferayAuthError(
                    f"Liferay auth failed ({e.status}). Check oauth2_token."
                ) from e
            raise

    async def _paginate(self, url, params=None):
        """Async generator that pages through Liferay's standard paged responses.

        Yields each item dict from the `items` array.
        """
        base_params = {"pageSize": PAGE_SIZE, "page": 1}
        if params:
            base_params.update(params)

        while True:
            self._logger.debug(f"Fetching {url} page={base_params['page']}")
            try:
                async with self._session.get(url, params=base_params) as resp:
                    data = await resp.json()
            except aiohttp.ClientResponseError as e:
                self._logger.warning(f"Liferay API error at {url}: {e}")
                return

            items = data.get("items", [])
            for item in items:
                yield item

            total = data.get("totalCount", 0)
            fetched = (base_params["page"] - 1) * PAGE_SIZE + len(items)
            if fetched >= total or not items:
                break
            base_params["page"] += 1

    # ------------------------------------------------------------------ #
    # FILE source type — Documents & Media                                #
    # ------------------------------------------------------------------ #

    async def get_documents(self, folder_id):
        """Yield document dicts from a Documents & Media folder.

        Each yielded dict has: id, title, body, timestamp, content_url,
        file_extension, size_in_bytes.
        """
        url = (
            f"{self.base_url}/o/headless-delivery/v1.0"
            f"/document-folders/{folder_id}/documents"
        )
        async for item in self._paginate(url):
            doc_id = str(item.get("id", ""))
            content_url = item.get("contentUrl", "")

            body = await self._fetch_text_content(content_url)

            yield {
                "id": f"file-{doc_id}",
                "title": item.get("title", ""),
                "body": body,
                "timestamp": item.get("dateModified", ""),
                "content_url": content_url,
                "file_extension": item.get("fileExtension", ""),
                "size_in_bytes": item.get("sizeInBytes", 0),
                "creator": _extract_creator(item),
                "folder_id": folder_id,
            }

    async def _fetch_text_content(self, content_url):
        """Download document body as text (best-effort; returns '' on failure)."""
        if not content_url:
            return ""
        # Only attempt text-extractable types to avoid pulling down binaries
        lower = content_url.lower()
        if not any(lower.endswith(ext) for ext in (".txt", ".md", ".html", ".htm", ".json", ".xml", ".csv")):
            return ""
        try:
            async with self._session.get(content_url) as resp:
                return await resp.text(errors="replace")
        except Exception as e:
            self._logger.debug(f"Could not fetch content from {content_url}: {e}")
            return ""

    # ------------------------------------------------------------------ #
    # LIFERAY source type — Web Content + Blog Posts                      #
    # ------------------------------------------------------------------ #

    async def get_web_content(self, site_id):
        """Yield structured web content articles for a site."""
        url = (
            f"{self.base_url}/o/headless-delivery/v1.0"
            f"/sites/{site_id}/structured-contents"
        )
        async for item in self._paginate(url):
            content_id = str(item.get("id", ""))
            # The rendered HTML content lives under contentFields or renderedContents
            body = _extract_web_content_body(item)
            yield {
                "id": f"webcontent-{content_id}",
                "title": item.get("title", ""),
                "body": body,
                "timestamp": item.get("dateModified", ""),
                "content_type": "web_content",
                "site_id": site_id,
                "creator": _extract_creator(item),
                "friendly_url": item.get("friendlyUrlPath", ""),
            }

    async def get_blog_posts(self, site_id):
        """Yield blog posting dicts for a site."""
        url = (
            f"{self.base_url}/o/headless-delivery/v1.0"
            f"/sites/{site_id}/blog-postings"
        )
        async for item in self._paginate(url):
            post_id = str(item.get("id", ""))
            yield {
                "id": f"blog-{post_id}",
                "title": item.get("headline", ""),
                "body": item.get("articleBody", ""),
                "description": item.get("description", ""),
                "timestamp": item.get("dateModified", ""),
                "content_type": "blog_post",
                "site_id": site_id,
                "creator": _extract_creator(item),
                "friendly_url": item.get("friendlyUrlPath", ""),
            }


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _extract_creator(item):
    creator = item.get("creator", {})
    return creator.get("name", "") if isinstance(creator, dict) else ""


def _extract_web_content_body(item):
    """Pull readable text from a structured-content item.

    Prefers renderedContents (HTML render), falls back to contentFields values.
    """
    rendered = item.get("renderedContents", [])
    if rendered and isinstance(rendered, list):
        return rendered[0].get("renderedContentURL", "")

    parts = []
    for field in item.get("contentFields", []):
        value = field.get("contentFieldValue", {})
        text = value.get("data", "") or value.get("document", {}).get("title", "")
        if text:
            parts.append(str(text))
    return "\n".join(parts)
