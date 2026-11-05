FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv
FROM python:3.12-alpine3.24@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8 AS audio
WORKDIR /build
RUN apk upgrade --no-cache && apk add --no-cache build-base pkgconf nasm curl gnupg xz tar lame-dev opus-dev
# Signed upstream source, pinned hash and release-key fingerprint. Only audio parsers are built.
RUN curl --http1.1 --tls-max 1.2 --retry 3 --retry-all-errors --connect-timeout 20 --max-time 120 -fsSLO https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz \
    && curl --http1.1 --tls-max 1.2 --retry 3 --retry-all-errors --connect-timeout 20 --max-time 120 -fsSLO https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz.asc \
    && curl --http1.1 --tls-max 1.2 --retry 3 --retry-all-errors --connect-timeout 20 --max-time 120 -fsSLo release-key.asc https://ffmpeg.org/ffmpeg-devel.asc \
    && echo '8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e  ffmpeg-9.0.2.tar.xz' | sha256sum -c - \
    && mkdir -m 700 keyring \
    && gpg --batch --homedir /build/keyring --import release-key.asc \
    && test "$(gpg --batch --homedir /build/keyring --with-colons --list-keys | awk -F: '$1=="fpr" {print $10; exit}')" = FCF986EA15E6E293A5644F10B4322F04D67658D8 \
    && gpg --batch --homedir /build/keyring --verify ffmpeg-9.0.2.tar.xz.asc ffmpeg-9.0.2.tar.xz \
    && gpgconf --homedir /build/keyring --kill gpg-agent \
    && tar -xf ffmpeg-9.0.2.tar.xz --strip-components=1
RUN ./configure --prefix=/opt/audio --disable-everything --disable-autodetect \
      --disable-debug --disable-doc --disable-network --disable-ffplay --disable-avdevice \
      --disable-swscale --disable-gpl --disable-nonfree --enable-pic \
      --enable-ffmpeg --enable-ffprobe --enable-avcodec --enable-avformat --enable-avfilter --enable-swresample \
      --enable-libmp3lame --enable-libopus --enable-protocol=file,pipe \
      --enable-demuxer=matroska,mov,ogg,mp3,aac,wav,flac \
      --enable-parser=aac,mpegaudio,opus,vorbis,flac \
      --enable-decoder=aac,aac_fixed,mp3,mp3float,opus,vorbis,flac,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s32le,pcm_f32le,pcm_f64le \
      --enable-encoder=libmp3lame,libopus,pcm_s16le --enable-muxer=mp3,ogg,wav \
      --enable-filter=abuffer,abuffersink,aformat,aresample,asetrate,asplit,apad,afir,volume,amix,anull,alimiter,atrim,asetpts,afade \
      --extra-cflags='-O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2' \
      --extra-ldflags='-Wl,-z,relro,-z,now -Wl,-z,noexecstack' \
    && make -j2 && make install \
    && mkdir -p /opt/audio/licenses && cp COPYING.LGPLv2.1 /opt/audio/licenses/
FROM python:3.12-alpine3.24@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ARG REVISION=local
ENV SRTUBE_REVISION=$REVISION
LABEL org.opencontainers.image.revision=$REVISION
LABEL org.opencontainers.image.source="https://github.com/cuwuvaa/SRTube"
WORKDIR /app
RUN apk upgrade --no-cache && apk add --no-cache nodejs lame-libs opus ca-certificates \
    && apk add --no-cache --repository https://dl-cdn.alpinelinux.org/alpine/edge/main 'sqlite-libs=3.54.1-r0' \
    && addgroup -g 10001 srtube && adduser -D -u 10001 -G srtube srtube
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=audio /opt/audio/bin/ffmpeg /opt/audio/bin/ffprobe /usr/local/bin/
COPY --from=audio /opt/audio/licenses /usr/local/share/licenses/ffmpeg
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python3.12 --no-python-downloads \
    && rm -f /usr/local/bin/uv \
    && rm -rf /root/.cache \
    && mkdir -p /app/data /app/state /app/tracks /run/srtube \
    && chown srtube:srtube /app/data /app/state /app/tracks /run/srtube \
    && chmod 700 /app/data /app/state /app/tracks /run/srtube
USER srtube
ENV PATH="/app/.venv/bin:$PATH" DATA_DIR=/app/data
CMD ["python", "-m", "srtube"]
