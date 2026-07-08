FROM python:3.11-slim

WORKDIR /app

# 系統套件
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Python 依賴
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 源碼
COPY . /app

# 不 bake .env，啟動時從 /opt/pos-v1.2-secrets/.env 讀
EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]