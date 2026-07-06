import pytest
from app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_index_page(client):
    rv = client.get('/')
    assert rv.status_code == 200

def test_live_page(client):
    rv = client.get('/live')
    assert rv.status_code == 200

def test_upload_page(client):
    rv = client.get('/upload')
    assert rv.status_code == 200

def test_manual_page(client):
    rv = client.get('/manual')
    assert rv.status_code == 200
