/*
 * Instant navigation for Cafe Manager.
 *
 * The app is server rendered, so every sidebar click used to throw away the
 * whole document and rebuild it: re-parse style.css, re-fetch the icon font,
 * re-run every script, repaint from white. On a cafe counter that reads as
 * lag between taking one order and starting the next.
 *
 * This layer keeps the shell (sidebar, topbar, order-status popup) alive and
 * swaps only #page-view:
 *
 *   1. Pages linked from the sidebar are fetched in the background while the
 *      counter is idle, so the common screens are already in memory.
 *   2. Hovering or touching any in-app link fetches that page too.
 *   3. Clicking swaps the cached HTML in immediately - no network wait, no
 *      white flash, no re-parsing of CSS or fonts.
 *
 * Anything unexpected (a redirect to /login, a non-HTML response, a network
 * failure, a page with no #page-view) falls back to a normal browser
 * navigation, so the app can never end up stuck.
 *
 * NOTE for page templates: scripts inside {% block content %} are re-executed
 * every time their page is swapped back in, in global scope. Declare
 * top-level things with `function` or `var`, never `const`/`let`/`class`,
 * or the second visit throws "already declared".
 */
(function () {
    "use strict";

    var VIEW_ID = "page-view";
    var VIEW_START = "<!--pv:start-->";
    var VIEW_END = "<!--pv:end-->";
    var NAV_START = "<!--nav:start-->";
    var NAV_END = "<!--nav:end-->";
    var FRESH_MS = 45000;          // a cached page younger than this is used as-is
    var MAX_ENTRIES = 24;
    var PROGRESS_DELAY_MS = 120;   // don't flash a bar for near-instant loads

    // Never fetch these speculatively and never swap them in. They either
    // change server state on a plain GET (logout, delete, cancel, complete,
    // toggle, pay) or are not an app page at all (JSON, images, the
    // sign-in shell, which has no #page-view of its own).
    var UNSAFE = new RegExp(
        "(^/(api|media|static|healthz|login|logout|register|forgot-password)(/|$))" +
        "|(/(delete|cancel|complete|toggle|pay|mark-paid|resend-otp|verify|webhook)(/|$))"
    );

    // Screens the counter opens most, warmed in this order.
    var WARM_ORDER = ["/orders/add", "/orders", "/billing"];

    var rawSetTimeout = window.setTimeout;
    var rawSetInterval = window.setInterval;
    var rawClearTimeout = window.clearTimeout;
    var rawClearInterval = window.clearInterval;
    var rawAdd = EventTarget.prototype.addEventListener;
    var rawRemove = EventTarget.prototype.removeEventListener;
    var rawFetch = window.fetch;

    // ------------------------------------------------------------------
    // Page scope
    //
    // A swap replaces the markup but not the JavaScript world around it, so
    // whatever a page's own scripts registered has to be undone by hand.
    // Without this, leaving the dashboard leaves its 5-second poll running
    // against elements that no longer exist, and coming back to Billing adds
    // a second copy of its delegated submit handler - which would post every
    // payment twice.
    // ------------------------------------------------------------------
    var tracking = false;
    var timers = [];
    var listeners = [];
    var readyQueue = null;   // collects DOMContentLoaded handlers during a swap

    function beginPage() {
        for (var i = 0; i < timers.length; i++) {
            if (timers[i][0] === "i") {
                rawClearInterval.call(window, timers[i][1]);
            } else {
                rawClearTimeout.call(window, timers[i][1]);
            }
        }
        timers = [];

        for (var j = 0; j < listeners.length; j++) {
            try {
                rawRemove.call(listeners[j][0], listeners[j][1], listeners[j][2], listeners[j][3]);
            } catch (error) { /* listener already gone */ }
        }
        listeners = [];

        tracking = true;
    }

    // Runs fn with tracking off, for shell code that must outlive every page.
    function untracked(fn) {
        var was = tracking;
        tracking = false;
        try {
            return fn();
        } finally {
            tracking = was;
        }
    }

    window.setTimeout = function (fn, delay) {
        var id = rawSetTimeout.apply(window, arguments);
        if (tracking) timers.push(["t", id]);
        return id;
    };

    window.setInterval = function (fn, delay) {
        var id = rawSetInterval.apply(window, arguments);
        if (tracking) timers.push(["i", id]);
        return id;
    };

    function shimListeners(target) {
        var original = target.addEventListener.bind(target);
        target.addEventListener = function (type, handler, options) {
            // A swapped-in page never sees DOMContentLoaded again - the
            // document loaded long ago - so its initialisers are collected
            // and run by hand once the new markup is in place.
            if (readyQueue && type === "DOMContentLoaded") {
                readyQueue.push(handler);
                return undefined;
            }
            if (tracking) listeners.push([target, type, handler, options]);
            return original(type, handler, options);
        };
    }

    shimListeners(document);
    shimListeners(window);

    // A write invalidates every cached read. Native form posts reload the
    // page and take the cache with them; the in-page fetch() posts (creating
    // an order, marking a bill paid) have to say so themselves.
    window.fetch = function (input, init) {
        var method = "GET";
        if (init && init.method) method = init.method;
        else if (input && typeof input !== "string" && input.method) method = input.method;
        method = String(method).toUpperCase();

        var result = rawFetch.apply(window, arguments);

        if (method !== "GET" && method !== "HEAD") {
            result.then(function () { cache.clear(); }, function () {});
        }
        return result;
    };

    // ------------------------------------------------------------------
    // Page cache
    // ------------------------------------------------------------------
    var cache = new Map();
    var inflight = new Map();

    function store(url, html) {
        cache.set(url, { html: html, at: Date.now() });
        if (cache.size > MAX_ENTRIES) {
            cache.delete(cache.keys().next().value);
        }
    }

    function stripHash(url) {
        var index = url.indexOf("#");
        return index === -1 ? url : url.slice(0, index);
    }

    function load(url, speculative) {
        if (inflight.has(url)) return inflight.get(url);

        var headers = { "Accept": "text/html", "X-Instant-Nav": "1" };
        if (speculative) headers["X-Instant-Prefetch"] = "1";

        var request = rawFetch.call(window, url, {
            credentials: "same-origin",
            headers: headers,
            redirect: "follow"
        }).then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);

            var type = response.headers.get("Content-Type") || "";
            if (type.indexOf("text/html") === -1) throw new Error("not a page");

            // A redirect means the server sent us somewhere else - an expired
            // session, a role that cannot see this page. Let the browser
            // handle that properly rather than swapping in the wrong screen.
            if (response.redirected && stripHash(response.url) !== stripHash(url)) {
                throw new Error("redirected");
            }
            return response.text();
        }).then(function (html) {
            store(url, html);
            return html;
        });

        inflight.set(url, request);
        request.catch(function () {}).then(function () { inflight.delete(url); });
        return request;
    }

    // ------------------------------------------------------------------
    // Swapping
    // ------------------------------------------------------------------
    function currentView() {
        return document.getElementById(VIEW_ID);
    }

    function runScripts(root, done) {
        var scripts = Array.prototype.slice.call(root.querySelectorAll("script"));
        var index = 0;

        function next() {
            if (index >= scripts.length) { done(); return; }

            var original = scripts[index++];
            var fresh = document.createElement("script");

            for (var i = 0; i < original.attributes.length; i++) {
                fresh.setAttribute(original.attributes[i].name, original.attributes[i].value);
            }

            if (original.src) {
                fresh.async = false;
                fresh.onload = fresh.onerror = next;
                original.parentNode.replaceChild(fresh, original);
            } else {
                fresh.textContent = original.textContent;
                original.parentNode.replaceChild(fresh, original);
                next();
            }
        }

        next();
    }

    // The swap region is sliced out of the raw HTML between its markers, not
    // read off a parsed tree. A page with one stray </div> - and several here
    // have one - makes the parser close #page-view early, which would leave
    // that page's scripts outside the region and silently un-run. Assigning
    // the slice as innerHTML instead makes the container authoritative: an
    // unmatched end tag inside a fragment is simply ignored.
    function between(html, start, end) {
        var from = html.indexOf(start);
        if (from === -1) return null;
        var to = html.lastIndexOf(end);
        if (to === -1 || to < from) return null;
        return html.slice(from + start.length, to);
    }

    function titleOf(html) {
        var match = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html);
        if (!match) return null;
        var decoder = document.createElement("textarea");
        decoder.innerHTML = match[1];
        return decoder.value.trim();
    }

    function swap(html, url) {
        var viewHtml = between(html, VIEW_START, VIEW_END);
        var live = currentView();
        if (viewHtml === null || !live) return false;

        beginPage();
        readyQueue = [];

        // The sidebar marks the active section server-side, so take its
        // rendered state rather than re-deriving the rules here.
        var navHtml = between(html, NAV_START, NAV_END);
        var liveNav = document.querySelector(".sidebar-nav");
        if (navHtml !== null && liveNav) liveNav.innerHTML = navHtml;

        var title = titleOf(html);
        if (title) document.title = title;

        // On the first, fully parsed load an early-closed #page-view leaves
        // the rest of that page as siblings. Clear them, or they would sit
        // under every page swapped in afterwards.
        while (live.nextSibling) live.parentNode.removeChild(live.nextSibling);

        live.innerHTML = viewHtml;

        runScripts(live, function () {
            var queued = readyQueue || [];
            readyQueue = null;

            for (var i = 0; i < queued.length; i++) {
                try {
                    queued[i].call(document, new Event("DOMContentLoaded"));
                } catch (error) {
                    console.error(error);
                }
            }

            if (window.CafeShell && window.CafeShell.applyCsrf) {
                window.CafeShell.applyCsrf(live);
            }
            document.dispatchEvent(new CustomEvent("instant:load", { detail: { url: url } }));
        });

        return true;
    }

    // ------------------------------------------------------------------
    // Progress bar - only for pages that were not already warmed
    // ------------------------------------------------------------------
    var bar = null;

    function showProgress() {
        if (!bar) {
            bar = document.createElement("div");
            bar.className = "instant-progress";
            document.body.appendChild(bar);
        }
        bar.classList.add("instant-progress--on");
    }

    function hideProgress() {
        if (bar) bar.classList.remove("instant-progress--on");
    }

    // ------------------------------------------------------------------
    // Navigation
    // ------------------------------------------------------------------
    var navToken = 0;

    function safePath(pathname) {
        return !UNSAFE.test(pathname);
    }

    // Give up on the instant path and let the browser do it properly.
    // Assigning location.href to the address already showing is a no-op in
    // some browsers, so the same-URL case (a failed Back, a failed refresh)
    // has to reload explicitly.
    function hardNavigate(url) {
        if (stripHash(url) === stripHash(location.href)) window.location.reload();
        else window.location.href = url;
    }

    function saveScroll() {
        try {
            var state = history.state || {};
            state.instant = true;
            state.scroll = window.scrollY;
            history.replaceState(state, "", location.href);
        } catch (error) { /* history unavailable */ }
    }

    function apply(html, url, options) {
        hideProgress();

        if (!swap(html, url)) {
            hardNavigate(url);
            return;
        }

        if (!options.pop) {
            // Re-clicking the section you are already on refreshes it; it
            // should not stack another entry the Back button has to chew
            // through.
            if (stripHash(url) === stripHash(location.href)) {
                history.replaceState({ instant: true, scroll: 0 }, "", url);
            } else {
                history.pushState({ instant: true, scroll: 0 }, "", url);
            }
        }
        window.scrollTo(0, options.scroll || 0);
    }

    function visit(url, options) {
        options = options || {};
        var token = ++navToken;

        if (!options.pop) saveScroll();

        var cached = cache.get(url);
        if (cached) {
            apply(cached.html, url, options);
            if (Date.now() - cached.at > FRESH_MS) revalidate(url);
            return;
        }

        var progressTimer = rawSetTimeout.call(window, function () {
            if (token === navToken) showProgress();
        }, PROGRESS_DELAY_MS);

        load(url, false).then(function (html) {
            rawClearTimeout.call(window, progressTimer);
            if (token !== navToken) return;
            apply(html, url, options);
        }).catch(function () {
            rawClearTimeout.call(window, progressTimer);
            if (token !== navToken) return;
            hideProgress();
            hardNavigate(url);
        });
    }

    // A stale page is shown instantly and refreshed underneath. The refresh
    // is dropped if the cashier has already started typing into it, so a
    // half-entered cash amount is never wiped by a background update.
    function revalidate(url) {
        var token = navToken;
        var previous = cache.get(url);

        load(url, true).then(function (html) {
            if (token !== navToken) return;
            if (location.href !== url) return;
            if (previous && previous.html === html) return;

            var active = document.activeElement;
            var live = currentView();
            if (active && live && live.contains(active)) {
                var tag = active.tagName;
                if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
            }
            if (document.querySelector(".order-status-overlay.open, .order-summary-overlay.open")) return;

            var scroll = window.scrollY;
            if (swap(html, url)) window.scrollTo(0, scroll);
        }).catch(function () {});
    }

    function instantLink(link) {
        if (link.target && link.target !== "_self") return false;
        if (link.hasAttribute("download")) return false;
        if (link.hasAttribute("data-no-instant")) return false;
        if (link.origin !== location.origin) return false;

        var href = link.getAttribute("href") || "";
        if (!href || href.charAt(0) === "#") return false;
        if (/^(mailto|tel|javascript):/i.test(href)) return false;
        if (link.hash && stripHash(link.href) === stripHash(location.href)) return false;

        return safePath(link.pathname);
    }

    function linkFrom(event) {
        var target = event.target;
        if (!target || !target.closest) return null;
        return target.closest("a[href]");
    }

    function prefetch(url) {
        if (!url) return;
        if (cache.has(url) || inflight.has(url)) return;
        try {
            if (!safePath(new URL(url, location.href).pathname)) return;
        } catch (error) {
            return;
        }
        load(url, true).catch(function () {});
    }

    rawAdd.call(document, "click", function (event) {
        if (event.defaultPrevented || event.button !== 0) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;

        var link = linkFrom(event);
        if (!link || !instantLink(link)) return;

        event.preventDefault();
        visit(link.href, {});
    });

    // Filter forms (Billing history, Reports) are plain GET submits, so they
    // can go through the same instant path instead of reloading the shell.
    rawAdd.call(document, "submit", function (event) {
        if (event.defaultPrevented) return;

        var form = event.target;
        if (!form || form.hasAttribute("data-no-instant")) return;
        if ((form.getAttribute("method") || "get").toLowerCase() !== "get") return;

        var action;
        try {
            action = new URL(form.getAttribute("action") || location.href, location.href);
        } catch (error) {
            return;
        }
        if (action.origin !== location.origin || !safePath(action.pathname)) return;

        try {
            // Throws on a form carrying a file input; such a form is left to
            // the browser rather than half-serialised into a query string.
            action.search = new URLSearchParams(new FormData(form)).toString();
        } catch (error) {
            return;
        }

        event.preventDefault();
        visit(action.href, {});
    });

    rawAdd.call(document, "mouseover", function (event) {
        var link = linkFrom(event);
        if (link && instantLink(link)) prefetch(link.href);
    });

    rawAdd.call(document, "touchstart", function (event) {
        var link = linkFrom(event);
        if (link && instantLink(link)) prefetch(link.href);
    }, { passive: true });

    rawAdd.call(window, "popstate", function (event) {
        if (!safePath(location.pathname)) { window.location.reload(); return; }
        visit(location.href, {
            pop: true,
            scroll: (event.state && event.state.scroll) || 0
        });
    });

    // ------------------------------------------------------------------
    // Background warming
    // ------------------------------------------------------------------
    function idle(fn) {
        if (window.requestIdleCallback) window.requestIdleCallback(fn, { timeout: 2000 });
        else rawSetTimeout.call(window, fn, 150);
    }

    function warmRank(url) {
        var path = new URL(url, location.href).pathname.replace(/\/$/, "");
        var index = WARM_ORDER.indexOf(path);
        return index === -1 ? WARM_ORDER.length : index;
    }

    // Walk the sidebar the server rendered for this user, so a cashier never
    // warms (or is even offered) the admin-only screens.
    function warmSidebar() {
        var links = document.querySelectorAll(".sidebar-nav a[href]");
        var urls = [];

        for (var i = 0; i < links.length; i++) {
            var link = links[i];
            if (!instantLink(link)) continue;
            if (stripHash(link.href) === stripHash(location.href)) continue;
            if (urls.indexOf(link.href) === -1) urls.push(link.href);
        }

        urls.sort(function (a, b) { return warmRank(a) - warmRank(b); });

        // One at a time: the managed MySQL plan hands out only a handful of
        // connections, and warming must never compete with the page the user
        // is actually looking at.
        var index = 0;
        function step() {
            if (index >= urls.length) return;

            var url = urls[index++];
            var cached = cache.get(url);
            if (cached && Date.now() - cached.at < FRESH_MS) { idle(step); return; }

            load(url, true).catch(function () {}).then(function () { idle(step); });
        }
        idle(step);
    }

    if ("scrollRestoration" in history) history.scrollRestoration = "manual";

    rawAdd.call(window, "load", function () {
        try {
            history.replaceState({ instant: true, scroll: 0 }, "", location.href);
        } catch (error) { /* history unavailable */ }

        rawSetTimeout.call(window, warmSidebar, 300);
    });

    rawAdd.call(document, "instant:load", function () {
        rawSetTimeout.call(window, warmSidebar, 300);
    });

    // Registers shell code - sidebar, topbar, order-status popup - that must
    // outlive every navigation. Both the registration and the callback run
    // outside the page scope, so nothing here is torn down on a swap.
    function shell(fn) {
        function run() { untracked(fn); }
        if (document.readyState === "loading") rawAdd.call(document, "DOMContentLoaded", run);
        else run();
    }

    window.Instant = {
        beginPage: beginPage,
        untracked: untracked,
        shell: shell,
        prefetch: prefetch,
        visit: visit,
        invalidate: function () { cache.clear(); }
    };
})();
