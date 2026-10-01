from fastapi import FastAPI, HTTPException
from .models.recommendation import RecommendationRequest, RecommendationResponse
from .services.rule_based import build_recommendation, personalization_bonus

app = FastAPI(
    title="SmartWardrobe Recommendation Service",
    description="Internal rule-based outfit recommendation service",
    version="0.1.0",
)

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "smartwardrobe-recommendation"}

def _build_explanation(request: RecommendationRequest, selected_items) -> str:
    criteria = []
    if request.season:
        criteria.append(f"season: {request.season}")
    if request.occasion:
        criteria.append(f"occasion: {request.occasion}")
    if request.style:
        criteria.append(f"style: {request.style}")

    types = ", ".join(sorted({item.type for item in selected_items}))
    if criteria:
        base = f"Selected {types} pieces from your wardrobe that match {', '.join(criteria)}."
    else:
        base = f"Selected {types} pieces from your wardrobe (no specific filters were requested)."

    personalized_total = sum(
        personalization_bonus(item, request.preferences) for item in selected_items
    )
    if personalized_total > 0:
        return base + " Personalized based on outfits you've saved or rated highly."
    return base


@app.post("/recommend", response_model=RecommendationResponse)
async def recommend_outfit(request: RecommendationRequest):
    if not request.wardrobe_items:
        raise HTTPException(status_code=400, detail="wardrobe_items cannot be empty")

    selected_items = build_recommendation(request)
    if not selected_items:
        raise HTTPException(
            status_code=404,
            detail="No items in your wardrobe match those filters. Try different filters or add more items.",
        )

    return RecommendationResponse(
        selected_items=selected_items,
        explanation=_build_explanation(request, selected_items),
    )
