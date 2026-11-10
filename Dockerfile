FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CUSTOMER_ORDERS_DB=/data/customer_orders.sqlite3

COPY requirements.txt /temp/requirements.txt

COPY main /main
COPY miniapp /miniapp

WORKDIR /main

VOLUME ["/data"]

EXPOSE 8000

RUN python -m pip install --no-cache-dir -r /temp/requirements.txt

CMD ["python", "bot.py"]
