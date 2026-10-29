#!/bin/sh
# Rendered video → small looping GIF for the README (needs ffmpeg).
#   tools/anim/make-gif.sh [video.webm] [out.gif] [width] [fps]
set -e
cd "$(dirname "$0")/../.."
IN=${1:-anim/purpose.webm}
OUT=${2:-anim/purpose.gif}
W=${3:-640}
FPS=${4:-12}
mkdir -p "$(dirname "$OUT")"
ffmpeg -v error -y -i "$IN" -vf "fps=$FPS,scale=$W:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" -loop 0 "$OUT"
ls -l "$OUT"
