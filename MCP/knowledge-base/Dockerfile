FROM --platform=linux/arm64 python:3.13-slim

WORKDIR /app

# mcp 2.x removed mcp.server.fastmcp — pin to 1.x for FastMCP + streamable-http.
RUN pip install --upgrade boto3 botocore \
    && pip install "mcp>=1.23.0,<2" \
    && pip install "aws-opentelemetry-distro>=0.10.0"

ENV PYTHONPATH=/app

EXPOSE 8000

COPY . .

CMD ["opentelemetry-instrument", "python", "-m", "mcp_server_retrieve"]
