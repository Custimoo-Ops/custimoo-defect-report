#!/bin/bash

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

# Write SSH key from secret
if [ -n "$SSH_PRIVATE_KEY" ]; then
    mkdir -p /root/.ssh
    echo "$SSH_PRIVATE_KEY" > /root/.ssh/id_rsa 2>/dev/null
    chmod 600 /root/.ssh/id_rsa 2>/dev/null
fi

# Write SSH config
cat > /root/.ssh/config << SSHCONF
Host custimoo-db-tunnel
  HostName ${TUNNEL_HOST}
  User ${TUNNEL_USER}
  Port ${TUNNEL_PORT}
  IdentityFile /root/.ssh/id_rsa
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
SSHCONF
chmod 600 /root/.ssh/config

# Start SSH tunnel (non-fatal)
echo "[fly] Starting SSH tunnel..."
ssh -f -N -L 3307:${RDS_HOST}:3306 custimoo-db-tunnel 2>&1 || echo "[fly] SSH tunnel failed, report will use cached data"

# Generate report on startup (non-fatal)
echo "[fly] Generating report..."
cd /app
python3 /app/scripts/html_report.py 2>&1 || echo "[fly] Report generation failed (serving cached version)"
if [ -f /app/report.html ]; then
    cp /app/report.html /usr/share/nginx/html/index.html
    echo "[fly] Report copied to web root"
fi

# Write cron wrapper
cat > /app/cron-regenerate.sh << 'CRONEND'
#!/bin/bash
. /var/run/report-env.sh 2>/dev/null || true
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd /app
if ! ssh -O check custimoo-db-tunnel 2>/dev/null; then
    ssh -f -N -L 3307:${RDS_HOST}:3306 custimoo-db-tunnel 2>&1
fi
python3 /app/scripts/html_report.py >> /var/log/report-cron.log 2>&1
cp /app/report.html /usr/share/nginx/html/index.html >> /var/log/report-cron.log 2>&1
echo "[cron] done $(date)" >> /var/log/report-cron.log
CRONEND
chmod +x /app/cron-regenerate.sh

# Save env for cron
printenv | grep -v SSH_PRIVATE_KEY > /var/run/report-env.sh 2>/dev/null || true
sed -i 's/^/export /' /var/run/report-env.sh 2>/dev/null || true

# Set up cron
SCHED="${REGENERATE_CRON:-0 8,16 * * 1-5}"
echo "${SCHED} root /app/cron-regenerate.sh > /dev/null 2>&1" > /etc/cron.d/report-regeneration
chmod 0644 /etc/cron.d/report-regeneration
service cron start 2>&1 || true

# Serve nginx
echo "[fly] Starting nginx..."
nginx -g "daemon off;"
