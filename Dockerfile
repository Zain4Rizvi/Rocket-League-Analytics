FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Copy the rest of the project (server.py, Scripts/, frontend/, examples/, etc.)
COPY . .

EXPOSE 8000

CMD ["uv", "run", "server.py"]