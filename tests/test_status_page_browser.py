"""The status page, run in a real browser.

tests/test_status_page.py reads the page as text: its headers, its policy, what it
may not contain. That cannot see what the script does, and an audit found four
defects it missed for exactly that reason. These tests load the page in headless
Chromium against a running service and look at what the operator would see.

They need Playwright and a Chromium build, which the ordinary test run does not
install, so they skip without them:

    pip install playwright && python -m playwright install chromium
    pytest tests/test_status_page_browser.py

CI runs them in their own job with FAIR_REQUIRE_BROWSER=1, which turns a missing
browser from a skip into a failure: a job that skipped everything would be green
and prove nothing.
"""

import asyncio
import contextlib
import json
import os
import socket
import threading
import time
from datetime import UTC, datetime

import pytest

REQUIRED = os.environ.get("FAIR_REQUIRE_BROWSER") == "1"

if REQUIRED:
    import playwright.sync_api as sync_api
    import uvicorn
else:
    sync_api = pytest.importorskip("playwright.sync_api")
    uvicorn = pytest.importorskip("uvicorn")

from fair import FAIR  # noqa: E402
from fair.providers.mock import MockAdapter  # noqa: E402
from fair.schemas.domain import ProviderSpec  # noqa: E402
from fair.service.app import create_app  # noqa: E402

KEY = "corp-client-key-0123456789"
REFUSED = "FAIR did not accept that key."


def _spec(provider_id="mock", limit=10):
    return ProviderSpec(
        provider_id=provider_id,
        access_class="FREE_LOCAL",
        status="ACTIVE",
        current_access_cost_usd=0,
        requires_paid_subscription=False,
        requires_credit_purchase=False,
        auto_billing_required=False,
        programmatic_access=True,
        production_eligibility=True,
        request_limit=limit,
        request_limit_window="DAILY_UTC",
        models=[{"model_id": "model", "context_window": 32768, "capabilities": {"reasoning"}}],
    )


def _provider(**fields):
    """One row of /v1/fair/providers, as the service would send it."""
    row = {
        "provider_id": "groq",
        "status": "ACTIVE",
        "access_class": "FREE_RECURRING",
        "request_limit": 10,
        "review_expires_at": None,
        "models": ["model"],
        "quota_pool_id": "groq",
        "quota_remaining": 10,
    }
    return row | fields


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            # The service is on loopback; an outbound proxy must not be asked for it.
            chromium = playwright.chromium.launch(args=["--no-proxy-server"])
        except sync_api.Error as error:  # no browser build on this machine
            if REQUIRED:
                raise
            pytest.skip(f"Chromium is not installed for Playwright: {error}")
        else:
            yield chromium
            chromium.close()


class _Hold:
    """Keeps chosen requests waiting inside the service until the test lets go.

    Done on the server so the browser's own event loop is never the thing that is
    blocked: the page has to stay free to be clicked while its request is out.
    """

    def __init__(self, matches, *, once=False):
        self.matches, self.once = matches, once
        self.holding, self.entered = threading.Event(), threading.Event()

    def wrap(self, app):
        async def held(scope, receive, send):
            held_back = scope["type"] == "http" and self.holding.is_set() and self.matches(scope)
            if held_back and not (self.once and self.entered.is_set()):
                self.entered.set()
                while self.holding.is_set():
                    await asyncio.sleep(0.02)
            await app(scope, receive, send)

        return held


def _authorization(scope):
    return dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")


