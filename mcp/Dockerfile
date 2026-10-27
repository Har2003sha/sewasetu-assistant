FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py instructions_citizen.txt instructions_officer.txt ./
RUN useradd -r -u 10001 mcp
USER mcp
# one image, two servers: `python server.py citizen` / `python server.py officer`
CMD ["python", "server.py", "citizen"]
