NPAAAD–SAFDZ Map Downloader

Flask-based GIS map downloader using read-only Cloudflare R2 GeoPackage datasets.

Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and optionally R2_BUCKET (default npaaad-safdz-data) in Render.

Render build: pip install -r requirements.txt
Render start: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 300

Routes: /health, /api/inventory, /api/preview, /api/download. Export options: JPEG, PDF, shapefile ZIP. Current export scope: entire province dataset, not municipality clip.