@contextlib.contextmanager
def _served(fair=None, hold=()):
    fair = fair or FAIR(providers=[(_spec(), MockAdapter("mock", text="345"))])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_app(fair, {"corp": KEY})
    for one in hold if isinstance(hold, (list, tuple)) else (hold,):
        app = one.wrap(app)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", fair
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class _Page:
    """A page on the service, with what it logged and what it asked for."""

    def __init__(
        self,
        browser,
        origin,
        *,
        timezone=None,
        now=None,
        providers=None,
        quota=None,
        init_script=None,
        wait=True,
    ):
        self.origin = origin
        options = {"timezone_id": timezone} if timezone else {}
        self.context = browser.new_context(locale="en-US", **options)
        if init_script:
            self.context.add_init_script(init_script)
        self.page = self.context.new_page()
        self.problems, self.requests = [], []
        self.page.on("console", self._console)
        self.page.on("pageerror", lambda error: self.problems.append(str(error)))
        self.page.on("request", lambda request: self.requests.append(request))
        if now is not None:
            self.page.clock.set_fixed_time(now)
        if providers is not None:
            # A list, or a callable for an answer that changes between polls.
            listing = providers if callable(providers) else (lambda: providers)
            self.page.route(
                "**/v1/fair/providers",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"providers": listing(), "skipped": {}}),
                ),
            )
        if quota is not None:
            self.page.route(
                "**/v1/fair/quota",
                lambda route: route.fulfill(
                    status=quota, content_type="application/json", body="{}"
                ),
            )
        self.page.goto(origin + "/status")
        if wait:
            self.page.locator("#stamp", has_text="Updated").wait_for()

    def poll(self):
        """What the page does when its tab is shown again: refresh now."""
        self.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

    def _console(self, message):
        if message.type in {"error", "warning"}:
            self.problems.append(message.text)

    def sign_in(self, key=KEY):
        self.page.fill("#key", key)
        self.page.click("button[type=submit]")

    def signed_in(self, key=KEY):
        self.sign_in(key)
        self.page.locator("#providers:not([hidden])").wait_for()
        return self

    def text(self, selector):
        return " ".join(self.page.locator(selector).inner_text().split())

    def hidden(self, selector):
        return self.page.locator(selector).get_attribute("hidden") is not None

    def asked(self, path):
        return [request for request in self.requests if request.url.endswith(path)]

    def close(self):
        self.context.close()


@pytest.fixture
def opened(browser):
    pages = []

    def open_page(origin, **options):
        pages.append(_Page(browser, origin, **options))
        return pages[-1]

    yield open_page
    for page in pages:
        page.close()


class TestThePageRuns:
    def test_it_renders_under_its_own_policy_and_only_reads_its_own_service(self, opened):
        with _served() as (origin, _):
            view = opened(origin)
            assert view.text("#headline") == "The one provider can take requests."
            assert view.hidden("#providers")
            view.signed_in()
            assert "mock" in view.text("#providers-body")
            assert view.text("#providers-body td[data-label='Requests left']") == "10 of 10"
            assert view.problems == []
            assert {request.method for request in view.requests} == {"GET"}
            assert all(request.url.startswith(origin) for request in view.requests)
            assert view.asked("/v1/fair/providers") and view.asked("/v1/fair/quota")

    def test_the_key_is_kept_nowhere_the_page_can_be_read_from(self, opened):
        with _served() as (origin, _):
            view = opened(origin).signed_in()
            assert view.page.input_value("#key") == ""
            assert KEY not in view.page.content()
            assert KEY not in view.page.url
            stored = view.page.evaluate(
                "JSON.stringify([Object.keys(localStorage), Object.keys(sessionStorage), "
                "document.cookie])"
            )
            assert stored == '[[],[],""]'

    def test_text_from_the_service_is_shown_as_text(self, opened):
        """A model id is whatever a provider's catalogue says it is."""
        hostile = "<img src=x onerror=window.pwned=1>"
        row = _provider(provider_id=hostile, models=[hostile + "<script>window.pwned=1</script>"])
        with _served() as (origin, _):
            view = opened(origin, providers=[row]).signed_in()
            assert hostile in view.text("#providers-body")
            assert view.page.locator("#providers-body img, #providers-body script").count() == 0
            assert view.page.evaluate("window.pwned === undefined")


