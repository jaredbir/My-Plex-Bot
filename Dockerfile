FROM python:3.14-slim AS builder

WORKDIR /app

RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    pip install -r requirements.txt

FROM python:3.14-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

COPY . .

CMD ["python3", "main.py"]