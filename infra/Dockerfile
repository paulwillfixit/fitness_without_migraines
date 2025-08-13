FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .

# Set timezone in the container
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/* && \
    ln -fs /usr/share/zoneinfo/Australia/Melbourne /etc/localtime && \
    dpkg-reconfigure -f noninteractive tzdata

ENV TZ=Australia/Melbourne

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend


CMD ["uvicorn","backend.app:app","--host","0.0.0.0","--port","8000"]
