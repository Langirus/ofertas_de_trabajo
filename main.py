# -*- coding: utf-8 -*-
import os
import sys
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional

import re
import requests
import urllib.parse
from bs4 import BeautifulSoup

try:
    from googlesearch import search as gsearch
except Exception:
    gsearch = None

from jinja2 import Template
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from groq_scorer import build_groq_client, load_cv_profile, score_job_with_ai, MIN_SCORE_THRESHOLD

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN ---
SITES = [
    "lever.co", "greenhouse.io", "airavirtual.com", "getonboard.com",
    "empleospublicos.cl", "laborum.cl", "computrabajo.cl", "bumeran.cl",
    "infojobs.net", "tecnoempleo.com", "trabajando.com",
]

# Alerta inmediata: ofertas publicadas hace menos de X minutos con score >= Y
FRESH_THRESHOLD_MINUTES = 90
FRESH_MIN_SCORE = 65
MAX_WORKERS = 8  # Hilos paralelos para scraping
REQ_TIMEOUT = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# Áreas de TI (Whitelist 1)
WHITELIST_TI = [
    "Informática", "TI", "IT", "Sistemas", "Software", "Programador", "Desarrollador", 
    "Developer", "Computación", "Soporte", "Redes", "Ciberseguridad", "Datos", "Data", 
    "Cloud", "Infraestructura", "QA", "Testing", "Informático", "Telecomunicaciones",
    "Web", "Frontend", "Backend", "Fullstack", "Java", "Python", "Javascript", "React",
    "Node", "SQL", "Base de datos", "Ingeniero", "Ingeniería", "Tecnología"
]

# Niveles Junior/Trainee (Whitelist 2)
WHITELIST_LEVEL = [
    "Junior", "Trainee", "Práctica", "Practicante", "Entry Level", "Sin experiencia", 
    "Analista", "Egresado", "Recién graduado", "Nivel inicial", "Analyst", "Jr"
]

# Blacklist: Filtros para descartar Senior o áreas ajenas
BLACKLIST = [
    "Senior", "Sr", "Lead", "Principal", "Jefe", "Director", "Manager", "Expert", 
    "Civil", "Viales", "Construcción", "Hidráulica", "Minas", "Minería", "BIM", 
    "Arquitecto", "Comercial", "Ventas", "Psicólogo", "Contador", "Social", "Veterinario",
    "Médico", "Enfermero", "Abogado", "Recursos Humanos", "RRHH"
]

# Palabras clave para detección de idioma (Heurística simple)
SPANISH_STOPWORDS = {
    'de', 'la', 'el', 'en', 'y', 'a', 'que', 'los', 'se', 'del', 'las', 'un', 'con', 'no', 'una', 
    'su', 'para', 'es', 'al', 'lo', 'como', 'más', 'pero', 'sus', 'le', 'ya', 'o', 'este', 'sí', 
    'porque', 'esta', 'entre', 'cuando', 'muy', 'sin', 'sobre', 'también', 'me', 'hasta', 'hay', 
    'donde', 'quien', 'desde', 'todo', 'nos', 'durante', 'todos', 'uno', 'les', 'ni', 'vacante',
    'requerimientos', 'conocimientos', 'manejo', 'experiencia', 'postular', 'postulación'
}

ENGLISH_STOPWORDS = {
    'the', 'of', 'and', 'a', 'to', 'in', 'is', 'you', 'that', 'it', 'he', 'was', 'for', 'on', 
    'are', 'as', 'with', 'his', 'they', 'at', 'be', 'this', 'have', 'from', 'or', 'one', 'had', 
    'by', 'word', 'but', 'not', 'what', 'all', 'were', 'we', 'when', 'your', 'can', 'said', 
    'there', 'use', 'an', 'each', 'which', 'she', 'do', 'how', 'their', 'if', 'will', 'job',
    'requirements', 'skills', 'experience', 'apply'
}

SEEN_FILE = "seen_jobs.txt"

LOCATION_CHILE = "Chile"
# ---------------------------------

