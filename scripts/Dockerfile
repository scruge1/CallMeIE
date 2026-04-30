FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
# AUD-038 — billing/ package needs explicit COPY (the *.py glob skips subdirs)
COPY billing ./billing/
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
