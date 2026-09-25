FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04

RUN apt update && apt install -y python3 python3-pip ffmpeg

WORKDIR /app
COPY requirements.txt .

RUN pip3 install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8000

CMD ["python3", "server.py"]
