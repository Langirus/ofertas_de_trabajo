# Automatización: Búsqueda diaria de empleo

Este repositorio contiene un script en Python que busca ofertas en varios portales ATS, genera un reporte HTML y lo envía por correo electrónico.

Archivos principales
- `main.py` — script principal (usa `googlesearch-python`, `requests`, `beautifulsoup4`, `Jinja2`).
- `requirements.txt` — dependencias.
- `.github/workflows/job_search.yml` — workflow de GitHub Actions programado diario.

Configuración de Secrets en GitHub
1. Ve al repositorio en GitHub → Settings → Secrets and variables → Actions → New repository secret.
2. Crea los siguientes secrets (valores desde tu cuenta Gmail):
   - `EMAIL_SENDER` — dirección de correo del remitente (p. ej. `tu@gmail.com`).
   - `EMAIL_PASSWORD` — contraseña de aplicación (recomiendo crear un App Password en Gmail, no usar tu contraseña principal).
   - `EMAIL_RECEIVER` — dirección que recibirá el reporte.

Notas de seguridad
- Nunca commitees contraseñas en el código. El workflow mapea los Secrets a variables de entorno.
- Para cuentas Gmail con verificación en dos pasos, crea un App Password y usa ese valor como `EMAIL_PASSWORD`.

Uso local (pruebas)
1. Crea y activa un entorno virtual (Windows PowerShell):

```powershell
python -m venv .venv
& .venv\Scripts\Activate.ps1
```

2. Instala dependencias:

```powershell
pip install -r requirements.txt
```

3. Ejecuta en modo prueba (no envía correo; guarda `report_preview_*.html`):

```powershell
$env:DRY_RUN = '1'
& .venv\Scripts\python.exe main.py
```

4. Para ejecutar en modo real (envía correo): exporta las variables necesarias y ejecuta:

```powershell
$env:EMAIL_SENDER = 'tu@gmail.com'
$env:EMAIL_PASSWORD = 'tu_app_password'
$env:EMAIL_RECEIVER = 'destino@correo.com'
& .venv\Scripts\python.exe main.py
```

GitHub Actions
- El workflow `.github/workflows/job_search.yml` ya incluye un `cron` diario. Ajusta la expresión `cron` si quieres otra hora (la actual es `0 12 * * *` UTC).

Limitaciones y buenas prácticas
- **Filtro de Idioma**: El script incluye un detector heurístico que prioriza ofertas en español y descarta aquellas claramente en otros idiomas.
- `googlesearch-python` depende de resultados públicos de Google; si Google bloquea peticiones, la búsqueda puede fallar.
- No hagas scraping agresivo; el script respeta pausas cortas entre peticiones.
- Revisa el archivo HTML generado antes de abrir enlaces desconocidos.

¿Siguiente paso?
- Puedo crear un commit y push al repositorio remoto si quieres (necesitaré acceso/credenciales), o guiarte para añadir los Secrets en GitHub.
