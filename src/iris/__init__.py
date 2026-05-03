from iris.app import app

__all__ = ["app", "main"]


def main() -> None:
    import uvicorn

    uvicorn.run("iris.app:app", host="127.0.0.1", port=8000)
