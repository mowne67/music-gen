#!/bin/bash
# Live progress bar for a running make_video.py. Ctrl+C quits (the pipeline keeps running).
cd "$(dirname "$0")"
LOG=/tmp/acestep-api.log; W=30; spin=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏); i=0

pid=$(pgrep -f "make_video.py" | head -1)
[ -n "$pid" ] || { echo "make_video.py is not running"; exit 1; }
read -r -a args <<< "$(ps -o args= -p "$pid" | sed 's/.*make_video.py //')"
name=${args[0]}; takes=$(ps -o args= -p "$pid" | awk '{for (i = NF; i > 0; i--) if ($i ~ /^[0-9]+$/ && $(i+1) ~ /^[0-9]+$/) {print $i; exit}}')
# process start time -> only count files made by this run
et=$(ps -o etime= -p "$pid" | tr -d ' '); IFS=:- read -r -a p <<< "$et"
secs=0; for v in "${p[@]}"; do secs=$((secs * 60 + 10#$v)); done
[ ${#p[@]} -eq 4 ] && secs=$(( 10#${p[0]} * 86400 + 10#${p[1]} * 3600 + 10#${p[2]} * 60 + 10#${p[3]} ))
t0=$(( $(date +%s) - secs )); total=$((takes + 4))

newer() { local n=0 f; for f in $1; do [ -f "$f" ] && [ "$(stat -f%m "$f")" -ge "$t0" ] && n=$((n + 1)); done; echo $n; }
stage() {  # latest meaningful ACE-Step log line -> friendly text
  local l; l=$(tr '\r' '\n' < "$LOG" | grep -vE "query_result|GET /health|DEBUG|^\s*$" | tail -1)
  case "$l" in
    *"MLX CFG Gen"*[0-9]%\|*) echo "planner writing notes $(grep -oE '[0-9]+%' <<<"$l" | tail -1)";;
    *[0-9]%\|*)               echo "rendering audio $(grep -oE '[0-9]+%' <<<"$l" | tail -1)";;
    *)                        echo "working";;
  esac
}

tput civis; trap 'tput cnorm; echo; exit' INT TERM
echo "▶ $name: $takes takes"
while true; do
  music=$(newer "candidates/${name}_*.wav")
  art=$(newer "art/${name}_*.png")
  loop=$(newer "video/${name}_*_loop.mp4")
  if [ "$(newer "releases/$name/youtube.md")" -gt 0 ]; then
    printf "\r\033[K\033[32m✔ Done in %dm%02ds → releases/%s/\033[0m\n" $(( ($(date +%s)-t0)/60 )) $(( ($(date +%s)-t0)%60 )) "$name"
    tput cnorm; exit
  fi
  if ! kill -0 "$pid" 2>/dev/null; then printf "\r\033[K\033[31m✘ make_video.py stopped before finishing\033[0m\n"; tput cnorm; exit 1; fi
  if   [ "$music" -lt "$takes" ]; then done_n=$music;    what="🎵 take $((music + 1))/$takes: $(stage)"
  elif [ ! -f "releases/$name/audio.wav" ] && [ "$art" -eq 0 ]; then done_n=$takes; what="🎚  stitching + mastering"
  elif [ "$art" -lt 2 ]; then done_n=$((takes + 1));     what="🎨 painting artwork ($art/2)"
  elif [ "$loop" -eq 0 ]; then done_n=$((takes + 2));    what="✨ animating loop"
  else done_n=$((takes + 3));                            what="🎬 assembling video + description"; fi
  fill=$((done_n * W / total)); el=$(( $(date +%s) - t0 ))
  bar=$(printf "%${fill}s" | tr ' ' '█')$(printf "%$((W - fill))s" | tr ' ' '░')
  printf "\r\033[K\033[36m%s\033[0m [\033[35m%s\033[0m] %3d%%  %dm%02ds  %s" "${spin[i++ % 10]}" "$bar" \
    $((done_n * 100 / total)) $((el / 60)) $((el % 60)) "$what"
  sleep 0.3
done
