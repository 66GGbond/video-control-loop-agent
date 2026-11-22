FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend ./backend
COPY frontend ./frontend
COPY run_demo.py ./
RUN useradd --create-home app && mkdir /data && chown app:app /data
USER app
ENV HOST=0.0.0.0 PORT=8000 RUNTIME_DIR=/data
EXPOSE 8000
CMD ["python3", "-u", "run_demo.py"]
