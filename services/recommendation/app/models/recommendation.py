from pydantic import BaseModel, Field

class ClothingItem(BaseModel):
    id: str
    type: str
    color: str | None = None
    season: str | None = None
    # A garment may fit more than one style/occasion - see smart-wardrobe's
    # web service, whose _doc_to_clothing_item always sends a list here
    # (coercing a legacy scalar-stored item into a one-element list before
    # it ever reaches this service, so this model never needs to accept a
    # bare string for either field).
    style: list[str] = Field(default_factory=list)
    occasion: list[str] = Field(default_factory=list)

class UserPreferences(BaseModel):
    disliked_colors: list[str] = Field(default_factory=list)
    preferred_styles: list[str] = Field(default_factory=list)

    # Long-term memory: explicit user-declared preferences, distinct from
    # the learned scores below. disliked_item_types is a hard exclusion
    # like disliked_colors; preferred_occasions is a soft bonus like
    # preferred_styles.
    disliked_item_types: list[str] = Field(default_factory=list)
    preferred_occasions: list[str] = Field(default_factory=list)

    # Personalization: signed weights learned from a user's saved
    # outfits and ratings/likes, keyed by lowercased attribute value.
    # Positive = the user tends to like this, negative = tends to dislike it.
    color_scores: dict[str, float] = Field(default_factory=dict)
    style_scores: dict[str, float] = Field(default_factory=dict)
    occasion_scores: dict[str, float] = Field(default_factory=dict)
    season_scores: dict[str, float] = Field(default_factory=dict)
    type_scores: dict[str, float] = Field(default_factory=dict)

class RecommendationRequest(BaseModel):
    occasion: str | None = None
    season: str | None = None
    style: str | None = None
    wardrobe_items: list[ClothingItem]
    preferences: UserPreferences = Field(default_factory=UserPreferences)

class RecommendationResponse(BaseModel):
    selected_items: list[ClothingItem]
    explanation: str
