# ResumeMax backend — serves the frontend statically too, so this is the
# only container needed for the whole app.
FROM python:3.12-slim

# System packages needed for OCR fallback (scanned PDFs) and PDF rendering.
# If you don't need OCR, you can drop tesseract-ocr/poppler-utils to shrink
# the image, but the app will just disable that one feature gracefully.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

EXPOSE 8000

# $PORT is set by most hosting platforms (Render, Railway, etc.) — falls
# back to 8000 for local `docker run`.
ENV PORT=8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
