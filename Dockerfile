FROM python:3.12-slim

RUN useradd --create-home --uid 1000 bpdl

WORKDIR /build
COPY pyproject.toml .
COPY bpdl/ ./bpdl/
RUN pip install --no-cache-dir .

USER bpdl
WORKDIR /config

EXPOSE 8095
ENTRYPOINT ["bpdl-web"]
