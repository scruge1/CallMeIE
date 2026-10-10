FROM python:3.11.17-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && pip check
COPY *.py ./
# AUD-038 — billing/ package needs explicit COPY (the *.py glob skips subdirs)
COPY billing ./billing/
# Static templates served by FastAPI (admin dashboard, etc) — *.py glob skips .html
COPY *.html ./
COPY voice-samples ./voice-samples/
COPY personal-agent.js personal-agent.css ./
COPY great-national-demo.js great-national-demo.css ./
# P5-4 — alembic versioned migrations
COPY alembic.ini .
COPY alembic ./alembic/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
