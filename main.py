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

from groq_scorer import build_groq_client, load_cv_profile, score_job_with_ai

MAX_SCORE_THRESHOLD = 95 # No usado realmente para filtro
MIN_SCORE_THRESHOLD = 20 # Bajamos el umbral para que pase casi todo a revisión manual

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN ---
SITES = [
    "lever.co", "greenhouse.io", "airavirtual.com", "getonboard.com",
    "firstjob.me", "chiletrabajos.cl", "laborum.cl", "computrabajo.cl",
    "infojobs.net", "tecnoempleo.com", "trabajando.com", "weworkremotely.com",
    "remote.co", "startup.jobs", "nodesk.co", "remotive.com", "indeed.cl", "indeed.com"
]

# Alerta inmediata: ofertas publicadas hace menos de X minutos con score >= Y
FRESH_THRESHOLD_MINUTES = 90
FRESH_MIN_SCORE = 65
MAX_WORKERS = 8  # Hilos paralelos para scraping
REQ_TIMEOUT = 10

import random

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
]

def get_headers():
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://www.google.com/"
    }
    return h

HEADERS = get_headers()

# Áreas de TI (Whitelist 1)
WHITELIST_TI = [
    "Informática", "TI", "IT", "Sistemas", "Software", "Programador", "Desarrollador",
    "Developer", "Computación", "Soporte", "Redes", "Ciberseguridad", "Datos", "Data",
    "Cloud", "Infraestructura", "QA", "Testing", "Informático", "Telecomunicaciones",
    "Web", "Frontend", "Backend", "Fullstack", "Java", "Python", "Javascript", "React",
    "Node", "SQL", "Base de datos", "Ingeniero", "Ingeniería", "Tecnología",
    "Software", "Informática", "Sistemas", "Python", "IT", "Desarrollador", "Developer",
    "Programador", "Data", "IA", "AI", "Backend", "Frontend", "Fullstack", "Web",
    "Soporte", "TI", "Informático", "Computación", "Redes", "Infraestructura", "Cloud",
    "QA", "Testing", "Automatización", "Analista", "Seguridad", "Ciberseguridad",
    "Machine Learning", "DevOps", "Base de Datos", "DBA", "Mobile", "Android", "iOS",
    "Java", "React", "Node", "Angular", "Vue", "PHP", "SQL", "Mantenimiento", "Digital"
]

# Niveles Junior/Trainee (Whitelist 2) — SOLO roles de entrada EXPLÍCITOS
WHITELIST_LEVEL = [
    "Junior", "Trainee", "Práctica", "Practicante", "Entry Level", "Sin experiencia",
    "Egresado", "Recién graduado", "Nivel inicial", "Jr",
    "Pasante", "Becario", "Aprendiz", "Intern", "Graduate",
    # "Analista" / "Asistente" / "Técnico" / "Soporte" sueltos son demasiado amplios,
    # se mantienen SOLO si el título también contiene una keyword TI.
]

# Niveles que SÍ pasan si el título ya tiene una keyword TI fuerte
WHITELIST_LEVEL_WITH_TI = [
    "Analista", "Analyst", "Asistente", "Auxiliar", "Soporte", "Técnico"
]

# Blacklist: Filtros para descartar Senior o áreas ajenas
BLACKLIST = [
    "Senior", "Sr.", "Lead", "Principal", "Jefe", "Director", "Manager", "Expert",
    "Experto", "Especialista", "Arquitecto de", "Tech Lead", "Staff", "Head of",
    "Civil", "Viales", "Construcción", "Hidráulica", "Minas", "Minería", "BIM",
    "Arquitecto", "Comercial", "Ventas", "Psicólogo", "Contador", "Social", "Veterinario",
    "Médico", "Enfermero", "Abogado", "Recursos Humanos", "RRHH",
    # Categorías claramente NO-TI que generaban falsos positivos
    "Bodega", "Lavador", "Imprenta", "Cotizador", "Electricista", "Mecánico",
    "Secretaria", "Recepcionista", "Cajero", "Operario", "Cocinero", "Mesero",
    "Licitaciones", "Contabilidad", "Deposito", "Logística", "Operaciones generales",
]

