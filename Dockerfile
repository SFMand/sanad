FROM python:3.12-slim

# poppler-utils provides pdftotext, the preferred backend for transcript parsing.
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py transcript_parser.py data_layer.py courses.json course_planner.db index.html ./

EXPOSE 5000

CMD ["python", "app.py"]
