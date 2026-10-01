"""Like/dislike a fresh, unsaved recommendation directly -
independent of routers/feedback.py's feedback flow, which requires the
outfit to already exist in db.outfits."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status

from app.auth import get_current_user
from app.core.config import RATE_LIMIT_FEEDBACK
from app.core.database import get_db
from app.core.limiter import limiter
from app.models import RecommendationFeedbackCreate, RecommendationFeedbackPublic
from app.services.recommendation_feedback import build_recommendation_feedback_components
from app.services.recommendation_feedback_learning import learn_memory_from_recommendation_feedback

router = APIRouter(prefix="/recommendation-feedback", tags=["recommendation-feedback"])


@router.post("", response_model=RecommendationFeedbackPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit(RATE_LIMIT_FEEDBACK)
async def create_recommendation_feedback(
    request: Request,
    body: RecommendationFeedbackCreate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(current_user["_id"])
    components = await build_recommendation_feedback_components(body.item_ids, user_id, db)

    reason_component_index = (
        body.item_ids.index(body.reason_item_id) if body.reason_item_id is not None else None
    )

    doc = {
        "user_id": user_id,
        "components": [c.model_dump(mode="json") for c in components],
        "liked": body.liked,
        "reason_scope": body.reason_scope.value if body.reason_scope else None,
        "reason_component_index": reason_component_index,
        "request_occasion": body.occasion.value if body.occasion else None,
        "request_season": body.season.value if body.season else None,
        "request_style": body.style.value if body.style else None,
        "comment": body.comment,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.recommendation_feedback.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Not wrapped in try/except: learn_memory_from_recommendation_feedback
    # already returns False without raising for every expected outcome
    # (see its own docstring) - a genuine failure here should propagate,
    # the same way routers/feedback.py's analogous call is never wrapped
    # either.
    await learn_memory_from_recommendation_feedback(db, user_id, doc)

    return RecommendationFeedbackPublic(
        id=str(doc["_id"]),
        liked=body.liked,
        components=components,
        reason_scope=body.reason_scope,
        reason_component_index=reason_component_index,
        occasion=body.occasion,
        season=body.season,
        style=body.style,
        comment=body.comment,
        created_at=doc["created_at"],
    )
