from __future__ import annotations

import random

from meridian.lib.state.wordlists import ADJECTIVES, NOUNS


def generate_project_id() -> str:
    adj = random.choice(ADJECTIVES)
    noun1 = random.choice(NOUNS)
    noun2 = random.choice(NOUNS)
    while noun2 == noun1:
        noun2 = random.choice(NOUNS)
    return f"{adj}-{noun1}-{noun2}"