class TestSigningInAndOut:
    def test_a_wrong_key_is_refused_in_words(self, opened):
        with _served() as (origin, _):
            view = opened(origin)
            view.sign_in("not-a-real-client-key-0000")
            view.page.locator("#key-error", has_text=REFUSED).wait_for()
            assert view.hidden("#providers") and not view.hidden("#key-form")

    @pytest.mark.parametrize("stray", ["​", "”", "€"])
    def test_a_key_the_browser_cannot_send_is_refused_the_same_way(self, opened, stray):
        """A pasted zero-width space or smart quote made fetch() throw, which was
        reported as FAIR not answering and repeated on every poll."""
        with _served() as (origin, _):
            view = opened(origin)
            view.sign_in(KEY + stray)
            view.page.locator("#key-error", has_text=REFUSED).wait_for()
            assert view.asked("/v1/fair/providers") == []
            assert view.text("#stamp").startswith("Updated")
            assert not view.hidden("#key-form")
            # And the page still works once the right key is typed.
            view.signed_in()

    def test_forgetting_the_key_during_a_poll_keeps_the_page_forgotten(self, opened):
        """The answer to a request sent before Forget used to redraw the table, hide
        the key form, and then sit there unrefreshed under a fresh 'Updated'."""
        hold = _Hold(lambda scope: scope["path"] == "/v1/fair/providers")
        with _served(hold=hold) as (origin, _):
            view = opened(origin).signed_in()
            hold.holding.set()
            view.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            assert hold.entered.wait(10)
            view.page.click("#forget")
            assert view.hidden("#providers")
            hold.holding.clear()
            view.page.wait_for_timeout(600)
            assert view.hidden("#providers")
            assert not view.hidden("#key-form")
            assert view.hidden("#forget")
            assert "Enter a FAIR client key" in view.text("#detail")

    def test_an_old_answer_is_dropped_even_when_nothing_newer_has_arrived(self, opened):
        """Order of arrival is not enough. After Forget the page's next poll may
        still be waiting when the old key's answer lands, so that answer is the
        newest thing seen; it is dropped because it was asked for under a key the
        page no longer holds."""
        listing = _Hold(lambda scope: scope["path"] == "/v1/fair/providers")
        health = _Hold(lambda scope: scope["path"] == "/health")
        with _served(hold=[listing, health]) as (origin, _):
            view = opened(origin).signed_in()
            listing.holding.set()
            view.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            assert listing.entered.wait(10)
            health.holding.set()
            view.page.click("#forget")
            assert health.entered.wait(10)
            listing.holding.clear()  # the old key's answer arrives first
            view.page.wait_for_timeout(600)
            assert view.hidden("#providers")
            health.holding.clear()
            view.page.wait_for_timeout(600)
            assert view.hidden("#providers")
            assert not view.hidden("#key-form")

    def test_an_old_keys_refusal_arriving_first_does_not_sign_out_the_new_key(self, opened):
        refusal = _Hold(
            lambda scope: scope["path"] == "/v1/fair/providers" and "wrong" in _authorization(scope)
        )
        health = _Hold(lambda scope: scope["path"] == "/health")
        with _served(hold=[refusal, health]) as (origin, _):
            view = opened(origin)
            refusal.holding.set()
            view.sign_in("wrong-client-key-000000000")
            assert refusal.entered.wait(10)
            health.holding.set()
            view.sign_in(KEY)
            assert health.entered.wait(10)
            refusal.holding.clear()  # the wrong key's 401 lands before the right key's poll
            view.page.wait_for_timeout(600)
            health.holding.clear()
            view.page.locator("#providers:not([hidden])").wait_for()
            assert view.text("#key-error") == ""
            assert view.hidden("#key-form")

    def test_an_older_poll_answering_late_does_not_overwrite_a_newer_one(self, opened):
        """Same key, two polls in flight. The first is answered with what was true
        when it was asked and arrives after the second has already been drawn."""
        fair = FAIR(providers=[(_spec(), MockAdapter("mock", text="345"))])
        listed = fair.providers_async
        armed, entered, release = threading.Event(), threading.Event(), threading.Event()
        calls = []

        async def listing():
            answer = await listed()
            if armed.is_set():
                calls.append(None)
                # Each poll lists twice, for /health and then for the table. The
                # second listing after arming is the older poll's table.
                if len(calls) == 2:
                    entered.set()
                    while not release.is_set():
                        await asyncio.sleep(0.02)
            return answer

        fair.providers_async = listing
        with _served(fair) as (origin, _):
            view = opened(origin).signed_in()
            assert view.text("td[data-label='State']").startswith("Taking requests")
            armed.set()
            view.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            assert entered.wait(10)
            fair._router.quota.block_security("mock")
            view.page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            view.page.locator("td[data-label='State']", has_text="Blocked").wait_for()
            release.set()
            view.page.wait_for_timeout(600)
            assert view.text("td[data-label='State']").startswith("Blocked until you review it")

    def test_a_late_refusal_of_an_old_key_does_not_sign_out_the_new_one(self, opened):
        hold = _Hold(
            lambda scope: scope["path"] == "/v1/fair/providers" and "wrong" in _authorization(scope)
        )
        with _served(hold=hold) as (origin, _):
            view = opened(origin)
            hold.holding.set()
            view.sign_in("wrong-client-key-000000000")
            assert hold.entered.wait(10)
            view.signed_in()
            hold.holding.clear()
            view.page.wait_for_timeout(600)
            assert not view.hidden("#providers")
            assert view.hidden("#key-form")
            assert view.text("#key-error") == ""


