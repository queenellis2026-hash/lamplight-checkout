#!/bin/bash
set -e
cd ~/workspace/experiment/wallet/top3/paypal-checkout-v8

# Section 1: 16 frames over 65s (0:00-1:05) — "Done" visible by 1:04
ffmpeg -y -framerate 0.2462 -i tw2_frames/f_%03d.png \
  -vf "select='between(n,0,15)',setpts=N/30/TB,scale=1920:1080,fps=30,format=yuv420p" \
  -t 65 -c:v libx264 -pix_fmt yuv420p part1.mp4 2>&1 | tail -1

# Section 2: 12 frames over 20s (1:05-1:25)
ffmpeg -y -framerate 0.6 -start_number 16 -i tw2_frames/f_%03d.png \
  -vf "select='between(n,0,11)',setpts=N/30/TB,scale=1920:1080,fps=30,format=yuv420p" \
  -t 20 -c:v libx264 -pix_fmt yuv420p part2.mp4 2>&1 | tail -1

# Section 3: 15 frames over 41s (1:25-end)
ffmpeg -y -framerate 0.3659 -start_number 28 -i tw2_frames/f_%03d.png \
  -vf "select='between(n,0,14)',setpts=N/30/TB,scale=1920:1080,fps=30,format=yuv420p" \
  -t 41 -c:v libx264 -pix_fmt yuv420p part3.mp4 2>&1 | tail -1

# Concat with audio (no delay, starts at 0)
ffmpeg -y -i part1.mp4 -i part2.mp4 -i part3.mp4 -i demo-voiceover-v3.mp3 \
  -filter_complex "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]" \
  -map "[v]" -map 3:a \
  -c:v libx264 -pix_fmt yuv420p -r 30 -c:a aac -shortest \
  demo-video-v13-timed.mp4 2>&1 | tail -1

# Prepend 10s intro
ffmpeg -y -loop 1 -t 10 -i intro.png -i demo-video-v13-timed.mp4 \
  -filter_complex "[0:v][1:v]concat=n=2:v=1:a=0[v]" \
  -map "[v]" -map 1:a \
  -c:v libx264 -pix_fmt yuv420p -r 30 -c:a aac -shortest \
  demo-video-v13-final.mp4 2>&1 | tail -1

echo "Done: demo-video-v13-final.mp4"
