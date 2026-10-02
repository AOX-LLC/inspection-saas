#!/usr/bin/env python3
"""Walk through the web app in a headless browser, the way a person would.

A seeded user signs in, opens a project, drops in a batch of synthetic photos, watches
them upload and get processed, and sees the finished photos in the grid. Along the way
the script checks what the browser did: no console errors, no Content-Security-Policy
violations, and no image request that is not a thumbnail.

    cd api && uv run --with playwright python ../scripts/web_walkthrough.py \
        --screenshots ../docs/screenshots

`--states DIR` also visits every page and state (empty, loading, error, uploading,
done, refused files, a viewer, narrow and dark) and saves a screenshot of each, for a
design review. Needs the stack up (`docker compose up -d --wait`) and a Chromium that
Playwright can drive. Uses only the demo seed and generated images, all synthetic.
"""

import argparse
import re
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Request,
    ViewportSize,
    expect,
    sync_playwright,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))
from app.seed.batch import batch_photos  # noqa: E402

ALPHA_PROJECT = "Synthetic Car Park Level 2"
# The refused-files step uploads here, so it is not empty on a second run.
SCRATCH_PROJECT = "Synthetic Bridge Pier 4"
# Nothing in the walkthrough uploads to this one, so it always shows the empty state.
EMPTY_PROJECT = ("Demo Org Beta", "Synthetic Water Tank Roof")
VIEWPORT: ViewportSize = {"width": 1280, "height": 860}
PHONE: ViewportSize = {"width": 390, "height": 844}
STORE_ORIGIN = "http://127.0.0.1:4703"


class Watch:
    """Collects what the browser complains about and which images it asked for."""

    def __init__(self, page: Page, base: str) -> None:
        self.problems: list[str] = []
        self.images: list[str] = []
        self._base = base
        page.on("console", lambda m: self._console(m.type, m.text))
        page.on("pageerror", lambda e: self.problems.append(f"page error: {e}"))
        page.on("request", self._request)

    def _console(self, kind: str, text: str) -> None:
        # A 4xx the script provokes on purpose is logged by the browser as an error.
        if kind == "error" and "Failed to load resource" not in text:
            self.problems.append(f"console {kind}: {text}")

    def _request(self, request: Request) -> None:
        if request.resource_type == "image":
            self.images.append(request.url)


def sign_in(page: Page, base: str, account: str) -> None:
    page.goto(f"{base}/login")
    page.get_by_role("button", name=re.compile(account)).click()
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("heading", name="Organizations")).to_be_visible()


def open_project(page: Page, project: str, org: str = "Demo Org Alpha") -> None:
    page.get_by_role("link", name=re.compile(org)).click()
    page.get_by_role("link", name=re.compile(project)).click()
    expect(page.get_by_role("heading", name=project, level=1)).to_be_visible()


def write_photos(directory: Path, count: int) -> list[Path]:
    paths = []
    for photo in batch_photos(count):
        path = directory / photo.filename
        path.write_bytes(photo.data)
        paths.append(path)
    return paths


def shot(page: Page, folder: Path | None, name: str) -> None:
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / f"{name}.png"))


def throttle_upload(context: BrowserContext, page: Page, bytes_per_second: int) -> None:
    session = context.new_cdp_session(page)
    session.send("Network.enable")
    session.send(
        "Network.emulateNetworkConditions",
        {
            "offline": False,
            "latency": 40,
            "downloadThroughput": -1,
            "uploadThroughput": bytes_per_second,
        },
    )


