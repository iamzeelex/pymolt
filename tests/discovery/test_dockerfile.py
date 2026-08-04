from pymolt.core.enums import Confidence
from pymolt.discovery.dockerfile import parse_dockerfile


def test_simple_pinned():
    s = parse_dockerfile("FROM python:3.6.15-slim\nRUN pip install -r requirements.txt\n", "Dockerfile")
    assert s.runtime_python == "3.6.15"
    assert s.runtime_confidence == Confidence.PINNED
    assert s.os_hint == "slim"
    assert "pip" in s.install_tools


def test_arg_parameterized_image():
    df = """
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-bookworm
RUN poetry install
"""
    s = parse_dockerfile(df, "Dockerfile")
    assert s.runtime_python == "3.11"
    assert s.os_hint == "bookworm"
    assert "poetry" in s.install_tools


def test_floating_tag_is_unknownish():
    s = parse_dockerfile("FROM python:3\nCMD [\"python\"]\n", "Dockerfile")
    assert s.runtime_python == "3"
    assert s.runtime_confidence == Confidence.FLOATING


def test_multistage_runtime_is_last_or_cmd_stage():
    df = """
FROM python:3.8 AS builder
RUN pip install -r requirements.txt

FROM python:3.12-slim AS runtime
COPY --from=builder /app /app
CMD ["python", "app.py"]
"""
    s = parse_dockerfile(df, "Dockerfile")
    assert len(s.stages) == 2
    assert s.runtime_python == "3.12"   # runtime stage, not the builder
    assert s.runtime_confidence == Confidence.PINNED


def test_copy_venv_abi_mismatch_flagged():
    df = """
FROM python:3.8 AS builder
RUN python -m venv /venv && /venv/bin/pip install -r requirements.txt

FROM python:3.12-slim
COPY --from=builder /venv /venv
CMD ["/venv/bin/python", "app.py"]
"""
    s = parse_dockerfile(df, "Dockerfile")
    assert s.runtime_python == "3.12"
    assert any("ABI mismatch" in n for n in s.notes)


def test_non_python_base_inferred_from_run():
    df = """
FROM ubuntu:20.04
RUN apt-get update && apt-get install -y python3.9 python3-pip
RUN pip3 install -r requirements.txt
"""
    s = parse_dockerfile(df, "Dockerfile")
    assert s.runtime_python == "3.9"
    assert s.runtime_confidence == Confidence.INFERRED


def test_alpine_flagged():
    s = parse_dockerfile("FROM python:3.11-alpine\nRUN pip install .\n", "Dockerfile")
    assert s.os_hint == "alpine"
    assert any("musl" in n for n in s.notes)


def test_digest_pinned_is_unknown():
    s = parse_dockerfile("FROM python@sha256:abcdef\nCMD ['python']\n", "Dockerfile")
    assert s.runtime_python is None
    assert s.runtime_confidence == Confidence.UNKNOWN
