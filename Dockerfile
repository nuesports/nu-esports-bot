FROM ghcr.io/astral-sh/uv:python3.10-alpine
WORKDIR /bot

# Deps first, from just the lockfiles, so this layer is cached and skipped
# unless pyproject.toml/uv.lock actually change (not on every code edit).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . /bot

# Accept build arguments for config fetching
ARG PROD
ARG CONFIG_ACCESS_TOKEN
ARG GGLEAP_URL

# Fetch config.yaml and secrets.yaml from API in production
RUN if [ "$PROD" = "true" ]; then \
        echo "Production build: Fetching config and secrets from API..." && \
        apk add --no-cache curl && \
        curl -f -sS -H "Authorization: ${CONFIG_ACCESS_TOKEN}" \
             "${GGLEAP_URL}/config/yaml" > config.yaml && \
        echo "Config fetched successfully!" && \
        curl -f -sS -H "Authorization: ${CONFIG_ACCESS_TOKEN}" \
             "${GGLEAP_URL}/secrets/yaml" > secrets.yaml && \
        echo "Secrets fetched successfully!" && \
        apk del curl; \
    else \
        echo "Local build: Using local config and secrets files"; \
    fi

# Install dependencies using uv
RUN uv sync --frozen

CMD ["uv", "run", "bot.py"]
