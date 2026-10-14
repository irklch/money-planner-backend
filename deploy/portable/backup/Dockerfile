# Тот же образ, что у БД: pg_dump совпадает с сервером, на чистом сервере качается один образ.
# age — шифрование, rclone — копии вне облака, dig — проверка основного сервера.
FROM postgres:16
RUN apt-get update && apt-get install -y --no-install-recommends age rclone bind9-dnsutils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh
ENTRYPOINT ["/usr/local/bin/backup.sh"]
