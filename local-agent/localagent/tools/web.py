"""Reading web pages and searching the web (no account or API key needed)."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from .base import ToolContext, ToolError, tool, truncate

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.links: list[tuple[str, str]] = []
        self._href = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
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


def html_to_text(page: str) -> str:
    p = _Text()
    p.feed(page)
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


@tool("web_fetch", "Download a web page and read its text.",
      {"url": {"type": "string"}}, required=["url"], group="web")
def web_fetch(ctx: ToolContext, a: dict):
    url = a["url"].strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    r = _get(url)
    ctype = r.headers.get("content-type", "")
    if "html" in ctype or r.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        body = html_to_text(r.text)
    else:
        body = r.text
    return truncate(f"[{r.status_code}] {r.url}\n\n{body}", ctx.settings.max_tool_output)


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
