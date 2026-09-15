"""Lead-priority scoring: features, proxy labels, and sklearn trainers."""

__all__ = ["FEATURE_COLUMNS", "NICHES"]

FEATURE_COLUMNS = (
    "rating",
    "log_review_count",
    "website_missing",
    "has_phone",
    "has_description",
    "name_token_count",
    "address_completeness",
    "niche_service_score",
    "niche_is_auto",
    "niche_is_beauty",
    "niche_is_food",
    "niche_is_home",
    "niche_is_health",
)

NICHES = (
    "automotive",
    "beauty & personal care",
    "food & beverage",
    "home services",
    "health care",
    "fitness",
    "pets",
    "legal services",
    "local service business",
)
