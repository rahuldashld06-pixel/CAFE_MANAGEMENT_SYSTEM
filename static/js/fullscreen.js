/*
 * Fill the screen.
 *
 * A page cannot put itself full screen when it loads. Every browser
 * requires a user gesture for that, because otherwise any page could take
 * over the display the moment it opened. So this does the two things that
 * are actually possible:
 *
 *   1. A button in the top bar. One tap fills the screen; Escape, the
 *      phone's back gesture, or the button again brings the browser back.
 *
 *   2. It remembers. Once the button has been used on a device, the next
 *      visit goes full screen on the first tap anywhere - the earliest
 *      moment a browser will allow it. Leaving full screen forgets the
 *      preference, so pressing Escape means what it looks like it means
 *      rather than being undone by the next click.
 *
 * For a screen that is full the instant it opens, with nothing to tap at
 * all, the site has to be installed: "Install" in a desktop browser, "Add
 * to Home Screen" on a phone. The manifest asks for fullscreen display, so
 * an installed copy has no address bar and no browser chrome. That is also
 * the only route on an iPhone, where Safari has no Fullscreen API.
 */
(function () {
    "use strict";

    var KEY = "cafe.fullscreen";

    var root = document.documentElement;

    function supported() {
        return !!(root.requestFullscreen || root.webkitRequestFullscreen);
    }

    function active() {
        return !!(document.fullscreenElement
                  || document.webkitFullscreenElement);
    }

    function remembered() {
        try {
            return window.localStorage.getItem(KEY) === "1";
        } catch (error) {
            return false;       // private window, or storage blocked
        }
    }

    function remember(on) {
        try {
            if (on) {
                window.localStorage.setItem(KEY, "1");
            } else {
                window.localStorage.removeItem(KEY);
            }
        } catch (error) {
            /* Nothing to do - it just will not persist. */
        }
    }

    function enter() {
        var request = root.requestFullscreen || root.webkitRequestFullscreen;
        if (!request) return;
        try {
            // navigationUI hides what the platform will let us hide.
            var result = request.call(root, { navigationUI: "hide" });
            if (result && result.catch) {
                result.catch(function () { /* refused; nothing broke */ });
            }
        } catch (error) {
            /* Some browsers reject the options argument. */
            try {
                request.call(root);
            } catch (ignored) { /* give up quietly */ }
        }
    }

    function leave() {
        var exit = document.exitFullscreen || document.webkitExitFullscreen;
        if (!exit) return;
        try {
            var result = exit.call(document);
            if (result && result.catch) {
                result.catch(function () { /* already out */ });
            }
        } catch (error) { /* already out */ }
    }

    function paint() {
        var button = document.getElementById("fullscreenBtn");
        if (!button) return;

        var on = active();
        button.setAttribute("aria-pressed", on ? "true" : "false");
        var label = on ? "Leave full screen" : "Fill the screen";
        button.setAttribute("aria-label", label);
        button.title = label;
    }

    function start() {
        var button = document.getElementById("fullscreenBtn");

        if (!supported()) {
            // Leave it hidden. On an iPhone the installed app is the only
            // way to a full screen, and a button that does nothing is
            // worse than no button.
            return;
        }

        if (button) {
            button.hidden = false;
            button.addEventListener("click", function () {
                if (active()) {
                    leave();
                } else {
                    enter();
                    remember(true);
                }
            });
        }

        // Whichever way the screen was left - the button, Escape, the back
        // gesture - the state is read back from the browser rather than
        // assumed, and leaving forgets the preference.
        document.addEventListener("fullscreenchange", function () {
            if (!active()) remember(false);
            paint();
        });
        document.addEventListener("webkitfullscreenchange", function () {
            if (!active()) remember(false);
            paint();
        });

        paint();

        // The earliest a browser will allow it on a fresh page load.
        if (remembered()) {
            var onFirstGesture = function () {
                document.removeEventListener("pointerdown", onFirstGesture);
                document.removeEventListener("keydown", onFirstGesture);
                if (!active() && remembered()) enter();
            };
            document.addEventListener("pointerdown", onFirstGesture);
            document.addEventListener("keydown", onFirstGesture);
        }
    }

    // Registered outside instant.js's page scope, like the other shell
    // controls: the top bar is never swapped out, and listeners registered
    // the ordinary way from a deferred script are torn down on the first
    // navigation.
    if (window.Instant && window.Instant.shell) {
        window.Instant.shell(start);
    } else if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
}());
