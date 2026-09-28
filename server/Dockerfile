FROM python:3.11-alpine
WORKDIR /app
COPY server/app.py /app/app.py
COPY web /app/web
EXPOSE 5178
CMD ["python", "/app/app.py", "--port", "5178"]
