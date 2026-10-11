"""Content bank domain (plan section 6): anti-repeat over the Jaccard
dedup, the record formatting validator and the bank pipeline for slot
scheduler rubrics (the LLM transport itself is the shared
writers/completion.py client)."""

from max_news_bot.bank.antirepeat import (
    filter_dissimilar,
    is_similar_to_recent,
    least_recently_shown_first,
    recent_posts_section,
)
from max_news_bot.bank.pipeline import (
    BANK_STOCK_ALERT_DAYS,
    DEFAULT_FALLBACK_TYPES,
    PRIORITY_STOCK_TYPES,
    BankPipeline,
    BankSource,
    BankSourceError,
    build_system_prompt,
    build_user_prompt,
    low_stock_types,
    parse_bank_source,
    rotation_type_index,
    stock_alert_text,
)
from max_news_bot.bank.validator import (
    check_names,
    check_numbers,
    extract_names,
    extract_numbers,
    validate_bank_formatting,
)

__all__ = [
    "BANK_STOCK_ALERT_DAYS",
    "DEFAULT_FALLBACK_TYPES",
    "PRIORITY_STOCK_TYPES",
    "BankPipeline",
    "BankSource",
    "BankSourceError",
    "build_system_prompt",
    "build_user_prompt",
    "check_names",
    "check_numbers",
    "extract_names",
    "extract_numbers",
    "filter_dissimilar",
    "is_similar_to_recent",
    "least_recently_shown_first",
    "low_stock_types",
    "parse_bank_source",
    "recent_posts_section",
    "rotation_type_index",
    "stock_alert_text",
    "validate_bank_formatting",
]
