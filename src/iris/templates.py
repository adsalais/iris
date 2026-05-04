from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
