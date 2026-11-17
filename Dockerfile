FROM python:3.9-slim

RUN apt-get update && apt-get install -y ffmpeg

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p upload/temp upload/projects upload/trimmed

EXPOSE 7860

CMD ["gunicorn", "-b", "0.0.0.0:7860", "--timeout", "600", "backend.server:app"]