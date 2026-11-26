FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1

# Blender (headless) + ffmpeg + виртуальный дисплей для Eevee
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates ffmpeg xvfb xauth \
        libgl1 libegl1 libgl1-mesa-dri libglx-mesa0 libxi6 libxrender1 \
        libxkbcommon0 libsm6 libxext6 libxxf86vm1 libxfixes3 libgomp1 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

ARG BLENDER_VER=4.2.3
RUN curl -fsSL "https://download.blender.org/release/Blender4.2/blender-${BLENDER_VER}-linux-x64.tar.xz" \
        | tar -xJ -C /opt \
    && ln -s /opt/blender-${BLENDER_VER}-linux-x64/blender /usr/local/bin/blender-real

# blender через xvfb: Eevee требует OpenGL, на сервере его даёт Mesa (программно)
COPY blender-xvfb /usr/local/bin/blender-xvfb
RUN chmod +x /usr/local/bin/blender-xvfb

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV BLENDER_BIN=/usr/local/bin/blender-xvfb \
    WORK_DIR=/data/work \
    PIPER_VOICES_DIR=/data/voices \
    PORT=3000

EXPOSE 3000
CMD ["python", "bot.py"]
