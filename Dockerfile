FROM python:3.12-alpine
WORKDIR /app
RUN pip install "discord.py>=2.3" aiohttp
COPY bot.py .
CMD ["python3", "-u", "bot.py"]
