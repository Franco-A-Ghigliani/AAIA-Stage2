# Image for running the target's checks. Build context: the target clone
# (docker build -t coding-harness-cosmic:1 -f docker/Dockerfile targets/cosmicpython).
# Dependencies are installed at build time, so checks run with --network none.
FROM python:3.11-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /work
