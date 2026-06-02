FROM python:3.12-slim
WORKDIR /app

COPY dead_mcp/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . .
ENV PYTHONPATH=/app

EXPOSE 8768
CMD ["python", "dead_mcp/server.py"]
