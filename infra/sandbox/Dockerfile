FROM python:3.13-slim
WORKDIR /sandbox
COPY fair/quality/code_validator.py /sandbox/code_validator.py
COPY infra/sandbox/runner.py /sandbox/runner.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "-B", "/sandbox/runner.py"]