# Patrones regex para detectar requisitos de experiencia excesiva en descripciones
EXP_BLACKLIST_PATTERNS = [
    r'\b([2-9]|1[0-9])\+?\s*(?:años?|years?|yrs?|a[ñn]os?)\b', # 2+ años, 3 años, etc.
    r'(?:experiencia|experience|exp)\s*(?:de\s*|m[ií]nima?\s*de\s*)?\+?\s*([2-9]|1[0-9])\b', # exp min 2
    r'\b([2-9]|1[0-9])\s*(?:years?|yrs?|años?)\s*(?:of\s*)?exp\b',
    r'\b(2|3|4|5|two|three|four|five)\s*(?:years?|años?)\s*(?:required|experience)\b',
    r'al\s*menos\s*([2-9]|1[0-9])\s*años?\b',
]

# Patrones regex para descartar ofertas que exijan inglés avanzado/bilingue
ENGLISH_REQ_PATTERNS = [
    r'ingl[eé]s\s*(?:avanzado|fluido|bilingue|bil[ií]ng[uü]e|nativo|c1|c2|nivel\s*alto)',
    r'english\s*(?:advanced|fluent|bilingual|native|proficiency|c1|c2|required|mandatory)',
    r'bilingual\s*(?:spanish|español)',
    r'fluent\s+in\s+english',
    r'english\s+(?:is\s+)?(?:required|mandatory|a\s+must)',
    r'must\s+(?:be\s+)?(?:fluent|proficient)\s+in\s+english',
    r'\bbilingue\b',
    r'\bbilinguals?\b',
]

