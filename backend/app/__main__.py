"""Start the API with safe defaults:  .venv/Scripts/python -m app [--port 8000] [--reload]

uvicorn's own proxy-header handling is switched OFF. By default uvicorn trusts X-Forwarded-For from
127.0.0.1 and rewrites the client address before the app sees it, which would let any local process
choose its own address and bypass per-IP rate limits. Client addresses behind a real proxy are
resolved by the app itself, only for peers listed in CIVICLENS_TRUSTED_PROXIES (see ratelimit.py).
If you start uvicorn directly instead, pass --no-proxy-headers.
"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app", description="Run the Namma CivicLens API.")
    parser.add_argument("--host", default="127.0.0.1", help="Interface to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Reload on code changes (development)")
    args = parser.parse_args()
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload, proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
