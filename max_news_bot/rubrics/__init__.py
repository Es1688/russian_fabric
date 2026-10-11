"""Rubric pipelines of the autonomous channel (plan section 8).

Every pipeline implements the ``RubricPipeline`` protocol from
``bot/slot_scheduler.py``: ``generate`` performs one attempt (data, LLM,
validators), ``fallback_post`` produces the reserve post from the content
bank without any LLM call. The scheduler owns attempt counts, the fallback
policy and slot lifecycle.
"""

from max_news_bot.rubrics.generative import (
    LLM_CALLS_PER_SLOT,
    GenerativePipeline,
    build_generation_prompt,
    parse_checker_answer,
)

__all__ = [
    "LLM_CALLS_PER_SLOT",
    "GenerativePipeline",
    "build_generation_prompt",
    "parse_checker_answer",
]