# Palabras clave para detección de idioma (Heurística simple)
SPANISH_STOPWORDS = {
    'de', 'la', 'el', 'el', 'en', 'y', 'a', 'que', 'los', 'se', 'del', 'las', 'un', 'con', 'no', 'una', 
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
    Heurística relajada: acepta si hay ALGUNA palabra española,
    o si no hay evidencia clara de inglés (títulos técnicos suelen mezclar).
    """
    words = re.findall(r'\b\w+\b', text.lower())
    if not words:
        return True  # sin texto = no descartar

    es_count = sum(1 for w in words if w in SPANISH_STOPWORDS)
    en_count = sum(1 for w in words if w in ENGLISH_STOPWORDS)

    # Aceptar si hay alguna palabra española, o si no hay mayoría clara de inglés
    if es_count > 0:
        return True
    if en_count == 0:
        return True  # título técnico puro (ej: "Junior Python Developer") → aceptar
    return False

# Máximo días de antigüedad permitidos para considerar una oferta (filtro de frescura)
MAX_OFFER_AGE_DAYS = 7
MAX_OFFER_AGE_MINUTES = MAX_OFFER_AGE_DAYS * 24 * 60  # 10080 minutos

def is_relevant(title: str, desc: str = "", location: str = "Chile") -> bool:
    """
    Verifica si el puesto es relevante:
    1. No debe tener palabras de la blacklist en el título.
    2. Debe tener al menos una keyword de TI (en título principalmente).
    3. Debe tener al menos una keyword de nivel inicial en título.
       (Analista/Técnico/Soporte se aceptan SOLO si hay TI en el título).
    4. Si NO es en Chile NI en ubicación remota, debe mencionar 'remote'/'remoto'.
    5. No debe requerir 2+ años de experiencia.
    6. No debe exigir inglés avanzado/bilingue.
    """
    title_lower = title.lower()
    desc_lower  = desc.lower()
    loc_lower   = location.lower()
    combined    = f"{title_lower} {desc_lower}"

    # 1. Filtro Blacklist en título (detectar categorías no TI / Senior)
    for black in BLACKLIST:
        if black.lower() in title_lower:
            logger.info("❌ Blacklist '%s': %s", black, title)
            return False

    # 2. Verificar si el TÍTULO tiene keyword TI (requerido para casi todos)
    has_ti_title = any(ti.lower() in title_lower for ti in WHITELIST_TI)
    has_ti_desc  = any(ti.lower() in desc_lower  for ti in WHITELIST_TI)

    if not has_ti_title and not has_ti_desc:
        logger.info("❌ Sin TI en título ni desc: %s", title)
        return False

    # 3. Verificar nivel (Junior/Trainee explícito en el título)
    has_level_explicit = any(level.lower() in title_lower for level in WHITELIST_LEVEL)
    
    # Nivel secundario: Analista/Técnico/Soporte — solo válidos si el título TAMBIÉN tiene TI
    has_level_secondary = has_ti_title and any(
        level.lower() in title_lower for level in WHITELIST_LEVEL_WITH_TI
    )
    
    has_level = has_level_explicit or has_level_secondary
    
    # Último recurso: buscar nivel en descripción (solo si no se encontró en título)
    if not has_level and desc_lower:
        has_level = any(level.lower() in desc_lower for level in WHITELIST_LEVEL)
    
    if not has_level:
        logger.info("❌ Sin nivel junior en título: %s", title)
        return False

    # 4. Si no es Chile, DEBE ser remoto
    if loc_lower not in ("chile", "remoto", "remoto latam", "remoto wwr"):
        loc_is_remote = any(kw in loc_lower for kw in ["remoto", "remote", "latam", "mundial", "internacional", "anywhere"])
        if not loc_is_remote:
            remote_kw = ["remote", "remoto", "teletrabajo", "home office", "anywhere", "latam", "mundial", "distancia"]
            if not any(kw in title_lower or kw in desc_lower for kw in remote_kw):
                logger.info("❌ No remoto, no Chile: %s | Loc: %s", title, location)
                return False

    # 5. Filtro de experiencia excesiva (2+ años)
    for pat in EXP_BLACKLIST_PATTERNS:
        if re.search(pat, combined, re.IGNORECASE):
            logger.info("❌ Experiencia excesiva: %s", title)
            return False

    # 6. Filtro de inglés avanzado/bilingue requerido
    for pat in ENGLISH_REQ_PATTERNS:
        if re.search(pat, combined, re.IGNORECASE):
            logger.info("❌ Inglés avanzado requerido: %s", title)
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
    # User-agent más realista para evitar bloqueos básicos
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3",
    }
    title = url.split("/")[-1].replace("-", " ").replace("_", " ")
    desc = "Sin descripción disponible."
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            if soup.title:
                title = soup.title.string.strip()
            # Intentar capturar descripción de meta tags comunes
            meta_desc = (soup.find("meta", attrs={"name": "description"}) or 
                         soup.find("meta", attrs={"property": "og:description"}))
            if meta_desc and meta_desc.get("content"):
                desc = meta_desc["content"][:400].strip()
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

    candidates = []
    for card in job_cards:
        try:
            title_tag   = card.find("h3", class_="base-search-card__title")
            company_tag = card.find("h4", class_="base-search-card__subtitle")
            link_tag    = card.find("a",  class_="base-card__full-link")
            if title_tag and link_tag:
                title   = title_tag.get_text(strip=True)
                company = company_tag.get_text(strip=True) if company_tag else "N/A"
                link    = link_tag["href"].split("?")[0]
                # Filtro rápido de título antes de pedir la descripción
                if is_relevant(title, location=location):
                    candidates.append({"url": link, "title": title, "company": company})
        except Exception:
            continue

    # Intenta obtener descripción real de LinkedIn para aplicar filtro de experiencia
    offers = []
    for c in candidates:
        desc = _fetch_linkedin_desc(c["url"])
        if is_relevant(c["title"], desc, location):
            offers.append({
                "url":      c["url"],
                "title":    c["title"],
                "company":  c["company"],
                "location": location,
                "desc":     desc if desc else f"Oferta en {location} · LinkedIn.",
            })
    return offers


def _fetch_linkedin_desc(url: str) -> str:
    """
    Intenta obtener el texto de la descripción de una oferta de LinkedIn.
    Retorna string vacío si falla (LinkedIn bloquea frecuentemente).
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Selector principal de descripción en LinkedIn público
            div = soup.find("div", class_="show-more-less-html__markup")
            if div:
                return div.get_text(separator=" ", strip=True)[:600]
            # Fallback: meta description
            meta = soup.find("meta", attrs={"name": "description"})
            if meta and meta.get("content"):
                return meta["content"][:600]
    except Exception:
        pass
    return ""


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
    # Buscamos en múltiples categorías para no perder nada de TI
    categories = ["programming", "data-science-analytics", "mobile-development", "sysadmin-devops-qa"]
    for cat in categories:
        for page in range(1, 2):
            try:
                url = (f"https://www.getonbrd.com/api/v0/categories/{cat}/jobs"
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
                logger.warning("GetOnBoard API error (cat %s, page %d): %s", cat, page, e)
    return offers


def fetch_firstjob_jobs() -> List[Dict]:
    """FirstJob.me: Portal líder para Juniors y Prácticas en Chile."""
    offers = []
    try:
        url = "https://firstjob.me/ofertas-de-trabajo?keywords=junior&category=tecnologia"
        resp = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
        soup = BeautifulSoup(resp.content, "html.parser")
        # El subagent identificó a[href^="/oferta/"] y h6 interno
        cards = soup.select('a[href^="/oferta/"]')
        for card in cards[:20]:
            title_tag = card.find("h6")
            if title_tag:
                title = title_tag.get_text(strip=True)
                jurl  = card["href"]
                if not jurl.startswith("http"):
                    jurl = "https://firstjob.me" + jurl
                if is_relevant(title, "", "Chile"):
                    offers.append({
                        "url": jurl, "title": title, "company": "FirstJob",
                        "location": "Chile", "desc": "Oferta en FirstJob.me", "published_minutes": None
                    })
    except Exception as e:
        logger.warning("FirstJob error: %s", e)
    return offers

def fetch_chiletrabajos_jobs() -> List[Dict]:
    """Chiletrabajos.cl: Intento vía RSS y Búsqueda directa."""
    offers = []
    # Primero intentamos RSS que es más estable para bots
    try:
        rss_url = "https://www.chiletrabajos.cl/rss/ofertas"
        resp = requests.get(rss_url, headers=HEADERS, timeout=REQ_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "xml")
            items = soup.find_all("item")
            for item in items[:30]:
                title = item.title.text if item.title else ""
                jurl  = item.link.text if item.link else ""
                if is_relevant(title, "", "Chile"):
                    offers.append({
                        "url": jurl, "title": title, "company": "Chiletrabajos (RSS)",
                        "location": "Chile", "desc": "Oferta vía RSS", "published_minutes": None
                    })
    except Exception as e:
        logger.warning("Chiletrabajos RSS error: %s", e)
    
    # Si RSS no dio nada, intentamos búsqueda directa
    if not offers:
        try:
            url = "https://www.chiletrabajos.cl/busqueda?q=informatica+junior"
            resp = requests.get(url, headers=get_headers(), timeout=REQ_TIMEOUT)
            logger.info("Chiletrabajos Directo Status: %d", resp.status_code)
            soup = BeautifulSoup(resp.content, "html.parser")
            items = soup.select("a.font-weight-bold") or soup.select(".job-item a")
            for item in items[:15]:
                title = item.get_text(strip=True)
                jurl  = item["href"]
                if not jurl.startswith("http"): jurl = "https://www.chiletrabajos.cl" + jurl
                if is_relevant(title, "", "Chile"):
                    offers.append({"url": jurl, "title": title, "company": "Chiletrabajos", "location": "Chile", "desc": "Búsqueda directa", "published_minutes": None})
        except Exception: pass
    return offers

def fetch_computrabajo_jobs() -> List[Dict]:
    """Computrabajo Chile: Intento de scraper directo."""
    offers = []
    try:
        url = "https://cl.computrabajo.com/ofertas-de-trabajo/?q=junior+informatica"
        # Computrabajo es muy estricto, usamos headers pesados
        resp = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            items = soup.select("article a.js-o-link")
            for item in items[:15]:
                title = item.get_text(strip=True)
                jurl  = "https://cl.computrabajo.com" + item["href"]
                if is_relevant(title, "", "Chile"):
                    offers.append({
                        "url": jurl, "title": title, "company": "Computrabajo",
                        "location": "Chile", "desc": "Oferta en Computrabajo.cl", "published_minutes": None
                    })
    except Exception as e:
        logger.warning("Computrabajo error: %s", e)
    return offers

def fetch_indeed_jobs() -> List[Dict]:
    """Indeed Chile: Scraper directo usando selectores de 2025."""
    offers = []
    try:
        url = "https://cl.indeed.com/jobs?q=junior+informatica&l=Chile&sort=date"
        resp = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
        # Si Indeed nos bloquea (403), lo intentamos por Google (ya está en Google-Chile)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            # Selectores identificados por el subagent
            cards = soup.select(".job_seen_beacon")
            for card in cards[:10]:
                title_tag = card.select_one("h2.jobTitle")
                link_tag  = card.select_one("a.jcs-JobTitle")
                if title_tag and link_tag:
                    title = title_tag.get_text(strip=True)
                    jurl  = "https://cl.indeed.com" + link_tag["href"]
                    if is_relevant(title, "", "Chile"):
                        offers.append({
                            "url": jurl, "title": title, "company": "Indeed",
                            "location": "Chile", "desc": "Oferta en Indeed Chile", "published_minutes": None
                        })
    except Exception as e:
        logger.warning("Indeed error: %s", e)
    return offers

def fetch_wwr_jobs() -> List[Dict]:
    """WeWorkRemotely: Una de las fuentes más grandes de remoto real."""
    offers = []
    try:
        # Buscamos en la categoría de Programación
        url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
        resp = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT)
        soup = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")
        for item in items[:15]:
            title = item.title.text
            jurl  = item.link.text
            # WWR suele poner "Company: Title" o similar
            company = "N/A"
            if " at " in title:
                title, company = title.split(" at ", 1)
            elif ":" in title:
                company, title = title.split(":", 1)
            
            desc = item.description.text[:300]
            if is_relevant(title, desc, "Remoto"):
                offers.append({
                    "url": jurl, "title": title.strip(), "company": company.strip(),
                    "location": "Remoto WWR", "desc": desc, "published_minutes": None
                })
    except Exception as e:
        logger.warning("WWR error: %s", e)
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
    # Agrupamos keywords para hacer menos peticiones y evitar bloqueos de LinkedIn
    combos_chile = [
        ["Junior", "Software", "Chile"],
        ["Trainee", "Informática", "Chile"],
        ["Asistente", "TI", "Chile"],
        ["Práctica", "Programador", "Chile"],
        ["Analista", "Sistemas", "Chile"],
        ["IA", "Junior", "Chile"]
    ]
    combos_remote = [
        ["Junior", "Remote", "Developer"],
        ["Trainee", "Remoto", "Sistemas"],
        ["Python", "Junior", "Remote"],
        ["AI", "Junior", "Remote"]
    ]
    regiones = ["Latin America", "Spain", "Mexico"] # Reducido para evitar bloqueos

    tasks: List[tuple] = []
    # LinkedIn Chile — ventana de 24h
    for c in combos_chile:
        tasks.append(("LI-Chile",  fetch_linkedin_jobs, (c, "Chile", 6, False)))
    # LinkedIn Remoto — ventana de 24h
    for loc in regiones:
        for c in combos_remote:
            tasks.append(("LI-Remoto", fetch_linkedin_jobs, (c, loc, 6, True)))
    
    # Fuentes alternativas y Portales Directos de Chile
    tasks.append(("FirstJob",       fetch_firstjob_jobs,      ()))
    tasks.append(("Chiletrabajos",  fetch_chiletrabajos_jobs, ()))
    tasks.append(("Computrabajo",   fetch_computrabajo_jobs,  ()))
    tasks.append(("Indeed",         fetch_indeed_jobs,        ()))
    tasks.append(("RemoteOK",       fetch_remoteok_jobs,      ()))
    tasks.append(("GetOnBoard",     fetch_getonboard_jobs,    ()))
    tasks.append(("Torre.ai",       fetch_torre_jobs,         ()))
    tasks.append(("WWR",            fetch_wwr_jobs,           ()))
    
    # Google general Chile
    tasks.append(("Google-Chile",  _fetch_google_ats,       ()))
    
    # DuckDuckGo como túnel de emergencia para Chiletrabajos e Indeed
    tasks.append(("DDG-Chile",     _fetch_via_duckduckgo,   ()))

    all_offers: List[Dict] = []
    logger.info("Lanzando %d tareas de scraping en paralelo...", len(tasks))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fmap = {ex.submit(fn, *args): name for name, fn, args in tasks}
        for fut in as_completed(fmap):
            name = fmap[fut]
            try:
                results = fut.result()
                count = len(results) if results else 0
                logger.info("  ✓ %-12s: %d ofertas encontradas", name, count)
                if results:
                    all_offers.extend(results)
            except Exception as e:
                logger.error("  ✗ %-12s: Error: %s", name, e)
    
    return all_offers


def _fetch_google_indeed() -> List[Dict]:
    """Búsqueda Google dedicada exclusivamente para Indeed Chile."""
    if gsearch is None: return []
    offers = []
    q = "site:cl.indeed.com (Junior OR Trainee) Informática Python"
    try:
        results = list(gsearch(q, num_results=10, lang="es"))
        for url in results:
            meta = fetch_metadata(url)
            if is_relevant(meta["title"], meta["desc"], "Chile"):
                offers.append({**meta, "company": "Indeed", "location": "Chile", "published_minutes": None})
    except Exception: pass
    return offers

def _fetch_via_duckduckgo() -> List[Dict]:
    """Usa DuckDuckGo Lite para encontrar ofertas sin ser bloqueado."""
    offers = []
    queries = [
        "site:chiletrabajos.cl junior informatica",
        "site:cl.indeed.com junior informatica"
    ]
    for q in queries:
        try:
            url = f"https://duckduckgo.com/html/?q={q.replace(' ', '+')}"
            resp = requests.get(url, headers=get_headers(), timeout=REQ_TIMEOUT)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, "html.parser")
                # En DDG Lite los resultados son enlaces en 'result__a'
                links = soup.select(".result__a")
                for link in links[:10]:
                    title = link.get_text(strip=True)
                    jurl  = link["href"]
                    # Limpieza de redirección de DDG si fuera necesario
                    if "duckduckgo.com/l/?uddg=" in jurl:
                        import urllib.parse
                        parsed = urllib.parse.urlparse(jurl)
                        jurl = urllib.parse.parse_qs(parsed.query).get('uddg', [jurl])[0]
                    
                    if is_relevant(title, "", "Chile"):
                        offers.append({"url": jurl, "title": title, "company": "Portal Chile", "location": "Chile", "desc": "Encontrado vía DDG", "published_minutes": None})
        except Exception as e:
            logger.warning("DDG error: %s", e)
    return offers

