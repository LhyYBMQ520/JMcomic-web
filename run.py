from __future__ import annotations

import sys


def main() -> None:
    import uvicorn
    from app.config import config

    server = config.get("server", {})
    uvicorn.run(
        "app.main:app",
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8000)),
        reload=False,
    )


if __name__ == "__main__":
    sys.exit(main())