def is_spanish_content(text: str) -> bool:
    """
    Heurística para determinar si un texto está en español.
    Compara la frecuencia de palabras comunes en español vs inglés.
    """
    words = re.findall(r'\b\w+\b', text.lower())
    if not words:
        return False
    
    es_count = sum(1 for w in words if w in SPANISH_STOPWORDS)
    en_count = sum(1 for w in words if w in ENGLISH_STOPWORDS)
    
    # Si hay una clara mayoría de español o presencia de palabras clave ES, aceptamos.
    # Damos un pequeño margen al español por ser el objetivo.
    return es_count >= en_count or es_count > 0

def is_relevant(title: str, desc: str = "", location: str = "Chile") -> bool:
    """
    Verifica si el puesto es relevante:
    1. No debe tener palabras de la blacklist.
    2. Debe tener al menos una palabra de TI.
    3. Debe tener al menos una palabra de nivel (Junior/Trainee/Analista).
    4. Debe estar en español o ser para hispanohablantes.
    5. Si NO es en Chile, debe ser Remoto.
    """
    title_lower = title.lower()
    desc_lower = desc.lower()
    
    # 1. Filtro Blacklist (Exclusión total)
    for black in BLACKLIST:
        if black.lower() in title_lower:
            return False
            
    # 2. Verificar si es de TI (con coincidencia de palabra completa para términos cortos)
    has_ti = False
    for ti in WHITELIST_TI:
        ti_l = ti.lower()
        if len(ti_l) <= 3:
            # Word boundary check for short terms like "TI", "IT", "QA"
            if re.search(rf"\b{re.escape(ti_l)}\b", title_lower):
                has_ti = True
                break
        elif ti_l in title_lower:
            has_ti = True
            break
            
    if not has_ti:
        return False
        
    # 3. Verificar si es nivel inicial
    has_level = any(level.lower() in title_lower for level in WHITELIST_LEVEL)
    if not has_level:
        return False

    # 4. Filtro de Idioma
    content_to_check = f"{title} {desc}"
    if not is_spanish_content(content_to_check):
        logger.info(f"Descartado por idioma: {title}")
        return False

    # 5. Si no es Chile, DEBE ser remoto
    if location.lower() != "chile":
        remote_keywords = ["remote", "remoto", "teletrabajo", "home office", "anywhere", "en cualquier lugar"]
        is_remote = any(kw in title_lower or kw in desc_lower for kw in remote_keywords)
        if not is_remote:
            logger.info(f"Descartado por no ser remoto fuera de Chile ({location}): {title}")
            return False
            
    return True

def try_search(query: str, max_results: int = 5) -> List[str]:
    if gsearch is None:
        return []
    try:
        results = gsearch(query, num_results=max_results, lang="es")
        return list(results)
    except Exception as e:
        logger.warning("Error en búsqueda Google: %s", e)
        return []

def fetch_metadata(url: str) -> Dict[str, str]:
    headers = {"User-Agent": "Mozilla/5.0"}
    title = url.split("/")[-1].replace("-", " ").replace("_", " ")
    desc = "Sin descripción disponible."
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            if soup.title:
                title = soup.title.string.strip()
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                desc = meta_desc["content"][:300] + "..."
    except Exception:
        pass
        
    return {"url": url, "title": title, "desc": desc}

def fetch_linkedin_jobs(keywords: List[str], location: str = "Chile", hours: int = 24, remote_only: bool = False) -> List[Dict[str, str]]:
    """
    LinkedIn guest search scraper.
    """
    tpr = "r3600" if hours <= 1 else f"r{hours * 3600}"
    query = urllib.parse.quote(" ".join(keywords))
    loc = urllib.parse.quote(location)
    
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={query}&location={loc}&f_TPR={tpr}&sortBy=DD&start=0"
    
    if remote_only:
        url += "&f_WT=2"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    
    logger.info("Scrapeando LinkedIn: keywords=%s, location=%s, remote=%s", keywords, location, remote_only)
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Error fetching LinkedIn (%s): %s", location, e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    job_cards = soup.find_all("li")
    
    offers = []
    for card in job_cards:
        try:
            title_tag = card.find("h3", class_="base-search-card__title")
            company_tag = card.find("h4", class_="base-search-card__subtitle")
            link_tag = card.find("a", class_="base-card__full-link")
            
            if title_tag and link_tag:
                title = title_tag.get_text(strip=True)
                
                # Check relevance with location context
                if not is_relevant(title, location=location):
                    continue
                
                company = company_tag.get_text(strip=True) if company_tag else "N/A"
                link = link_tag["href"].split("?")[0]
                
                offers.append({
                    "url": link,
                    "title": title,
                    "company": company,
                    "location": location,
                    "desc": f"Publicado recientemente en {location}."
                })
        except Exception:
            continue
            
    return offers


