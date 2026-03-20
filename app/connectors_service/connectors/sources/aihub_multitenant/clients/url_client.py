#
# Liferay AI Hub — Multi-Tenant Connector
# Async URL crawler client (BFS, depth-limited, domain-filtered)
#
# Uses only stdlib + aiohttp — no Playwright dependency.
# JavaScript-heavy SPAs will be crawled at the HTML level;
# for full JS rendering, swap _fetch() to use Playwright.
#

import asyncio
import hashlib
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import aiohttp
from connectors_sdk.logger import logger


REQUEST_TIMEOUT = 30
MAX_CONCURRENT_REQUESTS = 5
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB per page


class _LinkAndTextExtractor(HTMLParser):
    """Single-pass HTML parser: extracts visible text and <a href> links."""

    # Tags whose text content we skip (scripts, styles, etc.)
    _SKIP_TAGS = {"script", "style", "noscript", "head", "meta", "link"}

    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links = []
        self._text_parts = []
        self._skip_depth = 0
        self._current_tag = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self._current_tag = tag

        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

        if tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                absolute = urljoin(self.base_url, href)
                self.links.append(absolute)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._text_parts.append(text)

    @property
    def text(self):
        return " ".join(self._text_parts)


def _url_id(url):
    """Stable, ES-safe document ID for a URL."""
    return "url-" + hashlib.md5(url.encode()).hexdigest()


def _same_domain(url, allowed_domains):
    """Return True if url's hostname matches any of the allowed domains.

    If allowed_domains is empty, all domains are permitted.
    """
    if not allowed_domains:
        return True
    host = urlparse(url).hostname or ""
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


class UrlCrawlerClient:
    """Async BFS web crawler.

    Parameters
    ----------
    seed_urls:       list[str]  — starting URLs
    max_depth:       int        — how many link-hops from seeds (0 = seeds only)
    allowed_domains: list[str]  — restrict crawling to these domains (empty = all)
    """

    def __init__(self, seed_urls, max_depth, allowed_domains):
        self.seed_urls = seed_urls
        self.max_depth = max_depth
        self.allowed_domains = allowed_domains
        self._logger = logger
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    def set_logger(self, logger_):
        self._logger = logger_

    @property
    def _session(self):
        # One session per crawl() call — created lazily inside crawl()
        return self.__dict__.get("_cached_session")

    async def crawl(self):
        """Async generator: yields one doc dict per crawled page."""
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        headers = {"User-Agent": "ElasticAIHubConnector/1.0"}

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            self._cached_session = session

            visited = set()
            # queue items: (url, depth)
            queue = deque((url, 0) for url in self.seed_urls)

            while queue:
                url, depth = queue.popleft()

                if url in visited:
                    continue
                if not _same_domain(url, self.allowed_domains):
                    self._logger.debug(f"Skipping out-of-domain URL: {url}")
                    continue

                visited.add(url)
                self._logger.debug(f"Crawling [{depth}/{self.max_depth}] {url}")

                html, status = await self._fetch(session, url)
                if html is None:
                    continue

                parser = _LinkAndTextExtractor(base_url=url)
                try:
                    parser.feed(html)
                except Exception as e:
                    self._logger.warning(f"HTML parse error for {url}: {e}")

                body = parser.text
                if not body:
                    continue

                yield {
                    "id": _url_id(url),
                    "url": url,
                    "title": _extract_title(html),
                    "body": body,
                    "timestamp": "",   # no reliable last-modified from arbitrary pages
                    "http_status": status,
                    "depth": depth,
                }

                # Enqueue discovered links if we haven't hit max_depth
                if depth < self.max_depth:
                    for link in parser.links:
                        if link not in visited and _same_domain(link, self.allowed_domains):
                            queue.append((link, depth + 1))

    async def _fetch(self, session, url):
        """Fetch a URL, return (html_text, status) or (None, None) on error."""
        async with self._semaphore:
            try:
                async with session.get(url, allow_redirects=True) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    if "html" not in content_type:
                        self._logger.debug(
                            f"Skipping non-HTML content at {url} ({content_type})"
                        )
                        return None, None
                    # Read up to MAX_BODY_BYTES to avoid memory spikes
                    raw = await resp.content.read(MAX_BODY_BYTES)
                    html = raw.decode(errors="replace")
                    return html, resp.status
            except asyncio.TimeoutError:
                self._logger.warning(f"Timeout fetching {url}")
                return None, None
            except aiohttp.ClientError as e:
                self._logger.warning(f"HTTP error fetching {url}: {e}")
                return None, None
            except Exception as e:
                self._logger.warning(f"Unexpected error fetching {url}: {e}")
                return None, None


def _extract_title(html):
    """Best-effort <title> extraction from raw HTML."""
    lower = html.lower()
    start = lower.find("<title>")
    end = lower.find("</title>")
    if start != -1 and end != -1 and end > start:
        return html[start + 7 : end].strip()
    return ""
