"""The read-only status page, and the two facts added to the provider view for it.

No browser runs here, so what these pin is what can be read off the document: that
it is served without a key yet carries no data, that its Content-Security-Policy
admits exactly the script and stylesheet it ships, that it loads nothing from
elsewhere, only ever reads, and never writes provider text into the page as HTML.
"""

import base64
import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fair import FAIR
from fair.governor.qualification import REVIEW_MAX_AGE, qualified, review_expiry
from fair.providers.mock import MockAdapter
from fair.schemas.domain import AccessClass, ProviderSpec, ProviderState
from fair.schemas.qualification import ModelQualification, ProviderQualification
from fair.service.app import create_app
from fair.service.status_page import STATUS_PAGE

CLIENT_KEY = "corp-client-key-0123456789"
ADMIN_KEY = "admin-service-key-01234567890123456789"


def _spec(provider_id="reviewed", *, reviewed_days_ago=10, live_test_days_ago=None, local=False):
    """A provider whose review passes today, dated as the test asks."""
    now = datetime.now(UTC)
    reviewed = now - timedelta(days=reviewed_days_ago, seconds=30)
    tested = reviewed if live_test_days_ago is None else now - timedelta(days=live_test_days_ago)
    values = dict(
        provider_id=provider_id,
        access_class="FREE_LOCAL" if local else "FREE_RECURRING",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        request_limit=10,
        request_limit_window="DAILY_UTC",
        models=[{"model_id": "model", "context_window": 32768, "capabilities": {"reasoning"}}],
    )
    if not local:
        values |= dict(
            terms_last_verified=reviewed,
            qualification=ProviderQualification(
                provider_id=provider_id,
                free_status="verified_free_plan",
                production_allowed=True,
                billing_enabled=False,
                payment_method_required=False,
                payment_method_present=False,
                can_auto_bill=False,
                reviewed_at=reviewed,
                expires_at=reviewed + timedelta(days=29),
                reviewer_reference="t",
                billing_reference="t",
                terms_reference="t",
                privacy_reference="t",
                limits_reference="t",
                models=[
                    ModelQualification(
                        model_id="model",
                        input_price_per_million=0,
                        output_price_per_million=0,
                        request_price=0,
                        pricing_reference="t",
                        paid_tools_enabled=False,
                        live_test_passed=True,
                        live_test_at=tested,
                        live_test_reference="t",
                        zero_charge_verified=True,
                        zero_charge_reference="t",
                    )
                ],
            ),
        )
    return ProviderSpec(**values)


def _app(*specs, admin_key=None):
    specs = specs or (_spec(),)
    fair = FAIR(providers=[(spec, MockAdapter(spec.provider_id, text="345")) for spec in specs])
    return create_app(fair, {"corp": CLIENT_KEY}, admin_key=admin_key), fair


def _inline(tag):
    found = re.findall(rf"<{tag}>(.*?)</{tag}>", STATUS_PAGE, re.DOTALL)
    assert len(found) == 1
    return found[0]


SCRIPT, STYLE = _inline("script"), _inline("style")


class TestServingThePage:
    def test_it_is_served_without_a_key_and_holds_no_data(self):
        app, _ = _app(admin_key=ADMIN_KEY)
        with TestClient(app) as client:
            response = client.get("/status")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.text == STATUS_PAGE
        for secret in (CLIENT_KEY, ADMIN_KEY, "reviewed", "corp"):
            assert secret not in response.text

    def test_the_data_behind_it_still_needs_a_client_key(self):
        app, _ = _app()
        with TestClient(app) as client:
            for path in ("/v1/fair/providers", "/v1/fair/quota"):
                assert client.get(path).status_code == 401
                assert client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401

    def test_it_answers_get_and_nothing_else(self):
        app, _ = _app()
        with TestClient(app) as client:
            assert client.post("/status").status_code == 405
            assert client.put("/status").status_code == 405
            assert client.delete("/status").status_code == 405

    def test_the_policy_admits_exactly_the_script_and_style_that_are_shipped(self):
        """A hash that drifts from the content blocks the page in every browser."""
        app, _ = _app()
        with TestClient(app) as client:
            policy = client.get("/status").headers["content-security-policy"]
        directives = dict(part.strip().split(" ", 1) for part in policy.split(";"))

        def pin(source):
            digest = hashlib.sha256(source.encode()).digest()
            return "'sha256-" + base64.b64encode(digest).decode() + "'"

        assert directives == {
            "default-src": "'none'",
            "script-src": pin(SCRIPT),
            "style-src": pin(STYLE),
            "connect-src": "'self'",
            "base-uri": "'none'",
            "form-action": "'none'",
            "frame-ancestors": "'none'",
        }
        assert "unsafe" not in policy

    def test_it_cannot_be_framed_sniffed_cached_or_leak_a_referrer(self):
        app, _ = _app()
        with TestClient(app) as client:
            headers = client.get("/status").headers
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cache-control"] == "no-store"


