"""The serverless e2e's stand-in for Vercel's edge (e2e/docker-compose.vercel.yml, docs/deploy-vercel.md).

Every request goes to the NEXT instance of the `web` service, round-robin per request (not per connection), so
two consecutive requests of one browser land on different instances, as they may on Vercel. What Vercel does at
the edge and the app must live with is reproduced:

  * a request body over 4.5 MB is refused here with 413 FUNCTION_PAYLOAD_TOO_LARGE, before any instance sees it;
  * X-Forwarded-For is SET to the client's address (Vercel overwrites it rather than appending), X-Forwarded-Proto
    and X-Forwarded-Host are set; Host is passed through (the public URL's, or the deployment URL a cron call uses);
  * a request running longer than MAX_DURATION_S gets 504 FUNCTION_INVOCATION_TIMEOUT.

For the test only: `x-sim-route: <n>` pins a request to instance n (sorted by address), every response says which
instance answered in `x-sim-instance`, and GET /_sim/instances lists the instances with their request counts.
"""
import asyncio
import itertools
import json
import os
import socket

import httpx

UPSTREAM_HOST = os.environ.get("UPSTREAM_HOST", "web")
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "8140"))
BODY_LIMIT = int(os.environ.get("BODY_LIMIT", "4500000"))
MAX_DURATION_S = float(os.environ.get("MAX_DURATION_S", "800"))
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding",
       "upgrade", "content-length", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip"}
_turn = itertools.count()
HITS: dict = {}
client = httpx.AsyncClient(timeout=httpx.Timeout(MAX_DURATION_S, connect=5.0), follow_redirects=False)


def instances() -> list:
    """The web service's instances, as the network's DNS lists them (every replica), in a stable order."""
    infos = socket.getaddrinfo(UPSTREAM_HOST, UPSTREAM_PORT, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos}, key=lambda ip: tuple(int(p) for p in ip.split(".")))


async def _reply(send, status, body: bytes, ctype=b"text/plain", extra=()):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode()), *extra]})
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    headers = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope["headers"]]
    lookup = dict(headers)
    if scope["path"] == "/_sim/instances":
        await _reply(send, 200, json.dumps({"instances": instances(), "hits": HITS}).encode(), b"application/json")
        return
    declared = lookup.get("content-length", "")
    if declared.isdigit() and int(declared) > BODY_LIMIT:
        await _reply(send, 413, b"Request Entity Too Large\n\nFUNCTION_PAYLOAD_TOO_LARGE\n")
        return
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > BODY_LIMIT:
            await _reply(send, 413, b"Request Entity Too Large\n\nFUNCTION_PAYLOAD_TOO_LARGE\n")
            return
        chunks.append(chunk)
        if not message.get("more_body"):
            break
    pool = instances()
    pinned = lookup.get("x-sim-route", "")
    ip = pool[int(pinned) % len(pool)] if pinned.isdigit() else pool[next(_turn) % len(pool)]
    HITS[ip] = HITS.get(ip, 0) + 1
    client_ip = (scope.get("client") or ("0.0.0.0", 0))[0]
    forward = [(k, v) for k, v in headers if k not in HOP and k != "x-sim-route"]
    forward += [("x-forwarded-for", client_ip), ("x-forwarded-proto", scope.get("scheme", "http")),
                ("x-forwarded-host", lookup.get("host", ""))]
    query = scope.get("query_string", b"").decode("latin-1")
    url = f"http://{ip}:{UPSTREAM_PORT}{scope['raw_path'].decode('latin-1') if scope.get('raw_path') else scope['path']}"
    if query and "?" not in url:
        url += "?" + query
    try:
        response = await client.request(scope["method"], url, headers=forward, content=b"".join(chunks))
    except httpx.TimeoutException:
        await _reply(send, 504, b"An error occurred with your deployment\n\nFUNCTION_INVOCATION_TIMEOUT\n")
        return
    except httpx.HTTPError:
        await _reply(send, 502, b"An error occurred with your deployment\n\nNO_RESPONSE_FROM_FUNCTION\n")
        return
    out = [(k, v) for k, v in response.headers.raw
           if k.decode("latin-1").lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding")]
    body = response.content
    out += [(b"content-length", str(len(body)).encode()), (b"x-sim-instance", ip.encode())]
    await send({"type": "http.response.start", "status": response.status_code, "headers": out})
    await send({"type": "http.response.body", "body": body})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8140")), log_level="warning",
                proxy_headers=False, loop="asyncio")
    asyncio.run(client.aclose())