def main_flow(browser: Browser, base: str, count: int, readme: Path | None, states: Path | None):
    """The done-when flow. Returns the problems the browser reported."""
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    watch = Watch(page, base)

    page.goto(f"{base}/login")
    expect(page.get_by_role("heading", name="Sign in to Inspection")).to_be_visible()
    expect(page.get_by_text("Demo accounts")).to_be_visible()
    shot(page, readme, "login")
    shot(page, states, "01-login")

    sign_in(page, base, "Alpha Inspector")
    shot(page, states, "02-orgs")
    page.get_by_role("link", name=re.compile("Demo Org Alpha")).click()
    expect(page.get_by_role("heading", name="Demo Org Alpha projects")).to_be_visible()
    shot(page, states, "03-projects")
    page.get_by_role("link", name=re.compile(ALPHA_PROJECT)).click()
    expect(page.get_by_role("heading", name=ALPHA_PROJECT, level=1)).to_be_visible()
    expect(page.get_by_role("heading", name="Photos")).to_be_visible()
    expect(page.get_by_text("Loading photos…")).to_have_count(0, timeout=15000)
    shot(page, states, "04-project-start")

    with tempfile.TemporaryDirectory() as tmp:
        files = write_photos(Path(tmp), count)
        # Slow the uploads so the in-between state is on screen long enough to capture.
        throttle_upload(context, page, 700 * 1024)
        before = page.locator('ul[aria-label="Photos"] img').count()
        page.get_by_label(re.compile("choose files")).set_input_files([str(p) for p in files])

        panel = page.locator("section[aria-labelledby=batch-title]")
        expect(panel).to_be_visible()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            text = panel.inner_text()
            if "Uploading" in text and ("Processing" in text or "Done" in text or "Checking" in text):
                break
            time.sleep(0.15)
        shot(page, readme, "uploading")
        shot(page, states, "05-uploading")
        throttle_upload(context, page, -1)

        expect(page.get_by_text(f"All {count} photos uploaded and processed.")).to_be_visible(
            timeout=180000
        )
        shot(page, states, "06-batch-done")

        grid = page.locator('ul[aria-label="Photos"] img')
        expect(grid).to_have_count(before + count, timeout=30000)
        loaded = page.evaluate(
            "() => [...document.querySelectorAll('ul[aria-label=Photos] img')]"
            ".every(img => img.complete && img.naturalWidth > 0)"
        )
        assert loaded, "some thumbnails did not load"
        page.get_by_role("button", name="Dismiss").click()
        page.mouse.move(0, 0)
        shot(page, readme, "grid")
        shot(page, states, "07-grid")

    # The grid must have loaded nothing but thumbnails, from this origin or the store.
    for url in watch.images:
        assert url.startswith((base, STORE_ORIGIN)), f"image from an unexpected origin: {url}"
        if url.startswith(STORE_ORIGIN):
            assert "/thumb.jpg" in url, f"the grid loaded something that is not a thumbnail: {url}"
    thumbs = [u for u in watch.images if "/thumb.jpg" in u]
    print(f"grid: {len(thumbs)} thumbnail requests, no original loaded")
    context.close()
    return watch.problems


