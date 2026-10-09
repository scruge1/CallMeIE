FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e
WORKDIR /app
COPY requirements.txt requirements-runtime.txt ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-runtime.txt
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
