FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

COPY report.html /app/report.html
RUN pip install --no-cache-dir pymysql azure-storage-blob requests
COPY scripts/ /app/scripts/
COPY server.py /app/server.py

RUN chmod +x /app/server.py

EXPOSE 8080

CMD /app/server.py
