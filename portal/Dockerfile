FROM python:3.12-slim
WORKDIR /app
COPY mmrag_connectors /src/mmrag_connectors
COPY portal/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt /src/mmrag_connectors
COPY portal/app ./app
ENV PORTAL_DATA_DIR=/data
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
