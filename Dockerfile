FROM python:3.14-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends sane-utils sane-airscan \
    && rm -rf /var/lib/apt/lists/*

# Copy the uv binary from Astral's image (pin a version tag in real use)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies first so this layer is cached until the lockfile changes
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY scan_app.py .
COPY templates/ templates/

RUN useradd --create-home --uid 1000 scanappuser \
    && mkdir /scans \
    && chown scanappuser /scans
USER scanner
ENV SCAN_DIR=/scans
VOLUME /scans
EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "scan_app:app"]