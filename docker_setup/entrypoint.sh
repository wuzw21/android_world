#!/bin/bash

# Forward emulator port to host
#============================================
ip=$(ip -4 addr show scope global | grep inet | awk '{print $2}' | cut -d/ -f1 | head -n 1)
if [ -z "$ip" ]; then
  ip="0.0.0.0"
fi
socat tcp-listen:5555,bind=$ip,fork tcp:127.0.0.1:5555 &

# Start Emulator
#============================================
./docker_setup/start_emu_headless.sh && \
adb root && \
python3 -m server.android_server
