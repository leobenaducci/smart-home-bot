FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8080
# Single worker keeps the shared httpx client and SSE streams simple; the family
# workload is tiny. Scale with a process manager only if ever needed.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
