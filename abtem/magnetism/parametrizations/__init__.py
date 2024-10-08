from __future__ import annotations

import json
import os

from abtem.core.utils import get_data_path


def get_parameters():
    path = os.path.join(get_data_path(__file__), "lyon.json")
    with open(path, "r") as f:
        parameters = json.load(f)

    return parameters


class LyonParametrization:
    def __init__(self):
        self._parameters = get_parameters()

    @property
    def parameters(self):
        return self._parameters
