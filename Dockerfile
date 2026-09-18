FROM python:3.11-slim

# without this, Python buffers stdout inside the container and the startup
# banner / logs don't show up in `docker logs` until the buffer fills up
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
