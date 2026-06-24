FROM python:3.11-slim
COPY report.html /app/index.html
COPY scripts/ /app/scripts/
RUN pip install --no-cache-dir pymysql azure-storage-blob requests
WORKDIR /app
EXPOSE 8080
CMD ["python3", "-m", "http.server", "8080"]
