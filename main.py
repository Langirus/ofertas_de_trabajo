# -*- coding: utf-8 -*-
import os
import sys
import time
import logging
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

from groq_scorer import build_groq_client, load_cv_profile, score_job_with_ai, MIN_SCORE_THRESHOLD

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
        # For Google results, we treat location as "Internacional" to enforce remote rules
        # unless we find "Chile" in the title or URL.
        google_loc = "Internacional"
        if "chile" in meta["title"].lower() or "chile" in url.lower():
            google_loc = "Chile"
            
        if is_relevant(meta["title"], meta["desc"], location=google_loc):
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
            offers.extend(fetch_linkedin_jobs(combo, location=loc_es, hours=24, remote_only=True))
            time.sleep(2)

    seen_keys = load_seen()
    new_offers = []

    for o in offers:
        key = generate_key(o)
        if key not in seen_keys:
            new_offers.append(o)
            seen_keys.add(key)
            seen_keys.add(o["url"])

    logger.info("Total encontradas: %d | Nuevas para enviar: %d", len(offers), len(new_offers))

    if not new_offers:
        logger.info("No hay ofertas nuevas. Fin del proceso.")
        return

    # --- PASO 4: Evaluación IA con Groq ---
    groq_client = build_groq_client()
    cv_profile = load_cv_profile()

    if groq_client and cv_profile:
        logger.info("Evaluando %d ofertas con Groq AI...", len(new_offers))
        for i, o in enumerate(new_offers):
            logger.info("  [%d/%d] Evaluando: %s", i + 1, len(new_offers), o.get("title", "?"))
            result = score_job_with_ai(o, cv_profile, groq_client)
            o["ai_score"] = result["score"]
            o["ai_reason"] = result["reason"]
            o["ai_fit_level"] = result["fit_level"]
            time.sleep(0.5)  # Respetar rate limits de Groq
    else:
        logger.warning("Groq AI no disponible. Asignando score neutro a todas las ofertas.")
        for o in new_offers:
            o["ai_score"] = 50
            o["ai_reason"] = "Evaluación IA no disponible."
            o["ai_fit_level"] = "Medio"

    # Filtrar ofertas con score muy bajo
    scored_offers = [o for o in new_offers if o["ai_score"] >= MIN_SCORE_THRESHOLD]
    discarded = len(new_offers) - len(scored_offers)
    if discarded > 0:
        logger.info("Descartadas por score IA bajo (%d%%): %d ofertas", MIN_SCORE_THRESHOLD, discarded)

    if not scored_offers:
        logger.info("Ninguna oferta supera el umbral de relevancia IA. Fin del proceso.")
        return

    # Ordenar por score descendente
    scored_offers.sort(key=lambda x: x["ai_score"], reverse=True)

    # Formatear display_title
    for o in scored_offers:
        if "location" in o and "company" in o:
            o["display_title"] = f"[{o['location']}] {o['title']} @ {o['company']}"
        else:
            o["display_title"] = o.get("title", "Sin título")

    html = Template(HTML_TEMPLATE).render(
        offers=scored_offers,
        generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    )

    dry_run = os.environ.get("DRY_RUN") == "1" or "--dry-run" in sys.argv
    if dry_run:
        filename = f"report_preview_{datetime.utcnow().strftime('%Y%H%M%SZ')}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("DRY_RUN: Reporte guardado en %s (%d ofertas, score >= %d%%)",
                    filename, len(scored_offers), MIN_SCORE_THRESHOLD)
    else:
        alto_count = sum(1 for o in scored_offers if o["ai_fit_level"] == "Alto")
        subject = f"[JobSearch] {len(scored_offers)} Ofertas · {alto_count} Match Alto — {datetime.utcnow().strftime('%Y-%m-%d')}"
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
