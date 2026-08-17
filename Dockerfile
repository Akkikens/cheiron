# Reproducible run without a local Python, for a reviewer who would rather not install one.
# Built and smoke-tested: /health, the demo page, and a live /analyze from inside the container.
#
#   docker build -t cheiron .
#   docker run --rm -p 8000:8000 -e LLM_ENABLED=false cheiron        # no API key needed
#   docker run --rm -p 8000:8000 -e OPENAI_API_KEY=sk-... cheiron    # with the model
#
# Then open http://localhost:8000 for the demo, or POST to /analyze.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LLM_ENABLED=false

WORKDIR /app

# Dependencies resolve from pyproject alone, so this layer is cached until the deps change.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY demo ./demo
COPY SPEC.md ./
COPY docs ./docs

# Nothing here needs root, and the service only ever makes outbound requests.
RUN useradd --create-home --uid 10001 cheiron
USER cheiron

EXPOSE 8000

# The vocabulary loads at startup from /studies/enums; /health reports it as unavailable rather
# than refusing to boot if ClinicalTrials.gov is unreachable, which is why there is no wait here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
