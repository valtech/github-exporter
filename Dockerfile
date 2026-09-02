FROM ghcr.io/astral-sh/uv:0.12.5-python3.14-alpine@sha256:56b2e7ad659cd4b8d3abb5e67556a1e7ac53adf3a13eb8c292696df9c1e70b67

ENV UV_LINK_MODE=copy
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_DEV=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    touch README.md && uv sync --locked --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

CMD ["uv", "run", "github-exporter"]
