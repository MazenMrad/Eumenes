FROM python:3.11-slim

WORKDIR /app

COPY packages.txt .
RUN apt-get update && apt-get install -y --no-install-recommends $(grep -v '^#' packages.txt) && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x start.sh

EXPOSE 7860

CMD ["/bin/bash", "start.sh"]
