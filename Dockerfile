FROM python:3.12-slim

WORKDIR /app

# 시스템 패키지 (bcrypt 빌드용 등)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt 먼저 복사 → 캐싱으로 빌드 속도 향상
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# SQLite 볼륨 마운트 포인트 미리 생성 (docker-compose.yml 의 sqlite_data:/data)
RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "run.py"]
