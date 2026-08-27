"""
conftest.py for axiom_graph tests.
Adds the .research directory to sys.path so that 'axiom_graph' is importable.
"""

import sys
from pathlib import Path

# Ensure .research/ is on sys.path (axiom_graph lives there)
research_dir = Path(__file__).parent.parent.parent  # .research/
if str(research_dir) not in sys.path:
    sys.path.insert(0, str(research_dir))
