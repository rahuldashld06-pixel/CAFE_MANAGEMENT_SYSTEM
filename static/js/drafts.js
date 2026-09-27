/*
 * Nothing typed is lost to a closed browser.
 *
 * Every form in the app keeps a copy of what has been typed into it, on
 * this device, written the moment it changes - not on a timer, so a
 * power cut costs at most the last keystroke. If the page goes before
 * the form is saved - the browser closed, the battery died, the site
 * fell over in the middle of a save - the form comes back filled in the
 * next time it is opened, with a line saying so, and all that is left
 * to do is press Save.
 *
 * Never kept: passwords, files, and the hidden fields a page carries
 * for itself, the CSRF token among them. A form or a single field can
 * opt out with data-no-draft.
 *
 * Kept per person, so on a till several people sign in to nobody is
 * shown somebody else's half-typed work; and for a week, the same as a
 * remembered sign-in.
 *
 * The hard part is knowing when a copy can be thrown away, because
 * bringing back something that was in fact saved invites saving it
 * twice. A copy goes only once the save is known to have landed:
 *
 *   - every save the server answers comes back with a short-lived
 *     cafora_post cookie saying "done" or "failed", and which address
 *     it was. "failed" is needed because an unexpected error also ends
 *     on another page - the dashboard - and would otherwise look like
 *     success;
 *   - "done" and a different page means it went: a successful save is
 *     answered by moving on, a refused one by coming back to the form;
 *   - no answer at all - the browser closed, the power went, the site
 *     was down - keeps the copy, and says when it is brought back that
 *     it may already have gone through.
 *
 * A page that posts a form by itself, like New Order, tells this file
 * how it went with Drafts.forget(form) and Drafts.keep(form).
 */
