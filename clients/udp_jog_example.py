#!/usr/bin/env python3
"""Minimal UDP client: the template for a dedicated app or a network joystick.

Pans slowly left then right for a few seconds while printing the status
received from the head. Standard library only.

    python udp_jog_example.py 192.168.1.50
"""
import json
import math
import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = 9000
RATE_HZ = 30                     # jog commands must be refreshed (see jog_timeout)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setblocking(False)


def send(**msg):
    sock.sendto(json.dumps(msg).encode(), (HOST, PORT))


send(cmd="subscribe")             # receive status datagrams (renew every < 10 s)
t0 = time.monotonic()
last_print = 0.0
while (t := time.monotonic() - t0) < 6.0:
    send(cmd="jog", pan=0.4 * math.sin(t * 1.5), tilt=0.0, zoom=0.0)
    try:
        while True:
            data, _ = sock.recvfrom(4096)
            status = json.loads(data)
            if status.get("type") == "status" and t - last_print > 0.5:
                last_print = t
                axes = status["axes"]
                print(" ".join(f"{n}={a['pos']:8.2f}" for n, a in axes.items()))
    except BlockingIOError:
        pass
    time.sleep(1.0 / RATE_HZ)

send(cmd="jog", pan=0, tilt=0, zoom=0)
send(cmd="status", id=1)          # commands with an "id" get a reply
time.sleep(0.2)
print("done")
