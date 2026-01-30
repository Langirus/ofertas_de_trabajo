import os
import sys
import argparse
import time
import logging
import itertools
from datetime import datetime
from typing import List, Dict

import requests
from bs4 import BeautifulSoup

try:
    from googlesearch import search as gsearch
except Exception:
    # fallback name
    try:
        from googlesearch import search as gsearch
    except Exception:
        gsearch = None

from jinja2 import Template
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


SITES = [
    "lever.co",
    "greenhouse.io",
    "airavirtual.com",
    "getonboard.com",
]

KEYWORDS = ["Santiago", "Ingeniero", "Informática", "Junior"]


def generate_queries(sites: List[str], keywords: List[str]) -> List[str]:
    queries = []
    # use combinations of 2..len(keywords)
    for site in sites:
        for r in range(2, len(keywords) + 1):
            for combo in itertools.combinations(keywords, r):
                q = f"site:{site} " + " ".join(combo)
                queries.append(q)
    # deduplicate while preserving order
    seen = set()
    out = []
    for q in queries:
        if q not in seen:
            out.append(q)
            seen.add(q)
    return out


def try_search(query: str, max_results: int = 10) -> List[str]:
    if gsearch is None:
        logger.error("googlesearch library not available. Install googlesearch-python")
        return []

    # Try different common signatures
    try:
        # common signature: search(query, num=..., stop=..., pause=...)
        return list(gsearch(query, num=10, stop=max_results, pause=2))
    except TypeError:
        try:
            # alternative signature: search(query, num_results=...)
            return list(gsearch(query, num_results=max_results))
        except Exception as e:
            logger.exception("Error calling search: %s", e)
            return []
    except Exception as e:
        logger.exception("Search failed: %s", e)
        return []


def fetch_metadata(url: str) -> Dict[str, str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; JobSearchBot/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Failed to fetch %s: %s", url, e)
        return {"url": url, "title": url, "desc": "(No se pudo leer la página)"}

    soup = BeautifulSoup(resp.text, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else url

    desc = ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        desc = meta.get("content").strip()
    else:
        p = soup.find("p")
        if p and p.get_text(strip=True):
            desc = p.get_text(strip=True)
    if not desc:
        desc = "(Sin descripción disponible)"

    # truncate
    if len(desc) > 400:
        desc = desc[:400].rsplit(" ", 1)[0] + "..."

    return {"url": url, "title": title, "desc": desc}


HTML_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Reporte diario - Ofertas</title>
  <style>
    body{font-family:Inter, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial; background:#f6f8fb; color:#0f172a;}
    .container{max-width:800px;margin:24px auto;background:#fff;padding:20px;border-radius:8px;box-shadow:0 6px 18px rgba(15,23,42,0.06)}
    h1{margin:0 0 12px;font-size:20px}
    .meta{color:#667085;font-size:13px;margin-bottom:16px}
    .job{padding:12px;border-radius:8px;border:1px solid #eef2ff;margin-bottom:12px}
    .job a{color:#0b5fff;text-decoration:none;font-weight:600}
    .desc{color:#334155;margin-top:6px;font-size:14px}
    footer{color:#94a3b8;font-size:12px;margin-top:18px}
  </style>
</head>
<body>
  <div class="container">
    <h1>Reporte diario — Ofertas encontradas</h1>
    <div class="meta">Generado: {{ generated_at }} — Fuente: Búsqueda automática</div>
    {% if offers %}
      {% for o in offers %}
        <div class="job">
          <div><a href="{{ o.url }}" target="_blank" rel="noopener">{{ o.title }}</a></div>
          <div class="desc">{{ o.desc }}</div>
        </div>
      {% endfor %}
    {% else %}
      <div class="job">No se encontraron ofertas nuevas con los filtros especificados.</div>
    {% endif %}
    <footer>Este correo fue enviado automáticamente. Variables usadas: SITES={{ sites }}</footer>
  </div>
</body>
</html>
"""


def build_html(offers: List[Dict[str, str]]) -> str:
    tpl = Template(HTML_TEMPLATE)
    return tpl.render(offers=offers, generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), sites=", ".join(SITES))


def send_email(html_body: str, subject: str):
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    receiver = os.environ.get("EMAIL_RECEIVER")

    if not sender or not password or not receiver:
        logger.error("Missing EMAIL_SENDER, EMAIL_PASSWORD or EMAIL_RECEIVER environment variables")
        raise SystemExit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver

    part = MIMEText(html_body, "html")
    msg.attach(part)

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.sendmail(sender, receiver, msg.as_string())
            logger.info("Email enviado a %s", receiver)
    except Exception as e:
        logger.exception("Error enviando email: %s", e)
        raise


def main():
    logger.info("Inicio de búsqueda de ofertas")
    queries = generate_queries(SITES, KEYWORDS)
    logger.info("Total queries: %d", len(queries))

    found_urls = []
    seen = set()
    # limit per query to avoid too many requests
    for q in queries:
        logger.info("Buscando: %s", q)
        try:
            results = try_search(q, max_results=8)
        except Exception as e:
            logger.warning("Search error for query '%s': %s", q, e)
            results = []

        for url in results:
            if not url or url in seen:
                continue
            seen.add(url)
            found_urls.append(url)
        # friendly pause
        time.sleep(1)

    logger.info("URLs únicas encontradas: %d", len(found_urls))

    offers = []
    for url in found_urls:
        meta = fetch_metadata(url)
        offers.append(meta)
        # short sleep to be polite
        time.sleep(0.5)

    html = build_html(offers)
    subject = f"[JobSearch] Reporte ofertas — {datetime.utcnow().strftime('%Y-%m-%d') }"

    dry_run = os.environ.get("DRY_RUN") == "1" or "--dry-run" in sys.argv
    if dry_run:
        filename = f"report_preview_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.html"
        out_path = os.path.join(os.getcwd(), filename)
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("DRY_RUN: reporte guardado en %s", out_path)
        except Exception:
            logger.exception("Error guardando el reporte de previsualización")
    else:
        try:
            send_email(html, subject)
        except Exception:
            logger.exception("No se pudo enviar el correo.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrumpido por usuario")
        sys.exit(0)
    except Exception:
        logger.exception("Error no esperado en main")
        sys.exit(1)
