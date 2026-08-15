FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
COPY config.json .
ENV PYTHONPATH=/app/src CONFIG_PATH=/app/config.json DB_PATH=/data/hub.db TOKEN_PATH=/data/token.json
EXPOSE 8138
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8138/health')"
CMD ["python", "-m", "uvicorn", "family_hub.app:app", "--host", "0.0.0.0", "--port", "8138"]