def _fetch_google_ats() -> List[Dict]:
    """Búsqueda Google de emergencia con un solo barrido amplio."""
    if gsearch is None:
        return []
    offers = []
    seen_urls: set = set()
    
    # Una sola consulta potente para no activar el baneo de Google
    q = "site:computrabajo.cl OR site:laborum.cl OR site:indeed.cl Junior Informática Python"

    try:
        logger.info(">>> Google Search Emergencia: %s", q)
        results = list(gsearch(q, num_results=15, lang="es"))
        for url in results:
            if url and url not in seen_urls:
                seen_urls.add(url)
                meta = fetch_metadata(url)
                if is_relevant(meta["title"], meta["desc"], "Chile"):
                    offers.append({**meta, "company": "Portal Empleo", "location": "Chile", "published_minutes": None})
    except Exception as e:
        logger.warning("Google Emergencia error: %s", e)
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

def save_seen(keys):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for k in sorted(keys):
            f.write(k + "\n")

def normalize_url(url: str) -> str:
    """
    Normaliza una URL eliminando parámetros de tracking y fragmentos (#lc=, ?trk=, etc.).
    Así la misma oferta en Computrabajo con distinto #lc= se reconoce como duplicado.
    """
    # Eliminar fragment (#lc=..., #utm=..., etc.)
    url = url.split("#")[0]
    # Eliminar query strings de tracking comunes (trk, ref, source, etc.)
    # pero conservar parámetros funcionales de identificación si los hay
    tracking_params = {"trk", "ref", "source", "refId", "trackingId", "origin",
                       "utm_source", "utm_medium", "utm_campaign", "refcode"}
    try:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url)
        qs = {k: v for k, v in parse_qs(parsed.query).items() if k not in tracking_params}
        clean_query = urlencode(qs, doseq=True)
        url = urlunparse(parsed._replace(query=clean_query))
    except Exception:
        pass
    return url.rstrip("/")


