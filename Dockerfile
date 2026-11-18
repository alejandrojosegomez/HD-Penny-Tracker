# Playwright's image ships Chromium for one exact Playwright version.
# Keep this tag and `playwright==` in requirements.txt in sync, or the browser won't launch.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# app code only (.dockerignore keeps .env, data, git history and the brain out of the image)
COPY --chown=pwuser:pwuser . .
RUN mkdir -p /app/data && chown pwuser:pwuser /app/data

# don't run as root; pwuser comes with the Playwright image
USER pwuser

CMD ["python", "-m", "tracker", "run"]