(function () {
    "use strict";

    var PREFIX = "cafora-draft:";
    var PENDING = "cafora-draft-pending";
    var ANSWER_COOKIE = "cafora_post";
    var KEEP_FOR_MS = 7 * 24 * 60 * 60 * 1000;

    // Nothing here is ever worth keeping, or safe to.
    var NEVER = { password: 1, file: 1, hidden: 1, submit: 1, button: 1,
                  reset: 1, image: 1 };

    function storage(kind) {
        try {
            var store = window[kind];
            store.setItem("cafora-probe", "1");
            store.removeItem("cafora-probe");
            return store;
        } catch (error) {
            return null;             // a private window with storage off
        }
    }

    var kept = storage("localStorage");
    var tab = storage("sessionStorage");
    if (!kept) return;

    var restoring = false;

    // ---- Which forms, which fields ---------------------------------

    function person() {
        var meta = document.querySelector('meta[name="cafora-user"]');
        return meta ? meta.getAttribute("content") || "" : "";
    }

    function region() {
        return document.getElementById("page-view");
    }

    function fieldsOf(form) {
        var out = [];
        for (var i = 0; i < form.elements.length; i++) {
            var el = form.elements[i];
            var type = (el.type || "").toLowerCase();
            if (!el.name || el.disabled || NEVER[type]) continue;
            if (el.tagName === "BUTTON" || el.tagName === "FIELDSET"
                    || el.tagName === "OUTPUT") continue;
            if (el.hasAttribute("data-no-draft")) continue;
            // Belt and braces: whatever it is called, not these.
            if (/password|csrf/i.test(el.name)) continue;
            out.push(el);
        }
        return out;
    }

    function draftable(form) {
        if (!form || form.tagName !== "FORM") return false;
        if ((form.getAttribute("method") || "get").toLowerCase() !== "post") {
            return false;            // a search or a filter, not work
        }
        if (form.hasAttribute("data-no-draft")) return false;
        var view = region();
        if (!view || !view.contains(form)) return false;
        return fieldsOf(form).length > 0;
    }

    function formsHere() {
        var view = region();
        if (!view) return [];
        return [].filter.call(view.querySelectorAll("form"), draftable);
    }

    function keyOf(form) {
        var name = form.id;
        if (!name && form.getAttribute("action")) {
            try {
                name = new URL(form.getAttribute("action"),
                               location.href).pathname;
            } catch (error) {
                name = "";
            }
        }
        if (!name) name = "#" + formsHere().indexOf(form);
        return PREFIX + person() + ":" + location.pathname + ":" + name;
    }

    // ---- Reading and writing a form --------------------------------

    function read(form) {
        var values = {};
        fieldsOf(form).forEach(function (el) {
            var type = (el.type || "").toLowerCase();
            var list = values[el.name] || (values[el.name] = []);
            if (type === "checkbox" || type === "radio") {
                if (el.checked) list.push(el.value);
            } else if (el.tagName === "SELECT" && el.multiple) {
                for (var i = 0; i < el.options.length; i++) {
                    if (el.options[i].selected) list.push(el.options[i].value);
                }
            } else {
                list.push(el.value);
            }
        });
        return values;
    }

    function put(form, values) {
        var seen = {};
        restoring = true;
        try {
            fieldsOf(form).forEach(function (el) {
                if (!Object.prototype.hasOwnProperty.call(values, el.name)) return;
                var wanted = values[el.name];
                var type = (el.type || "").toLowerCase();
                var changed = false;

                if (type === "checkbox" || type === "radio") {
                    var on = wanted.indexOf(el.value) > -1;
                    changed = el.checked !== on;
                    el.checked = on;
                } else if (el.tagName === "SELECT" && el.multiple) {
                    for (var i = 0; i < el.options.length; i++) {
                        var pick = wanted.indexOf(el.options[i].value) > -1;
                        if (el.options[i].selected !== pick) changed = true;
                        el.options[i].selected = pick;
                    }
                } else {
                    // Several fields can share a name; the nth value goes
                    // to the nth of them, as they were read.
                    var at = seen[el.name] || 0;
                    seen[el.name] = at + 1;
                    if (at < wanted.length && el.value !== wanted[at]) {
                        el.value = wanted[at];
                        changed = true;
                    }
                }

                // The page's own scripts - a running total, a character
                // count - hear about it the way they would from a person.
                if (changed) {
                    el.dispatchEvent(new Event("input", { bubbles: true }));
                    el.dispatchEvent(new Event("change", { bubbles: true }));
                }
            });
        } finally {
            restoring = false;
        }
        form.dispatchEvent(new CustomEvent("draft:restored", { bubbles: true }));
    }

    // "5" and "5.00" are the same tax rate; the server writes numbers back
    // its own way, and that must not read as a difference.
    function same(a, b) {
        a = String(a).trim();
        b = String(b).trim();
        if (a === b) return true;
        var x = Number(a), y = Number(b);
        return a !== "" && b !== "" && isFinite(x) && isFinite(y) && x === y;
    }

    function matches(draft, rendered) {
        for (var name in draft) {
            if (!Object.prototype.hasOwnProperty.call(draft, name)) continue;
            if (!Object.prototype.hasOwnProperty.call(rendered, name)) continue;
            var a = draft[name], b = rendered[name];
            if (a.length !== b.length) return false;
            for (var i = 0; i < a.length; i++) {
                if (!same(a[i], b[i])) return false;
            }
        }
        return true;
    }

    function load(key) {
        try {
            var record = JSON.parse(kept.getItem(key) || "null");
            return record && record.fields ? record : null;
        } catch (error) {
            return null;
        }
    }

    function save(key, record) {
        try {
            kept.setItem(key, JSON.stringify(record));
        } catch (error) {
            /* Full, or switched off since. Nothing to be done. */
        }
    }

    function drop(key) {
        try { kept.removeItem(key); } catch (error) { /* gone already */ }
    }

    function write(form, sent) {
        save(keyOf(form), {
            path: location.pathname,
            at: Date.now(),
            sent: !!sent,
            fields: read(form)
        });
    }

    function stateOf(form) {
        return form.__cafora || (form.__cafora = { rendered: null, frozen: 0 });
    }

    // A week, the same as a remembered sign-in. Past that it is a stale
    // guess about work somebody has long since redone.
    function prune() {
        var now = Date.now();
        for (var i = kept.length - 1; i >= 0; i--) {
            var key = kept.key(i);
            if (!key || key.indexOf(PREFIX) !== 0) continue;
            var record = load(key);
            if (!record || now - (record.at || 0) > KEEP_FOR_MS) drop(key);
        }
    }

    // ---- How the last save went ------------------------------------

    // What the server said about the last save, read once and cleared.
    function answer() {
        var found = new RegExp("(?:^|; )" + ANSWER_COOKIE + "=([^;]*)")
            .exec(document.cookie);
        if (!found) return null;
        document.cookie = ANSWER_COOKIE + "=; Max-Age=0; path=/";

        var raw = found[1].replace(/^"|"$/g, "");
        try { raw = decodeURIComponent(raw); } catch (error) { /* as is */ }
        var bar = raw.indexOf("|");
        if (bar < 0) return null;
        return { how: raw.slice(0, bar), path: raw.slice(bar + 1) };
    }

    function pending() {
        if (!tab) return null;
        try {
            return JSON.parse(tab.getItem(PENDING) || "null");
        } catch (error) {
            return null;
        }
    }

    function setPending(value) {
        if (!tab) return;
        try {
            if (value) tab.setItem(PENDING, JSON.stringify(value));
            else tab.removeItem(PENDING);
        } catch (error) { /* nothing to be done */ }
    }

    // Called once a sent form has an answer - on the page it landed on.
    function settle(landedPath) {
        var sent = pending();
        if (!sent) return;
        setPending(null);

        var told = answer();
        var record = load(sent.key);
        if (!record) return;

        // Nothing from the server about this save: it was never answered,
        // and the copy stays marked as sent.
        if (!told || told.path !== sent.action) return;

        if (told.how === "failed") {
            record.sent = false;
            record.how = "failed";
            save(sent.key, record);
        } else if (landedPath !== sent.path) {
            drop(sent.key);          // it went, and the server moved on
        } else {
            record.sent = false;
            record.how = "refused";
            save(sent.key, record);
        }
    }

    // ---- What a person sees ----------------------------------------

    var SAYS = {
        failed: "That save did not go through. What you typed is back in " +
                "the form - press Save to try again.",
        refused: "What you typed is back in the form. It was not saved - " +
                 "see the message above.",
        sent: "This was being saved when the page closed, and may already " +
              "have gone through. Check before saving it again.",
        typed: "Brought back what you had typed here before the page " +
               "closed. It is not saved until you press Save."
    };

    function tell(form, record) {
        var old = form.querySelector(".draft-note");
        if (old) old.remove();

        var note = document.createElement("div");
        note.className = "draft-note" + (record.sent ? " draft-note--warn" : "");
        note.setAttribute("role", "status");

        var icon = document.createElement("i");
        icon.className = "bi " + (record.sent ? "bi-exclamation-triangle"
                                              : "bi-arrow-counterclockwise");
        icon.setAttribute("aria-hidden", "true");

        var text = document.createElement("span");
        text.className = "draft-note__text";
        text.textContent = SAYS[record.how] || SAYS[record.sent ? "sent" : "typed"];

        var discard = document.createElement("button");
        discard.type = "button";
        discard.className = "draft-note__discard";
        discard.textContent = "Discard";
        discard.addEventListener("click", function () {
            var state = stateOf(form);
            if (state.rendered) put(form, state.rendered);
            drop(keyOf(form));
            note.remove();
        });

        note.appendChild(icon);
        note.appendChild(text);
        note.appendChild(discard);
        form.insertBefore(note, form.firstChild);
    }

    // ---- A page arriving -------------------------------------------

    function arrive() {
        settle(location.pathname);
        prune();

        formsHere().forEach(function (form) {
            var state = stateOf(form);
            // Once per form. A second pass would read the restored values
            // as the page's own, find the copy matches them, and throw it
            // away - the work would survive one crash and not two.
            if (state.arrived) return;
            state.arrived = true;
            state.rendered = read(form);
            state.frozen = 0;

            var key = keyOf(form);
            var record = load(key);
            if (!record) return;

            // The page already shows exactly this: it was saved, or it
            // was put back by the server itself. Nothing to bring back.
            if (matches(record.fields, state.rendered)) {
                drop(key);
                return;
            }

            put(form, record.fields);
            tell(form, record);
        });
    }

    // ---- Typing, sending -------------------------------------------

    function onChange(event) {
        if (restoring) return;
        var form = event.target && event.target.form;
        if (!draftable(form)) return;
        if (stateOf(form).frozen) return;   // sent; waiting for the answer
        write(form, false);
    }

    // Once a form has been sent, what it says is frozen until somebody
    // touches it again. New Order empties its basket the moment it is
    // sent, before the kitchen has answered; saving that empty basket
    // over the full one would lose the order if the answer never came.
    // A person's own tap or keystroke after sending is what unfreezes it,
    // and the tap that sent it came before the send, so it does not.
    function onGesture(event) {
        if (!event.isTrusted) return;
        var form = event.target && event.target.closest
            ? event.target.closest("form") : null;
        if (!form || !form.__cafora || !form.__cafora.frozen) return;
        if (event.timeStamp <= form.__cafora.frozen) return;

        form.__cafora.frozen = 0;
        window.setTimeout(function () {
            if (draftable(form)) write(form, false);
        }, 0);
    }

    // Capture, so this sees the form as it was sent - before a page's own
    // handler empties it.
    function onSubmit(event) {
        var form = event.target;
        if (!draftable(form)) return;
        write(form, true);
        stateOf(form).frozen = performance.now();

        var action = location.pathname;
        try {
            action = new URL(form.getAttribute("action") || location.href,
                             location.href).pathname;
        } catch (error) { /* the page's own address */ }
        setPending({ key: keyOf(form), path: location.pathname,
                     action: action });
    }

    function onPosted(event) {
        var url = event.detail && event.detail.url;
        var path = location.pathname;
        try {
            path = new URL(url, location.href).pathname;
        } catch (error) { /* keep the current one */ }
        settle(path);
    }

    function start() {
        document.addEventListener("input", onChange);
        document.addEventListener("change", onChange);
        document.addEventListener("click", onGesture);
        document.addEventListener("keydown", onGesture);
        document.addEventListener("submit", onSubmit, true);
        document.addEventListener("instant:posted", onPosted);
        document.addEventListener("instant:load", arrive);

        // After the page's own start-up code, so a restored value reaches
        // listeners that are there to hear it.
        if (document.readyState === "complete") arrive();
        else document.addEventListener("DOMContentLoaded", arrive);
    }

    // For a page that posts its own form and knows how it went.
    window.Drafts = {
        forget: function (form) {
            if (!form) return;
            drop(keyOf(form));
            setPending(null);
            if (form.__cafora) form.__cafora.frozen = 0;
        },
        keep: function (form) {
            if (!form) return;
            setPending(null);
            if (form.__cafora) form.__cafora.frozen = 0;
            if (draftable(form)) write(form, false);
        }
    };

    // Registered outside instant.js's page scope, or every listener here
    // would be torn down on the first navigation.
    if (window.Instant && window.Instant.shell) {
        window.Instant.shell(start);
    } else if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
}());
