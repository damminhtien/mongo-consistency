FROM python:3.14.7-alpine

RUN apk add --no-cache iptables
COPY server.py /server.py

EXPOSE 29091 29092 29093
ENTRYPOINT ["python", "/server.py"]
