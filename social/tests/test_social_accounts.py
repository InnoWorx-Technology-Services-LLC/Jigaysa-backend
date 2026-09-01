"""Connect-page tests.

Focused on the things that are load-bearing and easy to get wrong: the state
signature that stands in for authentication on the callback, the upsert that
stops a reconnect duplicating rows, token encryption at rest, and the open
redirect the ``return_to`` echo would otherwise be.

Every network call is stubbed at the adapter boundary — these tests never touch
LinkedIn or Meta.
"""

import pytest
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Role, User
from social import oauth
from social.crypto import encrypt
from social.models import Provider, SocialAccount
from social.providers.base import ConnectedAccount, ProviderError

pytestmark = pytest.mark.django_db

FERNET_KEY = "Zt7Vp3nQ8sX1yL4kR6mB9wC2dF5gH0jN3pS7uV1xY8A="


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def trainer(db):
    return User.objects.create_user(
        email="trainer@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def other_trainer(db):
    return User.objects.create_user(
        email="rival@example.com", password="StrongPass123!", role=Role.TRAINER
    )


@pytest.fixture
def student(db):
    return User.objects.create_user(
        email="student@example.com", password="StrongPass123!", role=Role.STUDENT
    )


@pytest.fixture
def linkedin_configured(settings):
    settings.LINKEDIN_CLIENT_ID = "test-client"
    settings.LINKEDIN_CLIENT_SECRET = "test-secret"
    settings.SOCIAL_TOKEN_KEY = FERNET_KEY
    settings.SOCIAL_OAUTH_REDIRECT_BASE = "https://api.example.com"
    return settings


@pytest.fixture
def account(trainer, linkedin_configured):
    return SocialAccount.objects.create(
        user=trainer,
        provider=Provider.LINKEDIN,
        provider_account_id="li-1",
        display_name="Rohan Deshpande",
        handle="dr-kapoor",
        access_token="secret-token",
    )


# --- listing ---------------------------------------------------------------


def test_list_returns_every_provider_card(api, trainer):
    api.force_authenticate(trainer)
    resp = api.get(reverse("social:account-list"))

    assert resp.status_code == status.HTTP_200_OK
    keys = [card["provider"] for card in resp.data]
    assert set(keys) == {"linkedin", "x", "instagram", "facebook", "youtube"}


def test_unimplemented_providers_report_unavailable(api, trainer):
    api.force_authenticate(trainer)
    cards = {c["provider"]: c for c in api.get(reverse("social:account-list")).data}

    assert cards["youtube"]["available"] is False
    assert cards["linkedin"]["available"] is True


def test_configured_is_false_without_credentials(api, trainer, settings):
    settings.LINKEDIN_CLIENT_ID = ""
    settings.LINKEDIN_CLIENT_SECRET = ""
    api.force_authenticate(trainer)
    cards = {c["provider"]: c for c in api.get(reverse("social:account-list")).data}

    assert cards["linkedin"]["configured"] is False


def test_list_only_shows_own_accounts(api, trainer, other_trainer, account):
    api.force_authenticate(other_trainer)
    cards = {c["provider"]: c for c in api.get(reverse("social:account-list")).data}

    assert cards["linkedin"]["accounts"] == []


def test_students_cannot_reach_the_connect_page(api, student):
    api.force_authenticate(student)
    resp = api.get(reverse("social:account-list"))

    assert resp.status_code == status.HTTP_403_FORBIDDEN


# --- connect ---------------------------------------------------------------


def test_connect_returns_authorize_url(api, trainer, linkedin_configured):
    api.force_authenticate(trainer)
    resp = api.post(
        reverse("social:connect", kwargs={"provider": "linkedin"}), {}, format="json"
    )

    assert resp.status_code == status.HTTP_200_OK
    assert resp.data["authorize_url"].startswith(
        "https://www.linkedin.com/oauth/v2/authorization?"
    )
    assert "w_member_social" in resp.data["authorize_url"]
    # The registered redirect URI must win over the request's own host.
    assert "https%3A%2F%2Fapi.example.com" in resp.data["authorize_url"]


def test_connect_writes_nothing(api, trainer, linkedin_configured):
    api.force_authenticate(trainer)
    api.post(reverse("social:connect", kwargs={"provider": "linkedin"}), {}, format="json")

    assert SocialAccount.objects.count() == 0


def test_connect_503s_when_not_configured(api, trainer, settings):
    settings.LINKEDIN_CLIENT_ID = ""
    settings.LINKEDIN_CLIENT_SECRET = ""
    api.force_authenticate(trainer)
    resp = api.post(
        reverse("social:connect", kwargs={"provider": "linkedin"}), {}, format="json"
    )

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_connect_400s_for_unimplemented_provider(api, trainer):
    api.force_authenticate(trainer)
    resp = api.post(
        reverse("social:connect", kwargs={"provider": "youtube"}), {}, format="json"
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_connect_rejects_absolute_return_to(api, trainer, linkedin_configured):
    api.force_authenticate(trainer)
    resp = api.post(
        reverse("social:connect", kwargs={"provider": "linkedin"}),
        {"return_to": "https://evil.example.com/steal"},
        format="json",
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST


# --- state -----------------------------------------------------------------


def test_state_round_trips(trainer):
    state = oauth.make_state(trainer.id, "linkedin", "/trainer/courses/x/edit")
    read = oauth.read_state(state)

    assert read == {
        "user_id": trainer.id,
        "provider": "linkedin",
        "return_to": "/trainer/courses/x/edit",
    }


def test_tampered_state_is_rejected(trainer):
    state = oauth.make_state(trainer.id, "linkedin")
    with pytest.raises(oauth.InvalidState):
        oauth.read_state(state[:-4] + "aaaa")


def test_expired_state_is_rejected(trainer):
    state = oauth.make_state(trainer.id, "linkedin")
    with pytest.raises(oauth.InvalidState):
        oauth.read_state(state, max_age=-1)


# --- callback --------------------------------------------------------------


def _callback(api, provider="linkedin", **params):
    return api.get(
        reverse("social:callback", kwargs={"provider": provider}), params
    )


def test_callback_stores_the_account(api, trainer, linkedin_configured, monkeypatch):
    from social.providers import linkedin

    monkeypatch.setattr(
        linkedin,
        "exchange_code",
        lambda code, redirect_uri: [
            ConnectedAccount(
                provider="linkedin",
                provider_account_id="li-42",
                access_token="fresh-token",
                display_name="Rohan Deshpande",
                handle="dr-kapoor",
                meta={"author_urn": "urn:li:person:li-42"},
            )
        ],
    )

    state = oauth.make_state(trainer.id, "linkedin")
    resp = _callback(api, code="auth-code", state=state)

    assert resp.status_code == status.HTTP_302_FOUND
    assert "connected=linkedin" in resp.url

    account = SocialAccount.objects.get(user=trainer, provider="linkedin")
    assert account.provider_account_id == "li-42"
    assert account.access_token == "fresh-token"
    assert account.provider_meta["author_urn"] == "urn:li:person:li-42"


def test_reconnect_updates_instead_of_duplicating(
    api, trainer, linkedin_configured, monkeypatch
):
    from social.providers import linkedin

    SocialAccount.objects.create(
        user=trainer,
        provider=Provider.LINKEDIN,
        provider_account_id="li-42",
        access_token="stale",
        status=SocialAccount.Status.EXPIRED,
        last_error="Token expired",
    )

    monkeypatch.setattr(
        linkedin,
        "exchange_code",
        lambda code, redirect_uri: [
            ConnectedAccount(
                provider="linkedin",
                provider_account_id="li-42",
                access_token="renewed",
                display_name="Rohan Deshpande",
            )
        ],
    )

    _callback(api, code="auth-code", state=oauth.make_state(trainer.id, "linkedin"))

    assert SocialAccount.objects.filter(user=trainer).count() == 1
    account = SocialAccount.objects.get(user=trainer)
    assert account.access_token == "renewed"
    assert account.status == SocialAccount.Status.CONNECTED
    assert account.last_error == ""


def test_one_meta_consent_creates_facebook_and_instagram(
    api, trainer, settings, monkeypatch
):
    settings.META_APP_ID = "meta-app"
    settings.META_APP_SECRET = "meta-secret"
    settings.SOCIAL_TOKEN_KEY = FERNET_KEY
    from social.providers import meta

    monkeypatch.setattr(
        meta,
        "exchange_code",
        lambda code, redirect_uri: [
            ConnectedAccount(
                provider="facebook",
                provider_account_id="page-1",
                access_token="page-token",
                display_name="Jigyasa Academy",
            ),
            ConnectedAccount(
                provider="instagram",
                provider_account_id="ig-1",
                access_token="page-token",
                display_name="jigyasa.academy",
                meta={"page_id": "page-1"},
            ),
        ],
    )

    state = oauth.make_state(trainer.id, "facebook")
    resp = _callback(api, provider="facebook", code="auth-code", state=state)

    assert resp.status_code == status.HTTP_302_FOUND
    assert set(
        SocialAccount.objects.filter(user=trainer).values_list("provider", flat=True)
    ) == {"facebook", "instagram"}


def test_callback_without_state_redirects_with_error(api):
    resp = _callback(api, code="auth-code")

    assert resp.status_code == status.HTTP_302_FOUND
    assert "error=" in resp.url
    assert SocialAccount.objects.count() == 0


def test_callback_rejects_state_for_another_provider(api, trainer, linkedin_configured):
    state = oauth.make_state(trainer.id, "facebook")
    resp = _callback(api, provider="linkedin", code="auth-code", state=state)

    assert resp.status_code == status.HTTP_302_FOUND
    assert "error=" in resp.url
    assert SocialAccount.objects.count() == 0


def test_user_denial_redirects_without_storing(api, trainer, linkedin_configured):
    state = oauth.make_state(trainer.id, "linkedin")
    resp = _callback(api, state=state, error="user_cancelled_login")

    assert resp.status_code == status.HTTP_302_FOUND
    assert "error=" in resp.url
    assert SocialAccount.objects.count() == 0


def test_upstream_failure_redirects_with_the_reason(
    api, trainer, linkedin_configured, monkeypatch
):
    from social.providers import linkedin

    def boom(code, redirect_uri):
        raise ProviderError("No Facebook Page found on this account.")

    monkeypatch.setattr(linkedin, "exchange_code", boom)
    state = oauth.make_state(trainer.id, "linkedin")
    resp = _callback(api, code="auth-code", state=state)

    assert resp.status_code == status.HTTP_302_FOUND
    assert "No+Facebook+Page" in resp.url or "No%20Facebook%20Page" in resp.url
    assert SocialAccount.objects.count() == 0


@pytest.mark.parametrize(
    "hostile",
    ["//evil.example.com", "https://evil.example.com/steal", "http:/evil.example.com"],
)
def test_return_to_cannot_redirect_off_site(hostile):
    """An absolute return_to must never survive into the redirect.

    It is rejected at connect time too, but state is signed server-side and
    outlives that request, so the builder has to refuse it on its own.
    """
    url = oauth.frontend_redirect(hostile, connected="linkedin")

    assert url.startswith(oauth.frontend_origin() + oauth.DEFAULT_RETURN_TO)
    assert "evil.example.com" not in url


def test_frontend_origin_drops_any_path(settings):
    """FRONTEND_URL points at the student app and may carry a path.

    The trainer panel lives at the host root, so concatenating onto that value
    would send every completed connection to /student/trainer/settings/social.
    """
    settings.FRONTEND_URL = "https://lms.example.com/student"

    assert oauth.frontend_origin() == "https://lms.example.com"
    assert (
        oauth.frontend_redirect("", connected="linkedin")
        == "https://lms.example.com/trainer/settings/social?connected=linkedin"
    )


# --- disconnect ------------------------------------------------------------


def test_disconnect_deletes_the_row(api, trainer, account, monkeypatch):
    from social.providers import linkedin

    monkeypatch.setattr(linkedin, "revoke", lambda a: None)
    api.force_authenticate(trainer)
    resp = api.delete(reverse("social:account-detail", kwargs={"pk": account.pk}))

    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert not SocialAccount.objects.filter(pk=account.pk).exists()


def test_disconnect_succeeds_when_revoke_fails(api, trainer, account, monkeypatch):
    from social.providers import linkedin

    def boom(a):
        raise ProviderError("LinkedIn is unreachable.")

    monkeypatch.setattr(linkedin, "revoke", boom)
    api.force_authenticate(trainer)
    resp = api.delete(reverse("social:account-detail", kwargs={"pk": account.pk}))

    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert not SocialAccount.objects.filter(pk=account.pk).exists()


def test_cannot_disconnect_someone_elses_account(api, other_trainer, account):
    api.force_authenticate(other_trainer)
    resp = api.delete(reverse("social:account-detail", kwargs={"pk": account.pk}))

    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert SocialAccount.objects.filter(pk=account.pk).exists()


# --- token storage ---------------------------------------------------------


def test_token_is_ciphertext_in_the_database(account, django_assert_num_queries):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT access_token FROM social_socialaccount WHERE id = %s",
            [account.pk],
        )
        stored = cursor.fetchone()[0]

    assert stored.startswith("enc:v1:")
    assert "secret-token" not in stored


def test_token_decrypts_on_read(account):
    assert SocialAccount.objects.get(pk=account.pk).access_token == "secret-token"


def test_encrypt_is_idempotent(linkedin_configured):
    once = encrypt("token")
    assert encrypt(once) == once


def test_blank_token_stays_blank(linkedin_configured):
    assert encrypt("") == ""


def test_serializer_never_exposes_tokens(api, trainer, account):
    api.force_authenticate(trainer)
    cards = {c["provider"]: c for c in api.get(reverse("social:account-list")).data}
    payload = cards["linkedin"]["accounts"][0]

    assert "access_token" not in payload
    assert "refresh_token" not in payload
    assert payload["handle"] == "dr-kapoor"
