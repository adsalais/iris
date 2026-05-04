from dotenv import load_dotenv

load_dotenv()  # populate os.environ from .env if present; existing vars win

from iris.app import app  # noqa: E402

__all__ = ["app", "main"]


def main() -> None:
    import uvicorn

    uvicorn.run("iris.app:app", host="127.0.0.1", port=8000)
