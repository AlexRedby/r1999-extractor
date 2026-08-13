import os
from pathlib import Path

from platformdirs import user_data_path

application_directory_name = "Reverse1999Extractor"


def get_local_data_directory(environment=None):
    environment = os.environ if environment is None else environment
    configured = environment.get("R1999_EXTRACTOR_DATA")
    if configured:
        return Path(configured).expanduser()
    return user_data_path(application_directory_name, appauthor=False)
