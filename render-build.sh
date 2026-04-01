#!/usr/bin/env bash
# render-build.sh
# Downloads a pre-built static ffmpeg binary instead of using apt-get.
# Render's free tier has a read-only system partition, so apt-get fails.

set -o errexit

echo "▶ Downloading static ffmpeg binary…"
# Static build — no apt/sudo needed, works on Render's read-only FS
FFMPEG_URL="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
curl -L "$FFMPEG_URL" -o /tmp/ffmpeg.tar.xz

echo "▶ Extracting ffmpeg…"
mkdir -p /tmp/ffmpeg
tar -xf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1

echo "▶ Copying binaries to ~/.local/bin…"
mkdir -p "$HOME/.local/bin"
cp /tmp/ffmpeg/ffmpeg  "$HOME/.local/bin/ffmpeg"
cp /tmp/ffmpeg/ffprobe "$HOME/.local/bin/ffprobe"
chmod +x "$HOME/.local/bin/ffmpeg" "$HOME/.local/bin/ffprobe"

export PATH="$HOME/.local/bin:$PATH"

echo "▶ Verifying ffmpeg…"
ffmpeg -version | head -1

echo "▶ Installing Python dependencies…"
pip install --upgrade pip
pip install -r requirements.txt

echo "▶ Creating required directories…"
mkdir -p generated data

echo "✅ Build complete."