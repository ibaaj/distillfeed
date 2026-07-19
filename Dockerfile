FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir '.[server]'
EXPOSE 8080
CMD ["sh", "deployment/start.sh"]
