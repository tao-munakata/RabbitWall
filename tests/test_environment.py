"""Isolated network configuration for unit and routed-kernel tests."""
import os
from pathlib import Path
os.environ.setdefault('RABBITWALL_CONFIG', str(Path(__file__).with_name('network-fixture.json')))