class TestWhatThePageDoes:
    def test_nothing_is_loaded_from_anywhere(self):
        for marker in ("http://", "https://", "//cdn", "@import", "url(", " src=", " href="):
            assert marker not in STATUS_PAGE, marker
        assert STATUS_PAGE.count("<script") == 1
        assert STATUS_PAGE.count("<style") == 1

    def test_it_only_reads_and_only_from_three_endpoints(self):
        assert re.findall(r'read\("([^"]+)"', SCRIPT) == [
            "/health",
            "/v1/fair/providers",
            "/v1/fair/quota",
        ]
        assert SCRIPT.count("fetch(") == 1
        assert 'method: "GET"' in SCRIPT
        for verb in ("POST", "PUT", "PATCH", "DELETE"):
            assert verb not in SCRIPT
        assert "/admin/" not in STATUS_PAGE

    def test_provider_text_is_never_written_into_the_page_as_html(self):
        """Model ids and skip reasons come from outside; they go in as text only."""
        for sink in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "Function(",
            "setAttribute",
            "srcdoc",
        ):
            assert sink not in SCRIPT, sink
        assert "textContent" in SCRIPT

    def test_the_client_key_is_kept_nowhere_but_a_variable(self):
        for store in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
            assert store not in SCRIPT, store
        assert 'type="password"' in STATUS_PAGE
        assert 'autocomplete="off"' in STATUS_PAGE

    @pytest.mark.parametrize("state", list(ProviderState))
    def test_every_provider_state_has_its_own_wording(self, state):
        """A state added later must not reach the operator as a bare code."""
        assert re.search(
            rf'^\s+{state.value}: \["(ok|wait|out)", "[^"]+", "[^"]+"\],$', SCRIPT, re.M
        )

    @pytest.mark.parametrize("access", list(AccessClass))
    def test_every_access_class_has_its_own_wording(self, access):
        assert re.search(rf'^\s+{access.value}: "[^"]+",$', SCRIPT, re.M)

    def test_only_the_two_routable_states_count_as_taking_requests(self):
        """The headline must agree with what /health calls routable."""
        ok = re.findall(r'^\s+([A-Z_]+): \["ok",', SCRIPT, re.M)
        assert ok == ["ACTIVE"]
        assert 'provider.status === "ACTIVE" || provider.status === "QUOTA_PRESSURE"' in SCRIPT


class TestWhatTheProviderViewAdds:
    def test_the_request_cap_and_the_review_expiry_are_listed(self):
        reviewed, local = _spec(), _spec("local", local=True)
        app, fair = _app(reviewed, local)
        with TestClient(app) as client:
            listed = client.get(
                "/v1/fair/providers", headers={"Authorization": f"Bearer {CLIENT_KEY}"}
            ).json()["providers"]
        by_id = {provider["provider_id"]: provider for provider in listed}
        assert by_id["reviewed"]["request_limit"] == 10
        assert by_id["reviewed"]["review_expires_at"] == (
            reviewed.qualification.expires_at.isoformat()
        )
        # A local provider has no review, so nothing to run out.
        assert by_id["local"]["review_expires_at"] is None

    async def test_the_two_provider_views_agree(self):
        _, fair = _app(_spec(), _spec("local", local=True))
        assert fair.providers() == await fair.providers_async()
        await fair.close()


class TestReviewExpiry:
    def test_it_is_the_moment_the_review_stops_admitting_the_provider(self):
        spec = _spec()
        expiry = review_expiry(spec)
        assert expiry == spec.qualification.expires_at
        assert qualified(spec, expiry - timedelta(seconds=1)) is True
        assert qualified(spec, expiry) is False

    def test_an_ageing_live_test_can_end_it_sooner(self):
        """Every dated condition counts, not only the review's own expiry."""
        spec = _spec(reviewed_days_ago=2, live_test_days_ago=20)
        tested = spec.qualification.models[0].live_test_at
        expiry = review_expiry(spec)
        assert expiry == tested + REVIEW_MAX_AGE
        assert expiry < spec.qualification.expires_at
        assert qualified(spec, expiry - timedelta(seconds=1)) is True
        assert qualified(spec, expiry + timedelta(seconds=1)) is False

    def test_an_inactive_models_live_test_does_not_count(self):
        spec = _spec(reviewed_days_ago=2, live_test_days_ago=20)
        spare = spec.models[0].model_copy(update={"model_id": "spare"})
        spec.models.append(spare)
        review = spec.qualification.models[0]
        spec.qualification.models.append(
            review.model_copy(
                update={"model_id": "spare", "live_test_at": spec.qualification.reviewed_at}
            )
        )
        spec.models[0].active = False
        assert review_expiry(spec) == spec.qualification.expires_at

    def test_a_provider_with_no_review_has_nothing_to_expire(self):
        assert review_expiry(_spec(local=True)) is None
