FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY setup_bot_account.py .

# bot_tokens.json is created by setup_bot_account.py and should be
# mounted/injected at deploy time (or replaced with Secrets Manager -
# see token_manager.py) rather than baked into the image.

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
