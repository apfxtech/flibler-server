import logging, uvicorn
from .api import create_app
from .config import Config

def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname).1s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    cfg = Config.from_env()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
