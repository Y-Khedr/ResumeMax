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

EXPOSE 7860

# Render injects its own $PORT at runtime, overriding this default — so
# changing the default here doesn't affect Render. 7860 specifically
# matches Hugging Face Spaces' expected default container port for
# Docker-SDK Spaces, which don't inject a $PORT the way Render does.
ENV PORT=7860
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
