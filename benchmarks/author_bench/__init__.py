"""Repeated-trial authoring reliability (paper item 4).

One fixed brief, K independent authoring lifecycles through the **production** lead-driven
loop. Development code: nothing in the pipeline imports it and it never runs during a
research run.
"""

from benchmarks.author_bench.brief import BRIEF_PATH, load_brief

__all__ = ["BRIEF_PATH", "load_brief"]
