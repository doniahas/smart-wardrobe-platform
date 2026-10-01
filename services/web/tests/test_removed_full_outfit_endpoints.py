"""Regression guard: Analyze Full Outfit and Recreate from Photo
were removed as user-facing features - recreation now only works
from structured saved/shared outfit data (services/recreate.py), never
from an arbitrary photo. These routes must stay gone, not just hidden in
the UI.

Deliberately does not send auth headers: an unregistered route rejects
before any auth dependency ever runs, so this also proves the endpoint
isn't reachable at all, not merely reachable-but-rejecting-anonymous.
"""


def test_analyze_outfit_image_route_is_gone(client):
    """POST /wardrobe/analyze-outfit-image is unregistered, but the URL
    shape still matches GET/PUT/DELETE /wardrobe/{item_id} (a real, still-
    live route with "analyze-outfit-image" bound to item_id) - so the
    correct, precise outcome for a POST here is 405 Method Not Allowed,
    not 404. A 404 would actually be the *wrong* signal: it would mean
    /wardrobe/{item_id} itself had also broken."""
    resp = client.post("/wardrobe/analyze-outfit-image", files={"image": ("x.jpg", b"", "image/jpeg")})
    assert resp.status_code == 405


def test_recreate_from_photo_route_is_gone(client):
    resp = client.post("/posts/000000000000000000000000/recreate-from-photo", json={"attachment_index": 0})
    assert resp.status_code == 404


def test_analyze_image_route_still_works(client):
    """Sanity check alongside the above: Analyze Item must still be
    a registered, reachable route - this pass removes Full Outfit/Photo
    Recreate only, never Analyze Item. Same no-auth-header proof as
    test_recreate_structured_route_still_works: 401 means the route
    exists and is auth-gated; only a genuinely removed route would 404."""
    resp = client.post("/wardrobe/analyze-image", files={"image": ("x.jpg", b"", "image/jpeg")})
    assert resp.status_code == 401


def test_recreate_structured_route_still_works(client):
    """Sanity check: GET /posts/{id}/recreate (structured) must still be a
    registered route - only the photo-based one was removed. Called with
    no auth header on purpose: a *registered* route rejects with 401
    (auth runs after route matching), while a route that no longer exists
    always 404s regardless of auth - so 401 here is the positive proof
    the route is still live, not a weaker check than 200 would be."""
    resp = client.get("/posts/000000000000000000000000/recreate")
    assert resp.status_code == 401
