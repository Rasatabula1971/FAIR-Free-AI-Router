FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir . && useradd --create-home fair
USER fair
EXPOSE 8000
CMD ["uvicorn", "apps.api.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
