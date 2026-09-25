FROM python:3.12-slim

WORKDIR /app

# Pillow draws the /chat/map labels, and place names are Spanish. The slim image
# ships no fonts at all, and Pillow's built-in default has no accented glyphs —
# "Estación" would come out as "Estaci□n". DejaVu covers Latin-1 properly.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8443

CMD ["python", "app.py"]