class TestWhenThingsGoWrong:
    def test_an_old_keyless_poll_answering_late_does_not_cover_the_signed_in_view(self, opened):
        """The page's first poll, sent before any key, can come back after the key
        has been typed and drawn."""
        first_health = _Hold(lambda scope: scope["path"] == "/health", once=True)
        first_health.holding.set()
        with _served(hold=first_health) as (origin, _):
            view = opened(origin, wait=False)
            assert first_health.entered.wait(10)
            view.signed_in()
            first_health.holding.clear()
            view.page.wait_for_timeout(600)
            assert "Enter a FAIR client key" not in view.text("#detail")
            assert not view.hidden("#providers")
            assert view.text("#stamp").startswith("Updated")

    def test_a_late_failure_of_an_old_poll_does_not_mark_a_fresh_view_stale(self, opened):
        deferred = []
        with _served() as (origin, _):
            view = opened(origin).signed_in()

            def defer_first(route):
                if not deferred:
                    deferred.append(route)  # answered later, by the test
                else:
                    route.continue_()

            view.page.route("**/v1/fair/quota", defer_first)
            view.poll()
            view.page.wait_for_timeout(300)
            assert deferred
            view.poll()
            view.page.wait_for_timeout(600)
            assert view.text("#stamp").startswith("Updated")
            deferred[0].abort()
            view.page.wait_for_timeout(600)
            assert view.text("#stamp").startswith("Updated")

    def test_a_service_that_stops_answering_is_given_up_on(self, opened):
        """The page waits 20 seconds; here the browser's timer is made short so the
        test does not have to."""
        quick = (
            "AbortSignal.timeout = () => { const c = new AbortController(); "
            "setTimeout(() => c.abort(new DOMException('timed out', 'TimeoutError')), 300); "
            "return c.signal; };"
        )
        listing = _Hold(lambda scope: scope["path"] == "/v1/fair/providers")
        with _served(hold=listing) as (origin, _):
            view = opened(origin, init_script=quick).signed_in()
            listing.holding.set()
            view.poll()
            view.page.locator("#stamp", has_text="stopped answering").wait_for()
            assert view.page.locator("#stamp.stale").count() == 1
            listing.holding.clear()
            view.poll()
            view.page.locator("#stamp", has_text="Updated").wait_for()

    def test_an_answer_the_page_cannot_draw_leaves_the_last_good_view_and_says_so(self, opened):
        rows = [_provider(provider_id="good")]
        with _served() as (origin, _):
            view = opened(origin, providers=lambda: rows).signed_in()
            assert "good" in view.text("#providers-body")
            assert view.text("#headline") == "The one provider can take requests."
            rows = [_provider(provider_id="broken", models=7), _provider(provider_id="fine")]
            view.poll()
            view.page.locator("#stamp", has_text="could not show what FAIR sent").wait_for()
            assert view.text("#providers-body").startswith("good")
            # Not half of the new answer either: the headline still matches the table.
            assert view.text("#headline") == "The one provider can take requests."
            assert "broken" not in view.text("#providers-body")
            assert "fine" not in view.text("#detail") + view.text("#providers-body")
            assert any("models" in problem or "forEach" in problem for problem in view.problems)


