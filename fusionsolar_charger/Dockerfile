FROM python:3.12-alpine
RUN pip install --no-cache-dir fusion_solar_py==0.1.2 websocket-client tzdata
COPY charger.py run.py /app/
CMD ["python", "-u", "/app/run.py"]