def generate_key(offer):
    """
    Genera una clave de deduplicación robusta basada en título normalizado + empresa.
    Insensible a: orden de palabras, mayúsculas, acentos, puntuación y stopwords.
    """
    title = offer.get("title", "").lower()
    company = offer.get("company", "n/a").lower()
    
    # Normalizar empresa: remover ruidos legales y regionales
    company = re.sub(r'\b(spa|s\.a\.|s\.a|sa|limitada|ltda|ltd|chile|latam|cl|inc|corp|llc)\b', '', company)
    clean_company = re.sub(r'[^a-z0-9]', '', company).strip()
    if not clean_company or clean_company in ("na", "n"):
        clean_company = "na"
    
    # Normalizar título: quitar acentos, puntuación, tokenizar y ORDENAR
    import unicodedata
    title = unicodedata.normalize('NFKD', title)
    title = ''.join(c for c in title if not unicodedata.combining(c))  # quitar acentos
    title = re.sub(r'[^a-z0-9 ]', ' ', title)
    
    # Stopwords de título para evitar que variaciones sin sentido sean diferentes
    stopwords = {'de', 'en', 'para', 'con', 'el', 'la', 'los', 'las', 'y', 'a',
                 'o', 'del', 'al', 'por', 'un', 'una', 'es', 'se', 'su', 'que',
                 'this', 'the', 'and', 'for', 'at', 'in', 'of', 'to', 'a', 'an'}
    tokens = title.split()
    tokens = [t for t in tokens
              if (len(t) > 2 or t in {"ti", "it", "ia", "ai", "jr", "qa", "bi"})
              and t not in stopwords]
    tokens.sort()  # orden insensible a variaciones de redacción
    clean_title = "".join(tokens)
    
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




