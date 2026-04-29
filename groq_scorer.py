# -*- coding: utf-8 -*-
"""
groq_scorer.py
Módulo de evaluación de compatibilidad entre ofertas de trabajo y el CV del candidato,
usando la API de Groq (modelo llama-3.3-70b-versatile).
"""

import os
import json
import logging
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

CV_PROFILE_FILE = "cv_profile.txt"
GROQ_MODEL = "llama-3.3-70b-versatile"
MIN_SCORE_THRESHOLD = 30  # Descartar ofertas con score inferior a este valor


def load_cv_profile() -> str:
    """Carga el perfil del CV desde cv_profile.txt."""
    if not os.path.exists(CV_PROFILE_FILE):
        logger.warning("No se encontró cv_profile.txt. La evaluación IA no estará disponible.")
        return ""
    with open(CV_PROFILE_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def score_job_with_ai(offer: Dict[str, str], cv_profile: str, client) -> Dict:
    """
    Evalúa la compatibilidad de una oferta con el CV del candidato usando Groq AI.

    Retorna un dict con:
      - score (int 0-100)
      - reason (str: frase corta en español)
      - fit_level (str: "Alto", "Medio" o "Bajo")
    """
    default = {"score": 50, "reason": "Evaluación no disponible.", "fit_level": "Medio"}

    if not cv_profile or client is None:
        return default

    title = offer.get('title', 'Sin título')
    company = offer.get('company', 'N/A')
    location = offer.get('location', 'N/A')
    desc = offer.get('desc', '')[:300]

    # Prompt conversacional simple para evitar el loop detection de Groq
    prompt = (
        f"Candidato: Ingeniero en Computación e Informática, titulado 2025. "
        f"Skills: Kotlin, Python, Java, SQL, JavaScript, Firebase, Android, Git, Gemini API. "
        f"Experiencia: práctica en automatización con Python+IA, soporte IT, liderazgo de equipos. "
        f"Busca: trabajo Junior/Trainee en Chile o remoto.\n\n"
        f"Oferta: {title} en {company} ({location}). {desc}\n\n"
        f"Evalúa la compatibilidad del candidato con esta oferta. "
        f"Responde en una sola línea con este formato exacto (sin texto extra):\n"
        f"SCORE: <número 0-100> | NIVEL: <Alto/Medio/Bajo> | RAZON: <frase corta en español>"
    )

    try:
        completion = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=80,
        )
        raw = completion.choices[0].message.content.strip()
        logger.debug("Groq raw response: %s", raw)

        return _parse_simple_response(raw)

    except Exception as e:
        err_str = str(e)
        if "looping" in err_str.lower() or "loop" in err_str.lower():
            logger.warning("Groq loop detection en '%s'. Usando score neutro.", title)
        else:
            logger.error("Error llamando a Groq API para '%s': %s", title, e)
        return default


def _parse_simple_response(raw: str) -> Dict:
    """Parsea la respuesta con formato: SCORE: X | NIVEL: Y | RAZON: Z"""
    default = {"score": 50, "reason": "Sin evaluación.", "fit_level": "Medio"}
    try:
        score_match = re.search(r'SCORE:\s*(\d+)', raw, re.IGNORECASE)
        nivel_match = re.search(r'NIVEL:\s*(Alto|Medio|Bajo)', raw, re.IGNORECASE)
        razon_match = re.search(r'RAZON:\s*(.+)', raw, re.IGNORECASE)

        if not score_match:
            # Fallback: intentar parsear JSON si viene en ese formato
            json_match = re.search(r'\{.*?\}', raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                score = max(0, min(100, int(data.get("score", 50))))
                fit_level = data.get("fit_level", "Medio")
                if fit_level not in ("Alto", "Medio", "Bajo"):
                    fit_level = "Alto" if score >= 75 else ("Medio" if score >= 50 else "Bajo")
                return {"score": score, "reason": str(data.get("reason", ""))[:120], "fit_level": fit_level}
            logger.warning("No se pudo parsear respuesta Groq: %s", raw[:80])
            return default

        score = max(0, min(100, int(score_match.group(1))))
        fit_level = nivel_match.group(1).capitalize() if nivel_match else (
            "Alto" if score >= 75 else ("Medio" if score >= 50 else "Bajo")
        )
        reason = razon_match.group(1).strip()[:120] if razon_match else "Sin evaluación."

        return {"score": score, "reason": reason, "fit_level": fit_level}

    except Exception as e:
        logger.warning("Error parseando respuesta Groq: %s | raw: %s", e, raw[:80])
        return default


def build_groq_client():
    """
    Construye y retorna el cliente de Groq.
    Retorna None si la API key no está configurada (degradación elegante).
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        logger.warning("GROQ_API_KEY no encontrada. El scoring IA estará desactivado.")
        return None
    try:
        from groq import Groq
        return Groq(api_key=api_key)
    except ImportError:
        logger.error("Librería 'groq' no instalada. Ejecuta: pip install groq")
        return None
    except Exception as e:
        logger.error("Error inicializando cliente Groq: %s", e)
        return None
