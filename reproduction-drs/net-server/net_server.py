#!/usr/bin/env python3
"""Server for DRS's Transmission app (Jian et al. 2024). The authors' server is not published:
their client (net-network.py) only shows that it receives 1024 bytes and echoes them, 2000
times. This server sends 1 KiB, waits for the echo, and repeats until the client closes: a
ping-pong, so the rate is set by the round trip. Standard library only. Logs one JSON line per
connection."""

import argparse
import asyncio
import json
import os
import time


async def serve(reader, writer, size, pause_s):
    peer = writer.get_extra_info("peername")
    started, exchanges = time.monotonic(), 0
    payload = os.urandom(size)
    try:
        while True:
            writer.write(payload)
            await writer.drain()
            echoed = 0
            while echoed < size:  # the client may echo a message in pieces
                chunk = await reader.read(size - echoed)
                if not chunk:
                    raise ConnectionResetError
                echoed += len(chunk)
            exchanges += 1
            if pause_s:
                await asyncio.sleep(pause_s)
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        elapsed = time.monotonic() - started
        print(json.dumps({"t": round(time.time(), 3), "peer": f"{peer[0]}:{peer[1]}", "exchanges": exchanges,
                          "seconds": round(elapsed, 3),
                          "kb_per_s": round(exchanges * size / 1024 / elapsed, 3) if elapsed else None}),
              flush=True)
        writer.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", required=True, help="the address the workers reach, not 0.0.0.0")
    parser.add_argument("--port", type=int, default=5301)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--pause-s", type=float, default=0.0, help="0: pure ping-pong, as assumed")
    args = parser.parse_args()
    server = await asyncio.start_server(lambda r, w: serve(r, w, args.size, args.pause_s), args.bind, args.port)
    print(json.dumps({"event": "listening", "bind": args.bind, "port": args.port, "size": args.size,
                      "pause_s": args.pause_s}), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