# ── Nuevos scrapers ────────────────────────────────────────────────────────────

def fetch_remoteok_jobs() -> List[Dict]:
    """RemoteOK: RSS público sin bloqueos, incluye fecha de publicación."""
    urls = [
        "https://remoteok.com/remote-junior-dev-jobs.rss",
        "https://remoteok.com/remote-python-jobs.rss",
    ]
    offers = []
    for feed_url in urls:
        try:
            resp = requests.get(feed_url, headers=HEADERS, timeout=REQ_TIMEOUT)
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item"):
                title = item.findtext("title", "").strip()
                link  = item.findtext("link",  "").strip()
                desc  = BeautifulSoup(item.findtext("description", ""), "html.parser").get_text()[:300]
                pub   = item.findtext("pubDate", "")
                mins  = None
                if pub:
                    try:
                        dt   = parsedate_to_datetime(pub)
                        mins = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
                    except Exception:
                        pass
                if link and is_relevant(title, desc, "Internacional"):
                    offers.append({"url": link, "title": title, "company": "RemoteOK",
                                   "location": "Remoto", "desc": desc,
                                   "published_minutes": mins})
        except Exception as e:
            logger.warning("RemoteOK RSS error (%s): %s", feed_url, e)
    return offers


def fetch_getonboard_jobs() -> List[Dict]:
    """GetOnBoard: API pública oficial con fechas exactas de publicación."""
    offers = []
    for page in range(1, 3):
        try:
            url = (f"https://www.getonbrd.com/api/v0/categories/programming/jobs"
                   f"?per_page=20&page={page}&published=true")
            resp = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
            resp.raise_for_status()
            for job in resp.json().get("data", []):
                attrs   = job.get("attributes", {})
                title   = attrs.get("title", "")
                comp    = (attrs.get("company") or {})
                company = (comp.get("data") or {}).get("attributes", {}).get("name", "N/A")
                remote  = attrs.get("remote", False)
                loc     = "Remoto" if remote else "Chile"
                desc    = BeautifulSoup(attrs.get("description", ""), "html.parser").get_text()[:300]
                jid     = job.get("id", "")
                jurl    = f"https://www.getonbrd.com/jobs/{jid}"
                mins    = None
                pub_at  = attrs.get("published_at") or attrs.get("updated_at")
                if pub_at:
                    try:
                        dt   = datetime.fromisoformat(pub_at.replace("Z", "+00:00"))
                        mins = int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
                    except Exception:
                        pass
                if is_relevant(title, desc, loc):
                    offers.append({"url": jurl, "title": title, "company": company,
                                   "location": loc, "desc": desc,
                                   "published_minutes": mins})
        except Exception as e:
            logger.warning("GetOnBoard API error (page %d): %s", page, e)
    return offers


def fetch_torre_jobs() -> List[Dict]:
    """Torre.ai: API pública para Latam, enfocada en perfiles tech."""
    offers = []
    queries = [
        {"skill": "python", "career-stages": ["early"]},
        {"skill": "javascript", "career-stages": ["early"]},
        {"skill": "kotlin", "career-stages": ["early"]},
    ]
    for body in queries:
        try:
            body["remote"] = True
            body["size"]   = 15
            resp = requests.post("https://torre.ai/api/opportunities/_search",
                                 json=body, headers=HEADERS, timeout=REQ_TIMEOUT)
            for item in resp.json().get("results", []):
                opp     = item.get("opportunity", item)
                title   = opp.get("objective", "")
                orgs    = opp.get("organizations") or [{}]
                company = orgs[0].get("name", "N/A") if orgs else "N/A"
                slug    = opp.get("id", "")
                jurl    = f"https://torre.ai/opportunities/{slug}"
                if is_relevant(title, "", "Internacional"):
                    offers.append({"url": jurl, "title": title, "company": company,
                                   "location": "Remoto Latam", "desc": f"Torre.ai – Remoto",
                                   "published_minutes": None})
        except Exception as e:
            logger.warning("Torre.ai error: %s", e)
    return offers


