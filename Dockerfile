FROM python:3.9-slim

# Instalar dependências do sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    unzip \
    curl \
    gnupg \
    libnss3 \
    libgconf-2-4 \
    libxss1 \
    libappindicator3-1 \
    libxkbfile1 \
    xdg-utils \
    fonts-liberation \
    libatk-bridge2.0-0 \
    libgbm1 \
    libcups2 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    libx11-xcb1 \
    libxcomposite1 \
    chromium-driver \
    && rm -rf /var/lib/apt/lists/*

# Instalar Chrome (necessário para Selenium)
RUN curl -sS https://dl.google.com/linux/linux_signing_key.pub | apt-key add - && \
    echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && apt-get install -y --no-install-recommends google-chrome-stable

ENV CHROME_BIN=/usr/bin/google-chrome
ENV CHROMEDRIVER_BIN=/usr/bin/chromedriver

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
WORKDIR /app

EXPOSE 5000

CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:server"]