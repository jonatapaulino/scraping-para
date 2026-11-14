import logging
import random

import httpx

from app import config

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

JS_APP_MARKERS = ('id="app"', 'id="root"', "__NEXT_DATA__", "ng-app", "data-reactroot")


def _proxy() -> str | None:
    return random.choice(config.PROXY_POOL) if config.PROXY_POOL else None


def new_client(**kwargs) -> httpx.Client:
    return httpx.Client(
        proxy=_proxy(),
        timeout=config.HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": random.choice(USER_AGENTS)},
        **kwargs,
    )


def fetch_html(url: str, force_render: bool = False) -> str:
    """Baixa o HTML. Se force_render ou a pagina parecer SPA sem links, renderiza via Browserless."""
    if not force_render:
        with new_client() as c:
            resp = c.get(url)
            resp.raise_for_status()
            html = resp.text
        if config.RENDER_FALLBACK and "<a " not in html and any(m in html for m in JS_APP_MARKERS):
            log.info("Pagina parece SPA — rerenderizando via Browserless: %s", url)
            return render(url)
        return html
    return render(url)


def render(url: str) -> str:
    """Consome o Browserless (headless Chrome) para renderizar a pagina."""
    endpoint = f"{config.BROWSERLESS_URL}/content?token={config.BROWSERLESS_TOKEN}"
    with new_client(timeout=config.HTTP_TIMEOUT_SECONDS * 3) as c:
        resp = c.post(
            endpoint,
            json={"url": url, "gotoOptions": {"waitUntil": "networkidle2"}},
        )
        resp.raise_for_status()
        return resp.text