class TestWhatItSaysAboutTime:
    """The built-in reviews end at midnight UTC, which is 8 pm the evening before
    in Port of Spain. Elapsed days rounded up said '2 days left' beside tomorrow's
    date, and '1 day left' all through the last day."""

    EXPIRES = "2026-10-26T00:00:00+00:00"  # 25 Oct, 20:00 in Port of Spain

    def _review(self, opened, origin, now, timezone="America/Port_of_Spain"):
        view = opened(
            origin,
            timezone=timezone,
            now=datetime.fromisoformat(now).astimezone(UTC),
            providers=[_provider(review_expires_at=self.EXPIRES)],
        ).signed_in()
        return view.text("td[data-label='Review']"), view.text("#detail")

    def test_the_day_before_is_one_day_and_names_that_date(self, opened):
        with _served() as (origin, _):
            cell, detail = self._review(opened, origin, "2026-10-24T10:00:00-04:00")
        assert cell == "1 day left Runs out Oct 25"
        assert "runs out in 1 day, on Oct 25." in detail

    def test_the_last_day_says_today_and_when(self, opened):
        with _served() as (origin, _):
            cell, detail = self._review(opened, origin, "2026-10-25T10:00:00-04:00")
        assert cell == "Runs out today Runs out 8:00 PM"
        assert "runs out today at 8:00 PM." in detail

    def test_it_is_still_today_one_minute_before(self, opened):
        with _served() as (origin, _):
            cell, _ = self._review(opened, origin, "2026-10-25T19:59:00-04:00")
        assert cell == "Runs out today Runs out 8:00 PM"

    def test_afterwards_it_has_run_out(self, opened):
        with _served() as (origin, _):
            cell, detail = self._review(opened, origin, "2026-10-25T20:00:00-04:00")
        assert cell == "Ran out Oct 25"
        assert "A provider review has run out." in detail

    def test_the_same_moment_is_a_different_day_further_east(self, opened):
        with _served() as (origin, _):
            cell, _ = self._review(opened, origin, "2026-10-25T10:00:00-04:00", "Asia/Kolkata")
        assert cell == "1 day left Runs out Oct 26"

    def test_days_are_counted_across_a_clock_change(self, opened):
        """New York leaves daylight time on 1 Nov 2026; that day is 25 hours long."""
        view_now = "2026-10-31T12:00:00-04:00"
        with _served() as (origin, _):
            view = opened(
                origin,
                timezone="America/New_York",
                now=datetime.fromisoformat(view_now).astimezone(UTC),
                providers=[_provider(review_expires_at="2026-11-02T12:00:00-05:00")],
            ).signed_in()
            assert view.text("td[data-label='Review']") == "2 days left Runs out Nov 2"

    def test_a_resting_model_shows_when_it_returns(self, opened):
        now = datetime.fromisoformat("2026-10-24T10:00:00-04:00").astimezone(UTC)
        row = _provider(benched_models={"model": now.timestamp() + 240})
        with _served() as (origin, _):
            view = opened(
                origin, timezone="America/Port_of_Spain", now=now, providers=[row]
            ).signed_in()
            assert view.text("td[data-label='Models']") == "model Resting until 10:04 AM"


class TestWhatItSaysAboutAllowance:
    def test_a_provider_that_is_out_of_allowance_shows_none_left(self, opened):
        """FAIR's own count can sit under the cap when the provider says it is spent."""
        row = _provider(status="QUOTA_EXHAUSTED", quota_remaining=9)
        with _served() as (origin, _):
            view = opened(origin, providers=[row]).signed_in()
            assert view.text("td[data-label='State']").startswith("Out of allowance")
            assert view.text("td[data-label='Requests left']") == "0 of 10"
            assert view.page.locator(".bar span").evaluate("node => node.style.width") == "0%"

    def test_a_quota_report_that_failed_is_not_shown_as_sharing_switched_off(self, opened):
        with _served() as (origin, _):
            view = opened(origin, quota=500).signed_in()
            note = view.text("#quota-note")
            assert "could not be read" in note
            assert "Off." not in note

    def test_quota_sharing_that_is_off_says_so(self, opened):
        with _served() as (origin, _):
            view = opened(origin).signed_in()
            assert view.text("#quota-note").startswith("Off.")
