# -*- coding: utf-8 -*-
import os
import sys
import argparse
import time
import logging
import itertools
from datetime import datetime
from typing import List, Dict

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

# Configuración de logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# --- CONFIGURACIÓN DE TU PERFIL ---
SITES = [
    "lever.co",
    "greenhouse.io",
    "airavirtual.com",
    "getonboard.com",
    "empleospublicos.cl",
]

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

def is_relevant(title: str, desc: str = "") -> bool:
    """
    Verifica si el puesto es relevante:
    1. No debe tener palabras de la blacklist.
    2. Debe tener al menos una palabra de TI.
    3. Debe tener al menos una palabra de nivel (Junior/Trainee/Analista).
    4. Debe estar en español o ser para hispanohablantes.
    """
    title_lower = title.lower()
    
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

    # 4. Filtro de Idioma (Nuevo)
    # Se aplica al título y opcionalmente a la descripción
    content_to_check = f"{title} {desc}"
    if not is_spanish_content(content_to_check):
        logger.info(f"Descartado por idioma: {title}")
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

def fetch_linkedin_jobs(keywords: List[str], location: str = "Chile", hours: int = 24) -> List[Dict[str, str]]:
    """
    LinkedIn guest search scraper.
    """
    tpr = "r3600" if hours <= 1 else f"r{hours * 3600}"
    query = urllib.parse.quote(" ".join(keywords))
    loc = urllib.parse.quote(location)
    
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={query}&location={loc}&f_TPR={tpr}&sortBy=DD&start=0"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    
    logger.info("Scrapeando LinkedIn: keywords=%s, location=%s", keywords, location)
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
                # LinkedIn guest search doesn't usually give description in the landing, 
                # but we can pass the title for the language check.
                if not is_relevant(title):
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

