FROM python:3.12-slim

WORKDIR /app

# install deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# application code + station-independent variable dictionary
COPY app.py ancillary_lib.py bif_parser.py BIF_Ancillary_Variables.csv ./

# station CSV cache lives on a mounted volume (see docker-compose.yml)
ENV ANCILLARY_CACHE=/data/cache
EXPOSE 8050

# gunicorn serves the Flask server exposed by Dash as `app:server`
CMD ["gunicorn", "--bind", "0.0.0.0:8050", "--workers", "2", \
     "--timeout", "180", "--access-logfile", "-", "app:server"]
