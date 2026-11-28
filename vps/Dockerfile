FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive LIBGL_ALWAYS_SOFTWARE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates xz-utils xvfb xauth procps \
    libgl1 libegl1 libgl1-mesa-dri libglu1-mesa libgbm1 libdrm2 \
    libx11-6 libxext6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 libsm6 \
    && rm -rf /var/lib/apt/lists/*

ARG BLENDER_VERSION=4.2.3
RUN curl -fsSL "https://download.blender.org/release/Blender4.2/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
    | tar -xJ -C /opt \
    && ln -s /opt/blender-${BLENDER_VERSION}-linux-x64/blender /usr/local/bin/blender

RUN pip install --no-cache-dir "aiohttp>=3.9"
WORKDIR /app
COPY blender_worker.py bl_common.py bl_chars.py bl_anim.py bl_sets.py bl_sprite.py bl_cutout.py bl_custom.py scene_guard.py cutout_layout.py /app/
COPY vps/render_server.py /app/vps/render_server.py

EXPOSE 8080
CMD ["xvfb-run", "-a", "-s", "-screen 0 1280x720x24", "python", "vps/render_server.py"]