HTML_TEMPLATE = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Reporte de Ofertas TI Junior</title>
  <style>
    body{font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background:#f0f2f5; color:#1c1e21; line-height:1.6;}
    .container{max-width:800px;margin:20px auto;background:#fff;padding:30px;border-radius:12px;box-shadow:0 10px 25px rgba(0,0,0,0.05)}
    h1{color:#003566; margin-bottom:10px; font-size:24px}
    .meta{color:#65676b;font-size:14px;margin-bottom:20px; border-bottom:1px solid #ebedf0; padding-bottom:15px}
    .job{padding:15px;border-radius:10px;border:1px solid #e4e6eb;margin-bottom:15px; transition: 0.2s}
    .job:hover{border-color:#003566; background:#f8f9fa}
    .job a{color:#003566;text-decoration:none;font-weight:bold; font-size:18px}
    .job a:hover{text-decoration:underline}
    .desc{color:#4b4f56;margin-top:8px;font-size:14px}
    .badge{display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:bold; text-transform:uppercase; margin-left:5px}
    .badge-remote{background:#dcfce7; color:#166534}
    footer{color:#bcc0c4;font-size:12px;margin-top:30px; text-align:center}
  </style>
</head>
<body>
  <div class="container">
    <h1>🚀 Reporte TI Junior / Trainee (Español)</h1>
    <div class="meta">Generado: {{ generated_at }} | Ubicaciones: Chile, Latam & España</div>
    {% if offers %}
      {% for o in offers %}
        <div class="job">
          <div>
            <a href="{{ o.url }}" target="_blank" rel="noopener">{{ o.display_title }}</a>
            {% if "Remote" in o.display_title or "Remoto" in o.display_title %}
              <span class="badge badge-remote">Remoto</span>
            {% endif %}
          </div>
          <div class="desc">{{ o.desc }}</div>
        </div>
      {% endfor %}
    {% else %}
      <p>No se encontraron ofertas nuevas que cumplan con los filtros.</p>
    {% endif %}
    <footer>Este reporte se genera periódicamente de forma automática.</footer>
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

def main():
    logger.info("--- Iniciando Búsqueda TI Multisector ---")
    
    # --- PASO 1: Búsqueda en Google (Portales ATS) ---
    queries = []
    for site in SITES:
        for level in ["Junior", "Trainee"]:
            queries.append(f"site:{site} {level} (Informática OR Sistemas OR TI)")

    found_urls = []
    seen = set()
    for q in queries:
        logger.info("Buscando Google: %s", q)
        results = try_search(q, max_results=3)
        for url in results:
            if url and url not in seen:
                seen.add(url)
                found_urls.append(url)
        time.sleep(2)

    offers = []
    for url in found_urls:
        meta = fetch_metadata(url)
        if is_relevant(meta["title"], meta["desc"]):
            # Intentar extraer empresa del título o URL para sitios que no son LinkedIn
            title_parts = meta["title"].split(" at ")
            if len(title_parts) < 2:
                title_parts = meta["title"].split(" @ ")
            
            company = "N/A"
            if len(title_parts) >= 2:
                company = title_parts[-1].strip()
            elif "lever.co" in url:
                company = url.split("lever.co/")[1].split("/")[0]
            
            meta["company"] = company
            meta["location"] = "Mundial"
            offers.append(meta)
        time.sleep(1)

    # --- PASO 2: LinkedIn Chile ---
    combos_chile = [
        ["Junior", "Software"],
        ["Trainee", "Informática"],
        ["Soporte", "TI"],
        ["Analista", "Sistemas"],
        ["Práctica", "Programador"],
        ["Junior", "IT"]
    ]
    for combo in combos_chile:
        offers.extend(fetch_linkedin_jobs(combo, location="Chile", hours=24))
        time.sleep(2)

    # --- PASO 3: LinkedIn Remoto Internacional (Español) ---
    # Buscamos en regiones de habla hispana para asegurar relevancia
    regiones_es = ["Latin America", "Spain", "Mexico", "Argentina", "Colombia"]
    combos_remote = [
        ["Junior", "Remoto", "Software"],
        ["Trainee", "Remoto", "Desarrollador"],
        ["Junior", "Remoto", "Informática"],
        ["Analista", "Sistemas", "Remoto"],
        ["Programador", "Junior"]
    ]
    
    for loc_es in regiones_es:
        for combo in combos_remote:
            logger.info("Scrapeando LinkedIn Remoto: %s en %s", combo, loc_es)
            offers.extend(fetch_linkedin_jobs(combo, location=loc_es, hours=24))
            time.sleep(2)

    seen_keys = load_seen()
    new_offers = []
    
    for o in offers:
        key = generate_key(o)
        if key not in seen_keys:
            new_offers.append(o)
            seen_keys.add(key)
            # También guardamos la URL como clave técnica por si acaso
            seen_keys.add(o["url"])

    logger.info("Total encontradas: %d | Nuevas para enviar: %d", len(offers), len(new_offers))

    if not new_offers:
        logger.info("No hay ofertas nuevas. Fin del proceso.")
        return

    # Formatear títulos para el HTML final
    for o in new_offers:
        if "location" in o and "company" in o:
            o["display_title"] = f"[{o['location']}] {o['title']} @ {o['company']}"
        else:
            o["display_title"] = o["title"]

    html = Template(HTML_TEMPLATE).render(
        offers=new_offers, 
        generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    )
    
    dry_run = os.environ.get("DRY_RUN") == "1" or "--dry-run" in sys.argv
    if dry_run:
        filename = f"report_preview_{datetime.utcnow().strftime('%Y%H%M%SZ')}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("DRY_RUN: Reporte guardado en %s (Nuevas: %d)", filename, len(new_offers))
    else:
        subject = f"[JobSearch] {len(new_offers)} Ofertas Nuevas - {datetime.utcnow().strftime('%Y-%m-%d')}"
        send_email_safe(html, subject)
        save_seen(seen_keys)

def send_email_safe(html_body: str, subject: str):
    sender = os.environ.get("EMAIL_SENDER")
    password = os.environ.get("EMAIL_PASSWORD")
    receiver = os.environ.get("EMAIL_RECEIVER")

    if not all([sender, password, receiver]):
        logger.error("Credenciales de email faltantes. No se envía correo.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver
    msg.attach(MIMEText(html_body, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(sender, password)
            server.sendmail(sender, receiver, msg.as_string())
        logger.info("Email enviado correctamente.")
    except Exception as e:
        logger.error("Error enviando email: %s", e)

if __name__ == "__main__":
    main()