def main():
    logger.info("=== Iniciando búsqueda TI (paralela, %d fuentes) ===", MAX_WORKERS)

    # ── 1. Scraping paralelo de todas las fuentes ──────────────────────────────
    # Diagnóstico IA inicial
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        logger.error("!!! ERROR CRÍTICO: GROQ_API_KEY no configurada en GitHub Secrets !!!")
    
    all_raw = fetch_all_offers()
    
    # Limitar LinkedIn para dar espacio a otros portales
    li_offers = [o for o in all_raw if "linkedin" in o.get("url", "").lower()]
    other_offers = [o for o in all_raw if "linkedin" not in o.get("url", "").lower()]
    
    logger.info("LinkedIn encontró %d, Otros portales: %d", len(li_offers), len(other_offers))
    # Nos quedamos solo con las 10 mejores/primeras de LinkedIn
    all_raw = other_offers + li_offers[:10]
    
    now_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dry_run   = os.environ.get("DRY_RUN") == "1" or "--dry-run" in sys.argv

    logger.info("Total bruto: %d ofertas", len(all_raw))

    # ── 2. Filtro de antigüedad + Deduplicar contra historial y entre sí ────────
    seen_history = load_seen()
    new_offers: List[Dict] = []
    
    # Sets temporales para deduplicar dentro de la misma ejecución
    current_run_keys = set()
    current_run_urls = set()
    skipped_old = 0

    for o in all_raw:
        # Filtro de antigüedad: descartar ofertas publicadas hace más de MAX_OFFER_AGE_DAYS días
        pub_mins = o.get("published_minutes")
        if pub_mins is not None and pub_mins > MAX_OFFER_AGE_MINUTES:
            skipped_old += 1
            logger.info("⏰ Descartada por antigua (%d días): %s",
                        pub_mins // 1440, o.get('title', '?'))
            continue

        url = normalize_url(o.get("url", ""))  # Normalización robusta de URL
        key = generate_key(o)
        
        # Si ya se vio en ejecuciones pasadas, ignorar
        if url in seen_history or key in seen_history:
            continue
            
        # Si ya apareció en esta misma ejecución (por otro portal), ignorar
        if key in current_run_keys or url in current_run_urls:
            continue
            
        current_run_keys.add(key)
        if url:
            current_run_urls.add(url)
        new_offers.append(o)
        
    logger.info("Nuevas tras deduplicar: %d (descartadas por antiguas: %d)",
                len(new_offers), skipped_old)
    if not new_offers:
        logger.warning(">>> No hay ofertas nuevas después de deduplicar.")
        status_subject = f"[JobSearch] Estado: 0 ofertas nuevas – {now_str[:10]}"
        status_body = f"<h2>Estado de la búsqueda</h2><p>El script se ejecutó correctamente pero no encontró ofertas nuevas en los portales rastreados.</p><p>Esto puede ser porque no hay vacantes Junior hoy o porque los portales están bloqueando el rastreo.</p><p>Generado a las: {now_str}</p>"
        if not dry_run:
            send_email_safe(status_body, status_subject)
        # Aquí sí retornamos porque no hay nada nuevo que guardar
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
        logger.warning(">>> Ninguna oferta aprobada por IA (todas bajo score %d).", MIN_SCORE_THRESHOLD)
        status_subject = f"[JobSearch] Estado: {len(new_offers)} encontradas, 0 aprobadas – {now_str[:10]}"
        status_body = f"<h2>Estado de la búsqueda</h2><p>Se encontraron {len(new_offers)} ofertas potenciales, pero ninguna superó el filtro de calidad de la IA.</p><p>Generado a las: {now_str}</p>"
        if not dry_run:
            send_email_safe(status_body, status_subject)
        # NO RETORNAMOS, para que al final se guarde el historial de lo que ya procesamos

    # Formatear display_title
    for o in scored:
        o["display_title"] = f"[{o.get('location','?')}] {o.get('title','')} @ {o.get('company','N/A')}"

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
        if not dry_run:
            send_email_safe(alert_html, alert_subject)

    if scored:
        alto_count = sum(1 for o in scored if o.get("ai_fit_level") == "Alto")
        html = Template(HTML_TEMPLATE).render(offers=scored, generated_at=now_str)
        subject = f"[JobSearch] {len(scored)} ofertas · {alto_count} Match Alto – {now_str[:10]}"

        if not dry_run:
            logger.info("Intentando enviar correo final con %d ofertas...", len(scored))
            send_email_safe(html, subject)
        
    # ── 7. Guardar historial SIEMPRE (incluso si no pasaron el score) ──────────
    # Así no volvemos a procesar ni a ver lo que ya descartamos o procesamos hoy
    for o in new_offers:
        norm_url = normalize_url(o.get("url", ""))
        if norm_url:
            seen_history.add(norm_url)
        seen_history.add(generate_key(o))
    
    # Limpiar claves antiguas de URLs con parámetros (solo en historial previo)
    # Mantener máximo 2000 entradas para no crecer indefinidamente
    if len(seen_history) > 2000:
        # Conservar las más recientes (aproximación: mantener las alfabéticamente últimas)
        seen_history = set(sorted(seen_history)[-2000:])
        logger.info("Historial podado a 2000 entradas.")
    
    save_seen(seen_history)
    logger.info("Historial actualizado (%d nuevas llaves/urls) y guardado.", len(new_offers) * 2)


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
