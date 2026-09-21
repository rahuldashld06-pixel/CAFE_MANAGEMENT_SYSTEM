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
    var BRAND_START = "<!--brand:start-->";
    var BRAND_END = "<!--brand:end-->";
    var TOPBRAND_START = "<!--tbrand:start-->";
    var TOPBRAND_END = "<!--tbrand:end-->";
    var NAV_START = "<!--nav:start-->";
    var NAV_END = "<!--nav:end-->";
    // A cached page is always painted immediately. These say how long that
    // copy is trusted before a fresh one is fetched underneath it.
    //
    // REVALIDATE_AFTER_MS is short on purpose: returning to Orders or the
    // menu should show what is true now, not what was true the last time
    // you looked. The cached copy still appears instantly - the refresh
    // lands a round trip later and only redraws if something changed.
    var REVALIDATE_AFTER_MS = 1500;

    // How long a warmed page is left alone before the background warm-up
    // bothers to fetch it again.
    var FRESH_MS = 45000;
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

    // Every screen in the sidebar is warmed after sign-in, in this order -
    // the ones the counter opens most first, then the rest, then anything
    // not named here. They go one at a time and only while the browser is
    // idle, so the managed database's handful of connections are never all
    // busy at once and warming never competes with the page in front of
    // the user. Whoever is signed in only warms what their own sidebar
    // offers, so a cashier never touches the admin screens.
    var WARM_ORDER = [
        "/orders/add", "/kitchen", "/billing", "/foods", "/inventory",
        "/categories", "", "/reports", "/users"
    ];
    // One past the end, so a page not named above is still warmed, last.
    var WARM_LIMIT = WARM_ORDER.length + 1;

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
        // Before anything is unhooked, so a page's own cleanup still has
        // its listeners. A page that changed something outside itself -
        // the colour picker paints the whole app while you drag it - puts
        // it back here, or the preview would follow you to the next page.
        try {
            document.dispatchEvent(new CustomEvent("instant:teardown"));
        } catch (error) { /* a page's cleanup is never worth a failed swap */ }

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
    //
    // But only a write somebody made. The app talks to itself all day: the
    // kitchen screen says it is still switched on every twenty seconds, a
    // till claims a ticket nobody has printed, the tour records that it has
    // been seen. Not one of those changes anything a page shows, and
    // throwing every cached page away for them is why switching pages went
    // back to costing a round trip within seconds of the warm-up finishing.
    // Measured: a page that painted in 1ms took 258ms after one of them,
    // and that was with the database next door.
    var HOUSEKEEPING = new RegExp(
        "^/api/(kitchen/(heartbeat|claim)|tutorial/seen)(/|$)"
    );

    function pathOf(input) {
        var href = typeof input === "string"
            ? input
            : (input && input.url) || "";
        try {
            return new URL(href, location.href).pathname;
        } catch (error) {
            return "";
        }
    }

    window.fetch = function (input, init) {
        var method = "GET";
        if (init && init.method) method = init.method;
        else if (input && typeof input !== "string" && input.method) method = input.method;
        method = String(method).toUpperCase();

        var result = rawFetch.apply(window, arguments);

        if (method !== "GET" && method !== "HEAD"
                && !HOUSEKEEPING.test(pathOf(input))) {
            result.then(function () {
                cache.clear();
                rewarmSoon();
            }, function () {});
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

    // The accent colour is an attribute on <html>, which sits outside the
    // swapped region. Without this, an admin changing the theme would leave
    // everyone else's open session on the old colour until a full reload.
    function themeOf(html) {
        var tag = /<html[^>]*>/i.exec(html);
        if (!tag) return null;
        var match = /data-theme=["']([a-z0-9_-]+)["']/i.exec(tag[0]);
        return match ? match[1] : null;
    }

    // A cafe that has chosen its own colours carries them as custom
    // properties in an inline style on <html> - the same element the
    // theme rides on, and outside the swapped region for the same
    // reason. Without this an admin would save a new background and
    // watch nothing happen.
    //
    // Only custom properties, and only ones whose value is a colour.
    // This markup comes from our own server, but copying an entire style
    // attribute across on trust is a wider door than the job needs.
    var COLOUR_NAME = /^--[a-z0-9-]+$/;
    var COLOUR_VALUE = /^(#[0-9a-f]{3,8}|[0-9]{1,3}(\s*,\s*[0-9]{1,3}){2})$/i;

    function coloursOf(html) {
        var found = {};
        var tag = /<html[^>]*>/i.exec(html);
        if (!tag) return found;

        var style = /\sstyle="([^"]*)"/i.exec(tag[0]);
        if (!style) return found;

        var parts = style[1].split(";");
        for (var i = 0; i < parts.length; i++) {
            var at = parts[i].indexOf(":");
            if (at < 0) continue;

            var name = parts[i].slice(0, at).trim().toLowerCase();
            var value = parts[i].slice(at + 1).trim();
            if (COLOUR_NAME.test(name) && COLOUR_VALUE.test(value)) {
                found[name] = value;
            }
        }
        return found;
    }

    function applyColours(html) {
        var wanted = coloursOf(html);
        var root = document.documentElement;

        // Anything the new page does not ask for comes off. Otherwise a
        // cafe going back to one of the six presets would keep the
        // background it had chosen, with no way to be rid of it.
        var had = (root.getAttribute("style") || "").split(";");
        for (var i = 0; i < had.length; i++) {
            var name = had[i].split(":")[0].trim().toLowerCase();
            if (name && COLOUR_NAME.test(name)
                    && !Object.prototype.hasOwnProperty.call(wanted, name)) {
                root.style.removeProperty(name);
            }
        }

        for (var key in wanted) {
            if (Object.prototype.hasOwnProperty.call(wanted, key)) {
                root.style.setProperty(key, wanted[key]);
            }
        }
    }

    // Copies one marked region of a fetched page over the live one. Used
    // for the parts of the shell that can change without the page region
    // changing - the cafe's name and its symbol.
    function replaceRegion(html, startMarker, endMarker, selector) {
        var fresh = between(html, startMarker, endMarker);
        var live = document.querySelector(selector);
        if (fresh === null || !live) return;
        if (live.innerHTML === fresh) return;    // nothing moved
        live.innerHTML = fresh;
    }

    function swap(html, url) {
        var viewHtml = between(html, VIEW_START, VIEW_END);
        var live = currentView();
        if (viewHtml === null || !live) return false;

        beginPage();
        readyQueue = [];

        // The name and symbol live outside the page region, so renaming
        // the cafe used to leave the old name in the corner until a full
        // reload. They come across with every swap now.
        replaceRegion(html, BRAND_START, BRAND_END, ".sidebar-brand");
        replaceRegion(html, TOPBRAND_START, TOPBRAND_END, ".topbar-brand");

        // The sidebar marks the active section server-side, so take its
        // rendered state rather than re-deriving the rules here.
        var navHtml = between(html, NAV_START, NAV_END);
        var liveNav = document.querySelector(".sidebar-nav");
        if (navHtml !== null && liveNav) liveNav.innerHTML = navHtml;

        var title = titleOf(html);
        if (title) document.title = title;

        var theme = themeOf(html);
        if (theme) document.documentElement.setAttribute("data-theme", theme);

        applyColours(html);

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
            if (Date.now() - cached.at > REVALIDATE_AFTER_MS) revalidate(url);
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

    // ------------------------------------------------------------------
    // Saving something
    //
    // A form post used to reload everything: the sidebar, the top bar,
    // every stylesheet and every script, to change one panel. Posts go
    // through the same swap as a link now, so the browser keeps the shell
    // it already has and only the page region is replaced.
    //
    // The server answers a post with a redirect to the page to show.
    // fetch follows it, and response.url is where it landed - that is what
    // both the swap and the address bar use, so Back still works and a
    // refresh does not re-post.
    // ------------------------------------------------------------------
    function onPost(event) {
        if (event.defaultPrevented) return;      // a confirm() said no,
                                                 // or a page handled it

        var form = event.target;
        if (!form || form.hasAttribute("data-no-instant")) return;
        if ((form.getAttribute("method") || "get").toLowerCase() !== "post") {
            return;                              // GET forms are handled above
        }

        // The sign-in, registration and reset screens are not app pages and
        // have no region to swap into.
        if (!currentView()) return;

        var action;
        try {
            action = new URL(form.getAttribute("action") || location.href,
                             location.href);
        } catch (error) {
            return;
        }
        if (action.origin !== location.origin) return;

        var body;
        try {
            body = new FormData(form);
        } catch (error) {
            return;
        }

        // A form with two buttons - Save and Remove - tells them apart by
        // the button's own name and value, which FormData does not include.
        var submitter = event.submitter || null;
        if (submitter && submitter.name) {
            body.append(submitter.name, submitter.value || "");
        }

        event.preventDefault();
        saveScroll();

        var token = ++navToken;
        if (submitter) submitter.disabled = true;   // no double posts

        var progressTimer = rawSetTimeout.call(window, function () {
            if (token === navToken) showProgress();
        }, PROGRESS_DELAY_MS);

        function release() {
            rawClearTimeout.call(window, progressTimer);
            if (submitter) submitter.disabled = false;
        }

        function giveUpToTheBrowser() {
            release();
            hideProgress();
            // Post it the ordinary way rather than losing what was typed.
            form.setAttribute("data-no-instant", "");
            if (form.requestSubmit) {
                form.requestSubmit(submitter);
            } else {
                form.submit();
            }
        }

        rawFetch.call(window, action.href, {
            method: "POST",
            body: body,
            credentials: "same-origin",
            redirect: "follow"
        }).then(function (response) {
            return response.text().then(function (html) {
                return {
                    html: html,
                    url: response.url || action.href,
                    ok: response.ok
                };
            });
        }).then(function (result) {
            // A write makes every cached read stale, and the refill
            // starts straight away so the next screen opened is not back
            // to paying full price for itself.
            cache.clear();
            rewarmSoon();
            release();
            if (token !== navToken) return;

            var landed;
            try {
                landed = new URL(result.url);
            } catch (error) {
                giveUpToTheBrowser();
                return;
            }

            if (!result.ok || !safePath(landed.pathname)) {
                hideProgress();
                hardNavigate(result.url);
                return;
            }

            apply(result.html, result.url, {});
        }).catch(function () {
            giveUpToTheBrowser();
        });
    }

    // Registered last, and moved back to last after every swap.
    //
    // Some pages post a form themselves - Billing updates one row in place
    // rather than replacing the page - and they do that by handling the
    // submit and calling preventDefault. Listeners fire in the order they
    // were added, so this one, added when instant.js loads, would have run
    // before theirs and posted the form a second time. Re-adding it after
    // each page's own scripts have run puts it behind them, where a page
    // that wants to handle its own form always wins.
    function takeLastTurnOnPosts() {
        rawRemove.call(document, "submit", onPost);
        rawAdd.call(document, "submit", onPost);
    }

    takeLastTurnOnPosts();
    if (document.readyState === "loading") {
        rawAdd.call(document, "DOMContentLoaded", takeLastTurnOnPosts);
    }
    rawAdd.call(document, "instant:load", takeLastTurnOnPosts);

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
    // includeCurrent: normally there is no point fetching the page already
    // on screen. After a write there is - the cache was just emptied, and
    // the page the write happened on is the one somebody is most likely to
    // come straight back to. Leaving it out meant New Order, of all
    // screens, was the one that still cost a round trip after every order.
    function warmSidebar(includeCurrent) {
        var links = document.querySelectorAll(".sidebar-nav a[href]");
        var urls = [];

        for (var i = 0; i < links.length; i++) {
            var link = links[i];
            if (!instantLink(link)) continue;
            if (!includeCurrent
                    && stripHash(link.href) === stripHash(location.href)) {
                continue;
            }
            if (urls.indexOf(link.href) === -1) urls.push(link.href);
        }

        urls.sort(function (a, b) { return warmRank(a) - warmRank(b); });
        urls = urls.filter(function (url) { return warmRank(url) < WARM_LIMIT; });

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

    // Deliberately not re-warmed after every navigation. Cached pages stay
    // put, hovering or touching a link fetches whatever is missing - and
    // re-warming on each swap only added server load for pages already in
    // hand.
    //
    // After a write is the one time it is worth doing, because the cache
    // was just emptied on purpose. Without this, saving one thing meant
    // every other screen went back to costing its full round trips until
    // somebody happened to visit it - which is the complaint that "if
    // anything changes on one page, why should the others suffer".
    //
    // Debounced, so a burst of saves refills once rather than nine times,
    // and idle-scheduled like the first warm-up, so it never competes with
    // the page in front of the user.
    var rewarmTimer = null;

    function rewarmSoon() {
        if (rewarmTimer) rawClearTimeout.call(window, rewarmTimer);
        rewarmTimer = rawSetTimeout.call(window, function () {
            rewarmTimer = null;
            warmSidebar(true);
        }, 500);
    }

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
        invalidate: function () { cache.clear(); rewarmSoon(); }
    };
})();
