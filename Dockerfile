FROM python:3.12-slim

# poppler-utils provides pdftotext, the preferred backend for transcript parsing.
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py admin_app.py db_admin.py transcript_parser.py data_layer.py \
     courses.json course_planner.db index.html admin.html entrypoint.sh ./
RUN chmod +x entrypoint.sh

# 5000 = app.py (student-facing), 5050 = admin_app.py (catalog authoring).
EXPOSE 5000 5050

ENTRYPOINT ["./entrypoint.sh"]
CMD ["python", "app.py"]
