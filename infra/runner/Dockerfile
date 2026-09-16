FROM python:3.14.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/workspace/src

WORKDIR /workspace
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements.txt
COPY src /workspace/src
COPY scripts /workspace/scripts
COPY configs /workspace/configs
COPY schemas /workspace/schemas

USER 65532:65532
