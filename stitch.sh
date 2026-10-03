#!/bin/bash
# Stitch takes of a track into one continuous file with crossfades.
# Usage: ./stitch.sh last_bus_home          ->  all takes  -> mixes/last_bus_home_mix.wav
#        ./stitch.sh monsoon_window 2 3 4 5 ->  just those takes, in that order
set -euo pipefail
cd "$(dirname "$0")"
name=$1; shift; XF=4  # crossfade seconds
if [ $# -gt 0 ]; then files=(); for n in "$@"; do files+=("candidates/${name}_$n.wav"); done
else files=($(ls candidates/${name}_*.wav | sort -V)); fi
[ ${#files[@]} -ge 2 ] || { echo "need at least 2 takes of $name"; exit 1; }
mkdir -p mixes
inputs=(); filter=""
for i in "${!files[@]}"; do
  inputs+=(-i "${files[$i]}")
  # trim trailing silence so crossfades land on music, not dead air
  filter+="[$i]areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse[a$i];"
done
prev=a0
for ((i = 1; i < ${#files[@]}; i++)); do
  filter+="[$prev][a$i]acrossfade=d=$XF:c1=tri:c2=tri[x$i];"; prev=x$i
done
tmp="mixes/${name}_mix.tmp.wav"
ffmpeg -hide_banner -loglevel error -y "${inputs[@]}" -filter_complex "${filter%;}" -map "[$prev]" "$tmp"
mv "$tmp" "mixes/${name}_mix.wav"
echo "✓ mixes/${name}_mix.wav  (${#files[@]} takes: ${files[*]##*/})"
