FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

COPY requirements.txt /tmp/evocode-requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /tmp/evocode-requirements.txt \
    && rm /tmp/evocode-requirements.txt

COPY pytest_runner.py /opt/evocode/pytest_runner.py

WORKDIR /workspace
