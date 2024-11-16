ARG BASE=nvidia/cuda:11.8.0-base-ubuntu22.04
FROM ${BASE} AS builder

WORKDIR /app
RUN apt update && apt upgrade -y && apt install -y curl build-essential libsndfile1-dev

# Install rustup
RUN curl https://sh.rustup.rs -sSf | bash -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["tts"]
CMD ["--help"]
