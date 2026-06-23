FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    nginx \
    cron \
    gettext-base \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /root/.ssh /usr/share/nginx/html && chmod 700 /root/.ssh && rm -f /etc/nginx/sites-enabled/default 2>/dev/null; true

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY entrypoint.sh /entrypoint.sh
COPY report.html /usr/share/nginx/html/index.html
COPY index.html /usr/share/nginx/html/index2.html

RUN pip install --no-cache-dir pymysql azure-storage-blob requests

COPY scripts/ /app/scripts/
RUN mkdir -p /app/output

RUN chmod +x /entrypoint.sh

EXPOSE 80

CMD ["/entrypoint.sh"]
