FROM python:3.7.17-slim-bullseye@sha256:603879aeff06df23f0d392a19f7ce02aa85e14e6b4d25fd9647b116e0cb93467

COPY benchmarks/marshmallow_1359/docker/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY docker/pytest/pytest_runner.py /opt/evocode/pytest_runner.py
COPY benchmarks/marshmallow_1359/docker/evocode_import_guard.py /opt/evocode/evocode_import_guard.py
ENV PYTHONPATH=/workspace/src:/opt/evocode
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
ENV PYTEST_ADDOPTS="-p evocode_import_guard -o addopts= --tb=short"
WORKDIR /workspace
