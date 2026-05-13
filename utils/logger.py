import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

from utils.env_config import get_logs_dir

class SystemAuditor:
    """Singleton-диспетчер системного аудита и телеметрии."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SystemAuditor, cls).__new__(cls)
            cls._instance._init_logger()
        return cls._instance

    def _init_logger(self):
        self.log_dir = get_logs_dir()
        self.log_file = self.log_dir / "tensor_media.log"

        self.logger = logging.getLogger("TensorMedia")
        self.logger.setLevel(logging.DEBUG)
        
        if self.logger.hasHandlers():
            self.logger.handlers.clear()

        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | [%(module)s:%(lineno)d] | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        file_handler = RotatingFileHandler(
            self.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.INFO)

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def get_logger(self):
        return self.logger

auditor = SystemAuditor().get_logger()