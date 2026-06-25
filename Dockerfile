FROM python:3.11-slim
COPY report.html /app/index.html
COPY scripts/ /app/scripts/
COPY server.py /app/server.py
RUN pip install --no-cache-dir pymysql azure-storage-blob requests openpyxl
WORKDIR /app
EXPOSE 8080
CMD ["python3", "/app/server.py"]
