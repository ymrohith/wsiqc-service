FROM python:3.12-slim

# libopenslide0 is optional but cheap to include: it lets the same image
# handle .svs files, not just pyramidal TIFF.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libopenslide0 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wsiqc/ ./wsiqc/
COPY scripts/ ./scripts/

ENV PYTHONUNBUFFERED=1 \
    WSIQC_SLIDE_DIR=/data \
    WSIQC_OUTPUT_DIR=/out \
    WSIQC_DATABASE_URL=sqlite:////state/wsiqc.db

EXPOSE 8000
CMD ["uvicorn", "wsiqc.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
