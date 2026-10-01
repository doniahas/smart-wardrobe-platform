def test_root_redirects_to_ui(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


def test_root_redirect_is_followed_to_a_real_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.request.url.path == "/ui/"
