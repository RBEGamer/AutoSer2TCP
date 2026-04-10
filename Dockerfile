FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /config

VOLUME ["/config"]

ENTRYPOINT ["ser2tcp"]
CMD ["-v", "-c", "/config/config.yaml"]
