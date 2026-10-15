"""Reading web pages and searching the web (no account or API key needed)."""

from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from .base import RUN, SAFE, ToolContext, ToolError, tool, truncate

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
MAX_BYTES = 3_000_000     # a page bigger than this is cut
MAX_SECONDS = 45          # for the whole download, not per read
TEXT_TYPES = ("text/", "html", "xml", "json", "javascript", "yaml", "csv", "markdown")


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.links: list[tuple[str, str]] = []
        self._href = None

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self.skip = 0   # a page that never closes <head> would otherwise show nothing
        if tag in self.SKIP:
            self.skip += 1
        if self.skip:
            return
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.out.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.out.append("- ")
        if tag == "a":
            self._href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "a":
            self._href = None

    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(data)
        if self._href and data.strip():
            self.links.append((data.strip(), self._href))


def html_to_text(page: str, links: list | None = None) -> str:
    """The readable text of a page. Pass a list as links to also get its (text, href) links."""
    p = _Text()
    p.feed(page)
    if links is not None:
        links.extend(p.links)
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _get(url: str, timeout=20):
    import requests

    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    except requests.RequestException as e:
        raise ToolError(f"Could not load {url}: {e}")
    return r


def _download(url: str) -> tuple:
    """(response, body bytes, note). Streams, so a huge file can't fill the memory."""
    import requests

    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20, stream=True)
    except requests.RequestException as e:
        raise ToolError(f"Could not load {url}: {e}")
    with r:
        ctype = r.headers.get("content-type", "").lower()
        if ctype and not any(t in ctype for t in TEXT_TYPES):
            raise ToolError(f"{r.url} is {ctype.split(';')[0]}, not a web page or text, so it wasn't downloaded.")
        body, note, start = bytearray(), "", time.time()
        try:
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) >= MAX_BYTES:
                    note = f"[cut: only the first {MAX_BYTES // 1_000_000} MB were downloaded]"
                    break
                if time.time() - start > MAX_SECONDS:
                    note = f"[cut: the download took over {MAX_SECONDS}s]"
                    break
        except requests.RequestException as e:
            if not body:
                raise ToolError(f"Could not load {url}: {e}")
            note = f"[cut: the connection broke ({type(e).__name__})]"
    if not ctype and b"\x00" in body[:2000]:
        raise ToolError(f"{r.url} is a binary file, not a web page or text.")
    return r, bytes(body), note


def _charset(ctype: str, body: bytes) -> str:
    """The header's charset, else a <meta charset> in the page, else UTF-8 (requests would
    guess ISO-8859-1 for any text/* without a charset)."""
    m = re.search(r"charset=[\"']?([\w.:-]+)", ctype, re.I) or \
        re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([\w.:-]+)", body[:4096], re.I)
    enc = m.group(1) if m else "utf-8"
    return enc.decode("ascii", "replace") if isinstance(enc, bytes) else enc


def _web_risk(ctx: ToolContext, a: dict) -> str:
    """A query string can carry data out (https://x/?q=<something read from your files>), so it asks."""
    return RUN if urlparse(str(a.get("url") or "").strip()).query else SAFE


@tool("web_fetch", "Download a web page and read its text.",
      {"url": {"type": "string"}}, required=["url"], group="web", risk_for=_web_risk,
      describe=lambda a: f"Open the web address {a.get('url')}")
def web_fetch(ctx: ToolContext, a: dict):
    url = str(a["url"]).strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    r, data, note = _download(url)
    ctype = r.headers.get("content-type", "")
    try:
        text = data.decode(_charset(ctype, data), errors="replace")
    except LookupError:   # a charset Python doesn't know
        text = data.decode("utf-8", errors="replace")
    text = text.lstrip("\ufeff")
    links: list = []
    if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        body = html_to_text(text, links)
    else:
        body = text
    seen, lines = set(), []
    for label, href in links:
        full = urljoin(r.url, href)
        if full.startswith(("http://", "https://")) and full.split("#")[0] not in seen and len(lines) < 15:
            seen.add(full.split("#")[0])
            lines.append(f"- {label[:80]}: {full}")
    more = "\n\nLinks on the page:\n" + "\n".join(lines) if lines else ""
    return truncate(f"[{r.status_code}] {r.url}{' ' + note if note else ''}\n\n{body}{more}",
                    ctx.settings.max_tool_output)


def parse_ddg(page: str, limit: int = 8) -> list[dict]:
    results = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                         page, re.S):
        href, title, rest = m.groups()
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()
        results.append({"title": clean(title), "url": href, "snippet": clean(snip.group(1) if snip else "")})
        if len(results) >= limit:
            break
    return results


@tool("web_search", "Search the web (DuckDuckGo) and get titles, links and snippets. Then use web_fetch to read a page.",
      {"query": {"type": "string"}}, required=["query"], group="web")
def web_search(ctx: ToolContext, a: dict):
    r = _get("https://html.duckduckgo.com/html/?q=" + quote_plus(a["query"]))
    results = parse_ddg(r.text)
    if not results:
        return "No results (the search page may have changed or blocked the request). Try web_fetch on a site directly."
    return "\n\n".join(f"{i + 1}. {x['title']}\n{x['url']}\n{x['snippet']}" for i, x in enumerate(results))
