FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
# AUD-038 — billing/ package needs explicit COPY (the *.py glob skips subdirs)
COPY billing ./billing/
# Static templates served by FastAPI (admin dashboard, etc) — *.py glob skips .html
COPY *.html .
COPY voice-samples ./voice-samples/
COPY personal-agent.js personal-agent.css ./
COPY great-national-demo.js great-national-demo.css ./
# P5-4 — alembic versioned migrations
COPY alembic.ini .
COPY alembic ./alembic/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