def state_tour(browser: Browser, base: str, states: Path) -> list[str]:
    """Every other page and state, for the design review."""
    problems: list[str] = []

    # Sign-in failure.
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    watch = Watch(page, base)
    page.goto(f"{base}/login")
    page.get_by_label("Email").fill("alpha.inspector@alpha.example")
    page.get_by_label("Password").fill("not the password")
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_text("don't match an account")).to_be_visible()
    shot(page, states, "08-login-error")
    context.close()
    problems += watch.problems

    # Loading, error and empty project; refused files.
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    watch = Watch(page, base)
    sign_in(page, base, "Alpha Inspector")
    page.get_by_role("link", name=re.compile("Demo Org Alpha")).click()
    page.get_by_role("link", name=re.compile(SCRATCH_PROJECT)).click()
    expect(page.get_by_role("heading", name=SCRATCH_PROJECT, level=1)).to_be_visible()

    # Refused files: wrong type and too big stop in the browser; a fake photo is
    # refused by the API's content check.
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        (directory / "site-notes.gif").write_bytes(b"GIF89a" + b"\x00" * 100)
        (directory / "panorama.jpg").write_bytes(b"\x00" * (51 * 1024 * 1024))
        (directory / "not-really-a-photo.png").write_bytes(b"<html>this is not a png</html>" * 20)
        (directory / "empty.jpg").write_bytes(b"")
        good = write_photos(directory, 1)
        page.get_by_label(re.compile("choose files")).set_input_files(
            [str(directory / n) for n in ("site-notes.gif", "panorama.jpg", "not-really-a-photo.png",
                                          "empty.jpg")] + [str(good[0])]
        )
        expect(page.get_by_text("contents don't match its type")).to_be_visible(timeout=30000)
        expect(page.get_by_text("Done", exact=True).first).to_be_visible(timeout=90000)
        page.mouse.move(0, 0)
        shot(page, states, "10-refused-files")
    problems += watch.problems

    # The empty state, as the owner of the other org.
    page.get_by_role("button", name="Sign out").click()
    sign_in(page, base, "Beta Owner")
    open_project(page, EMPTY_PROJECT[1], EMPTY_PROJECT[0])
    expect(page.get_by_text("No photos yet")).to_be_visible()
    shot(page, states, "09-project-empty")
    page.get_by_role("button", name="Sign out").click()
    sign_in(page, base, "Alpha Inspector")
    open_project(page, SCRATCH_PROJECT)

    # A project that loads slowly, then one that fails.
    page.route(re.compile(r".*/photos\?limit.*"), lambda route: route.abort())
    page.reload()
    expect(page.get_by_role("button", name="Try again").first).to_be_visible(timeout=15000)
    shot(page, states, "11-photos-error")
    page.unroute(re.compile(r".*/photos\?limit.*"))
    page.get_by_role("button", name="Try again").first.click()

    delayed = {"on": True}

    def slow(route):
        if delayed["on"]:
            time.sleep(2.5)
        route.continue_()

    page.route(re.compile(r".*/photos\?limit.*"), slow)
    page.reload()
    page.wait_for_timeout(700)
    shot(page, states, "12-photos-loading")
    delayed["on"] = False
    context.close()

    # A viewer: may look, may not upload.
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    watch = Watch(page, base)
    sign_in(page, base, "Alpha Viewer")
    open_project(page, ALPHA_PROJECT)
    expect(page.get_by_text("view photos but not upload")).to_be_visible()
    page.wait_for_timeout(800)
    shot(page, states, "13-viewer")
    context.close()
    problems += watch.problems

    # Narrow and dark.
    context = browser.new_context(viewport=PHONE, device_scale_factor=2)
    page = context.new_page()
    watch = Watch(page, base)
    page.goto(f"{base}/login")
    shot(page, states, "14-phone-login")
    sign_in(page, base, "Alpha Inspector")
    open_project(page, ALPHA_PROJECT)
    page.wait_for_timeout(1200)
    shot(page, states, "15-phone-project")
    context.close()
    problems += watch.problems

    context = browser.new_context(viewport=VIEWPORT, color_scheme="dark")
    page = context.new_page()
    watch = Watch(page, base)
    page.goto(f"{base}/login")
    shot(page, states, "16-dark-login")
    sign_in(page, base, "Alpha Inspector")
    open_project(page, ALPHA_PROJECT)
    page.wait_for_timeout(1200)
    shot(page, states, "17-dark-project")
    context.close()
    return problems + watch.problems


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:4700")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--screenshots", type=Path, help="save the three README screenshots here")
    parser.add_argument("--states", type=Path, help="save a screenshot of every state here")
    args = parser.parse_args()
    # A shared host can be slow; a page that takes seconds is not a failure.
    expect.set_options(timeout=20_000)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            problems = main_flow(browser, args.base, args.count, args.screenshots, args.states)
            if args.states:
                problems += state_tour(browser, args.base, args.states)
        finally:
            browser.close()

    for problem in problems:
        print(f"PROBLEM: {problem}")
    print("walkthrough: ok" if not problems else "walkthrough: FAILED")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
