from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()  # populate os.environ from .env if present; existing vars win

__all__ = ["main"]


def main() -> None:
    import uvicorn

    # uvicorn factory mode calls build_app() itself, so there's no
    # module-level FastAPI singleton in iris.app — tests can import
    # build_app freely without triggering an eager (CH-dependent) install.
    uvicorn.run(
        "iris.app:build_app",
        host="127.0.0.1",
        port=8000,
        workers=1,
        factory=True,
    )
