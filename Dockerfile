FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global --add safe.directory /app

WORKDIR /app

COPY requirements-freeze.txt .
RUN pip install --no-cache-dir -r requirements-freeze.txt

# self-inspection support: the bot's live-tools call `journalctl -u discordbot`
COPY journalctl /usr/local/bin/journalctl
RUN chmod +x /usr/local/bin/journalctl

# trim the log on start, mirror stdout to /app/bot.log for the shim,
# and exec python as PID 1 so it receives docker stop signals
CMD ["bash", "-c", "[ -f bot.log ] && { tail -n 5000 bot.log > bot.log.tmp && mv bot.log.tmp bot.log; } ; exec > >(tee -a /app/bot.log) 2>&1; exec python main.py"]