def fetch_all_offers() -> List[Dict]:
    """Ejecuta todos los scrapers en paralelo y devuelve la lista combinada."""
    combos_chile = [
        ["Junior", "Software"], ["Trainee", "Informática"],
        ["Soporte", "TI"],      ["Analista", "Sistemas"],
        ["Práctica", "Python"], ["Junior", "IT"],
    ]
    combos_remote = [
        ["Junior", "Remoto", "Desarrollador"],
        ["Trainee", "Remoto", "Sistemas"],
        ["Programador", "Junior"],
    ]
    regiones = ["Latin America", "Spain", "Mexico", "Argentina"]

    tasks: List[tuple] = []
    # LinkedIn Chile
    for c in combos_chile:
        tasks.append(("LI-Chile",  fetch_linkedin_jobs, (c, "Chile", 2, False)))
    # LinkedIn Remoto (reducido a 3 regiones × 3 combos)
    for loc in regiones:
        for c in combos_remote:
            tasks.append(("LI-Remoto", fetch_linkedin_jobs, (c, loc, 2, True)))
    # Fuentes nuevas
    tasks.append(("RemoteOK",  fetch_remoteok_jobs,  ()))
    tasks.append(("GetOnBoard", fetch_getonboard_jobs, ()))
    tasks.append(("Torre.ai",  fetch_torre_jobs,      ()))
    # Google ATS (en hilo aparte para no bloquear)
    tasks.append(("Google-ATS", _fetch_google_ats,   ()))

    all_offers: List[Dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fmap = {ex.submit(fn, *args): name for name, fn, args in tasks}
        for fut in as_completed(fmap):
            name = fmap[fut]
            try:
                results = fut.result()
                if results:
                    logger.info("  ✓ %-12s %2d ofertas", name, len(results))
                    all_offers.extend(results)
            except Exception as e:
                logger.warning("  ✗ %-12s %s", name, e)
    return all_offers


def _fetch_google_ats() -> List[Dict]:
    """Búsqueda Google site: en portales ATS (ejecutado en un solo hilo)."""
    if gsearch is None:
        return []
    offers = []
    seen_urls: set = set()
    queries = []
    for site in SITES:
        for level in ["Junior", "Trainee"]:
            queries.append(f"site:{site} {level} (Informática OR Sistemas OR TI OR Python OR Desarrollador)")
    for q in queries:
        try:
            for url in gsearch(q, num_results=3, lang="es"):
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    meta = fetch_metadata(url)
                    gloc = "Internacional"
                    if "chile" in meta["title"].lower() or "chile" in url.lower():
                        gloc = "Chile"
                    if is_relevant(meta["title"], meta["desc"], gloc):
                        parts   = meta["title"].split(" at ")
                        company = parts[-1].strip() if len(parts) >= 2 else "N/A"
                        offers.append({**meta, "company": company,
                                       "location": "Mundial", "published_minutes": None})
            time.sleep(1.5)
        except Exception as e:
            logger.warning("Google ATS error: %s", e)
    return offers


HTML_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Reporte de Ofertas TI Junior — Match IA</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    *{box-sizing:border-box; margin:0; padding:0;}
    body{font-family:'Inter',sans-serif; background:#0f1117; color:#e2e8f0; line-height:1.6; padding:20px;}
    .container{max-width:820px; margin:0 auto;}
    .header{background:linear-gradient(135deg,#1e3a5f,#0d2137); border-radius:16px; padding:28px 30px; margin-bottom:24px; border:1px solid #1e3a5f;}
    .header h1{font-size:22px; font-weight:700; color:#e2e8f0; margin-bottom:6px;}
    .header .meta{font-size:13px; color:#64748b;}
    .stats{display:flex; gap:12px; margin-bottom:24px; flex-wrap:wrap;}
    .stat{background:#1a1f2e; border-radius:10px; padding:14px 20px; flex:1; min-width:140px; border:1px solid #1e293b; text-align:center;}
    .stat .num{font-size:26px; font-weight:700; line-height:1;}
    .stat .lbl{font-size:11px; color:#64748b; margin-top:4px; text-transform:uppercase; letter-spacing:.5px;}
    .num-alto{color:#4ade80;} .num-medio{color:#facc15;} .num-bajo{color:#f87171;} .num-total{color:#60a5fa;}
    .job{background:#1a1f2e; border-radius:12px; border:1px solid #1e293b; padding:18px 20px; margin-bottom:14px; transition:.2s;}
    .job:hover{border-color:#3b82f6; background:#1e2538;}
    .job-header{display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:8px;}
    .job-title a{color:#93c5fd; text-decoration:none; font-weight:600; font-size:16px; line-height:1.3;}
    .job-title a:hover{color:#60a5fa; text-decoration:underline;}
    .job-meta{font-size:12px; color:#64748b; margin-top:3px;}
    .score-box{display:flex; flex-direction:column; align-items:center; min-width:64px;}
    .score-num{font-size:22px; font-weight:700; line-height:1;}
    .score-lbl{font-size:10px; text-transform:uppercase; letter-spacing:.5px; margin-top:2px;}
    .alto .score-num, .alto .score-lbl{color:#4ade80;}
    .medio .score-num, .medio .score-lbl{color:#facc15;}
    .bajo .score-num, .bajo .score-lbl{color:#f87171;}
    .score-bar-bg{height:4px; background:#2d3748; border-radius:2px; margin-top:5px; width:64px;}
    .score-bar{height:4px; border-radius:2px;}
    .alto .score-bar{background:#4ade80;} .medio .score-bar{background:#facc15;} .bajo .score-bar{background:#f87171;}
    .ai-reason{font-size:13px; color:#94a3b8; margin-top:6px; font-style:italic; padding-left:10px; border-left:2px solid #334155;}
    .badges{display:flex; gap:6px; flex-wrap:wrap; margin-top:8px;}
    .badge{display:inline-block; padding:2px 8px; border-radius:4px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.5px;}
    .badge-alto{background:rgba(74,222,128,.15); color:#4ade80; border:1px solid rgba(74,222,128,.3);}
    .badge-medio{background:rgba(250,204,21,.15); color:#facc15; border:1px solid rgba(250,204,21,.3);}
    .badge-bajo{background:rgba(248,113,113,.15); color:#f87171; border:1px solid rgba(248,113,113,.3);}
    .badge-remote{background:rgba(96,165,250,.15); color:#60a5fa; border:1px solid rgba(96,165,250,.3);}
    .empty{text-align:center; color:#64748b; padding:40px; background:#1a1f2e; border-radius:12px;}
    footer{text-align:center; color:#475569; font-size:12px; margin-top:30px; padding:20px;}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🤖 Reporte TI Junior — Evaluado por IA</h1>
      <div class="meta">Generado: {{ generated_at }} · Perfil: Fernando Rabanal · Modelo: Llama 3.3 70B (Groq)</div>
    </div>

    {% set alto = offers | selectattr('ai_fit_level', 'equalto', 'Alto') | list %}
    {% set medio = offers | selectattr('ai_fit_level', 'equalto', 'Medio') | list %}
    {% set bajo = offers | selectattr('ai_fit_level', 'equalto', 'Bajo') | list %}

    <div class="stats">
      <div class="stat"><div class="num num-total">{{ offers | length }}</div><div class="lbl">Total</div></div>
      <div class="stat"><div class="num num-alto">{{ alto | length }}</div><div class="lbl">Match Alto</div></div>
      <div class="stat"><div class="num num-medio">{{ medio | length }}</div><div class="lbl">Match Medio</div></div>
      <div class="stat"><div class="num num-bajo">{{ bajo | length }}</div><div class="lbl">Match Bajo</div></div>
    </div>

    {% if offers %}
      {% for o in offers %}
        {% set fit = o.ai_fit_level | lower %}
        <div class="job {{ fit }}">
          <div class="job-header">
            <div class="job-title">
              <a href="{{ o.url }}" target="_blank" rel="noopener">{{ o.title }}</a>
              <div class="job-meta">{{ o.company }} · {{ o.location }}</div>
              <div class="badges">
                <span class="badge badge-{{ fit }}">Match {{ o.ai_fit_level }}</span>
                {% if 'remoto' in o.display_title | lower or 'remote' in o.display_title | lower %}
                  <span class="badge badge-remote">Remoto</span>
                {% endif %}
              </div>
            </div>
            <div class="score-box">
              <div class="score-num">{{ o.ai_score }}%</div>
              <div class="score-lbl">Match</div>
              <div class="score-bar-bg"><div class="score-bar" style="width:{{ o.ai_score }}%"></div></div>
            </div>
          </div>
          <div class="ai-reason">💡 {{ o.ai_reason }}</div>
        </div>
      {% endfor %}
    {% else %}
      <div class="empty">😴 No se encontraron ofertas nuevas que cumplan con los filtros.</div>
    {% endif %}
    <footer>Reporte generado automáticamente · Powered by Groq + Llama 3.3 70B</footer>
  </div>
</body>
</html>
"""

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_seen(urls):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for url in sorted(urls):
            f.write(url + "\n")

def generate_key(offer):
    # Genera una clave única basada en título y empresa para evitar duplicados de diferentes fuentes
    title = offer.get("title", "").lower()
    company = offer.get("company", "n/a").lower()
    # Limpiar caracteres especiales y espacios extras
    clean_title = re.sub(r'[^a-z0-9]', '', title)
    clean_company = re.sub(r'[^a-z0-9]', '', company)
    return f"{clean_title}|{clean_company}"

ALERT_TEMPLATE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>🚨 Alerta Empleo Reciente</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
  body{font-family:'Inter',sans-serif;background:#0a0f1e;color:#e2e8f0;padding:20px;}
  .wrap{max-width:700px;margin:0 auto;}
  .banner{background:linear-gradient(135deg,#7c3aed,#4f46e5);border-radius:14px;padding:24px 28px;margin-bottom:20px;}
  .banner h1{font-size:20px;font-weight:700;margin-bottom:4px;}
  .banner p{font-size:13px;color:#c4b5fd;}
  .job{background:#13192b;border:1px solid #312e81;border-radius:11px;padding:16px 18px;margin-bottom:12px;}
  .job a{color:#a5b4fc;font-weight:600;font-size:15px;text-decoration:none;}
  .job a:hover{text-decoration:underline;}
  .meta{font-size:12px;color:#6366f1;margin-top:4px;}
  .score{font-size:18px;font-weight:700;color:#4ade80;float:right;}
  .reason{font-size:13px;color:#94a3b8;margin-top:8px;font-style:italic;}
  footer{text-align:center;color:#4a5568;font-size:11px;margin-top:24px;}
</style></head><body><div class="wrap">
  <div class="banner">
    <h1>🚨 Oferta(s) recién publicada(s) con alto match</h1>
    <p>{{ offers|length }} empleo(s) publicado(s) en los últimos {{ threshold }} minutos · {{ generated_at }}</p>
  </div>
  {% for o in offers %}
  <div class="job">
    <span class="score">{{ o.ai_score }}%</span>
    <a href="{{ o.url }}" target="_blank">{{ o.title }}</a>
    <div class="meta">{{ o.company }} · {{ o.location }}
      {% if o.published_minutes is not none %} · hace {{ o.published_minutes }} min{% endif %}
    </div>
    <div class="reason">💡 {{ o.ai_reason }}</div>
  </div>
  {% endfor %}
  <footer>Alerta automática · Powered by Groq + Llama 3.3 70B</footer>
</div></body></html>"""


def score_offers_parallel(offers: List[Dict], cv_profile: str, groq_client) -> None:
    """Evalúa todas las ofertas con Groq AI usando hasta 3 hilos paralelos."""
    if not groq_client or not cv_profile:
        for o in offers:
            o.update({"ai_score": 50, "ai_reason": "IA no disponible.", "ai_fit_level": "Medio"})
        return

    def _score_one(offer):
        result = score_job_with_ai(offer, cv_profile, groq_client)
        offer.update({"ai_score": result["score"],
                      "ai_reason": result["reason"],
                      "ai_fit_level": result["fit_level"]})

    logger.info("Evaluando %d ofertas con Groq AI (paralelo)...", len(offers))
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(_score_one, o): o.get("title", "?") for o in offers}
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                fut.result()
            except Exception as e:
                logger.warning("Error scoring '%s': %s", futures[fut], e)
            if done % 5 == 0:
                logger.info("  Scored %d/%d", done, len(offers))


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_seen(keys):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for k in sorted(keys):
            f.write(k + "\n")

def generate_key(offer):
    title   = re.sub(r'[^a-z0-9]', '', offer.get("title",   "").lower())
    company = re.sub(r'[^a-z0-9]', '', offer.get("company", "n/a").lower())
    return f"{title}|{company}"


def main():
    logger.info("=== Iniciando búsqueda TI (paralela, %d fuentes) ===", MAX_WORKERS)

    # ── 1. Scraping paralelo de todas las fuentes ──────────────────────────────
    all_raw = fetch_all_offers()
    logger.info("Total bruto: %d ofertas", len(all_raw))

    # ── 2. Deduplicar contra historial ─────────────────────────────────────────
    seen_keys = load_seen()
    new_offers: List[Dict] = []
    for o in all_raw:
        key = generate_key(o)
        if key not in seen_keys:
            new_offers.append(o)
            seen_keys.add(key)
            seen_keys.add(o.get("url", ""))

    logger.info("Nuevas (no vistas antes): %d", len(new_offers))
    if not new_offers:
        logger.info("Nada nuevo. Fin.")
        return

    # ── 3. Scoring IA paralelo ─────────────────────────────────────────────────
    groq_client = build_groq_client()
    cv_profile  = load_cv_profile()
    score_offers_parallel(new_offers, cv_profile, groq_client)

    # ── 4. Filtrar por score mínimo y ordenar ──────────────────────────────────
    scored = [o for o in new_offers if o.get("ai_score", 0) >= MIN_SCORE_THRESHOLD]
    scored.sort(key=lambda x: x.get("ai_score", 0), reverse=True)
    logger.info("Aprobadas por IA (score>=%d%%): %d", MIN_SCORE_THRESHOLD, len(scored))

    if not scored:
        logger.info("Ninguna oferta aprobada por IA. Fin.")
        return

    # Formatear display_title
    for o in scored:
        o["display_title"] = f"[{o.get('location','?')}] {o.get('title','')} @ {o.get('company','N/A')}"

    now_str   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    dry_run   = os.environ.get("DRY_RUN") == "1" or "--dry-run" in sys.argv

    # ── 5. Alerta inmediata si hay ofertas muy recientes con buen score ─────────
    fresh = [o for o in scored
             if o.get("published_minutes") is not None
             and o["published_minutes"] <= FRESH_THRESHOLD_MINUTES
             and o.get("ai_score", 0) >= FRESH_MIN_SCORE]
    if fresh:
        logger.info("🚨 %d oferta(s) reciente(s) con score>=%d%%. Enviando alerta inmediata.", len(fresh), FRESH_MIN_SCORE)
        alert_html = Template(ALERT_TEMPLATE).render(
            offers=fresh, generated_at=now_str, threshold=FRESH_THRESHOLD_MINUTES)
        alert_subject = f"🚨 {len(fresh)} empleo(s) recién publicado(s) con match alto – {now_str[:10]}"
        if dry_run:
            with open("alert_preview.html", "w", encoding="utf-8") as f:
                f.write(alert_html)
            logger.info("DRY_RUN: Alerta guardada en alert_preview.html")
        else:
            send_email_safe(alert_html, alert_subject)

    # ── 6. Reporte regular ─────────────────────────────────────────────────────
    alto_count = sum(1 for o in scored if o.get("ai_fit_level") == "Alto")
    html = Template(HTML_TEMPLATE).render(offers=scored, generated_at=now_str)
    subject = f"[JobSearch] {len(scored)} ofertas · {alto_count} Match Alto – {now_str[:10]}"

    if dry_run:
        filename = f"report_preview_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("DRY_RUN: Reporte guardado en %s", filename)
    else:
        send_email_safe(html, subject)
        save_seen(seen_keys)


def send_email_safe(html_body: str, subject: str):
    sender   = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    receiver = os.environ.get("EMAIL_RECEIVER")
    if not all([sender, password, receiver]):
        logger.error("Credenciales de email faltantes.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = receiver
    msg.attach(MIMEText(html_body, "html"))
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as srv:
            srv.login(sender, password)
            srv.sendmail(sender, receiver, msg.as_string())
        logger.info("Email enviado: %s", subject[:60])
    except Exception as e:
        logger.error("Error enviando email: %s", e)


if __name__ == "__main__":
    main()
