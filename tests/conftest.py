import shutil
from pathlib import Path

def _stage(example_name: str, real_name: str) -> None:
    """Stages the config and secrets yamls if they exist. 
    If not, copies the examples to be used instead"""
    example = Path(example_name)
    real = Path(real_name)
    if not real.exists():
        shutil.copy(example, real)

_stage("config.example.yaml", "config.yaml")
_stage("secrets.example.yaml", "secrets.yaml")