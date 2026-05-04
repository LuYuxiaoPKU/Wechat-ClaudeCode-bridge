FROM python:3.11-slim

# Node.js + Claude Code CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY bridge.py .

# Bridge 数据持久化
VOLUME /root/.wechat-claude-bridge

# 默认不挂载项目目录，由 docker-compose 覆盖
CMD ["python3", "bridge.py"]
