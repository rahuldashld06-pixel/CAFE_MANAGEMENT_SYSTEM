/*
 * A "show password" button on every password field.
 *
 * Typing a password you cannot see is how people end up locked out, so
 * each field gets an eye button that reveals what was typed while it is
 * held open. Nothing is stored or sent anywhere; it flips the input
 * between password and text and back.
 *
 * The buttons are added here rather than written into each template
 * because there are nine fields across five pages, three of which are
 * standalone screens with their own styling. One implementation keeps
 * them identical. If this script never runs, every field behaves exactly
 * as it did before.
 */
(function () {
    "use strict";

    // Inline rather than an icon font: the sign-in, registration and
    // password-reset screens do not load one.
    var EYE =
        '<svg class="pw-toggle__on" viewBox="0 0 24 24" fill="none"' +
        ' stroke="currentColor" stroke-width="2" stroke-linecap="round"' +
        ' stroke-linejoin="round" aria-hidden="true" focusable="false">' +
        '<path d="M1.5 12S5.5 4.5 12 4.5 22.5 12 22.5 12 18.5 19.5 12 19.5' +
        ' 1.5 12 1.5 12z"/><circle cx="12" cy="12" r="3.2"/></svg>';

    var EYE_OFF =
        '<svg class="pw-toggle__off" viewBox="0 0 24 24" fill="none"' +
        ' stroke="currentColor" stroke-width="2" stroke-linecap="round"' +
        ' stroke-linejoin="round" aria-hidden="true" focusable="false">' +
        '<path d="M17.9 17.9A10.1 10.1 0 0 1 12 19.5C5.5 19.5 1.5 12 1.5 12' +
        'a18.5 18.5 0 0 1 5.1-5.9M9.9 4.75A9.1 9.1 0 0 1 12 4.5' +
        'c6.5 0 10.5 7.5 10.5 7.5a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07' +
        'a3.2 3.2 0 1 1-4.24-4.24"/>' +
        '<line x1="2.5" y1="2.5" x2="21.5" y2="21.5"/></svg>';

    function label(button, showing) {
        var text = showing ? "Hide password" : "Show password";
        button.setAttribute("aria-pressed", showing ? "true" : "false");
        button.setAttribute("aria-label", text);
        button.title = text;
    }

    function enhance(input) {
        if (input.getAttribute("data-pw-view") === "on") return;
        input.setAttribute("data-pw-view", "on");

        // Once revealed the field is a plain text input, and a phone will
        // helpfully capitalise the first letter and underline the rest as a
        // spelling mistake. Neither is wanted in a password.
        input.setAttribute("autocapitalize", "none");
        input.setAttribute("autocorrect", "off");
        input.setAttribute("spellcheck", "false");

        var wrap = document.createElement("div");
        wrap.className = "pw-field";
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);

        var button = document.createElement("button");
        // Not a submit button: these sit inside forms, and the default
        // type would send the form on the first click.
        button.type = "button";
        button.className = "pw-toggle";
        button.tabIndex = -1;        // Tab still runs label -> field -> next
        button.innerHTML = EYE + EYE_OFF;
        label(button, false);
        wrap.appendChild(button);
    }

    function scan() {
        var fields = document.querySelectorAll('input[type="password"]');
        for (var i = 0; i < fields.length; i++) enhance(fields[i]);
    }

    function onClick(event) {
        var button = event.target && event.target.closest
            ? event.target.closest(".pw-toggle")
            : null;
        if (!button) return;

        event.preventDefault();

        var input = button.parentNode.querySelector("input");
        if (!input) return;

        var showing = input.type === "text";
        input.type = showing ? "password" : "text";
        label(button, !showing);

        // Changing the type moves the caret to the start in some browsers,
        // which is jarring halfway through typing. Put it back at the end.
        try {
            var at = input.value.length;
            input.focus();
            input.setSelectionRange(at, at);
        } catch (error) {
            /* Not every browser allows a selection range here. */
        }
    }

    function start() {
        document.addEventListener("click", onClick);
        // instant.js swaps a new page in without a fresh load, so its
        // fields have never been through the code above.
        document.addEventListener("instant:load", scan);
        scan();
    }

    // Both listeners have to outlive every page swap. The shell starts
    // instant.js's page scope while the document is still parsing, so by
    // the time this deferred script runs, anything registered the ordinary
    // way is treated as belonging to the current page and is torn down on
    // the first navigation - the buttons would work until you went
    // anywhere, then quietly stop. Instant.shell() registers them outside
    // that scope. The sign-in, registration and reset screens have no
    // instant.js at all, and no swaps to survive.
    if (window.Instant && window.Instant.shell) {
        window.Instant.shell(start);
    } else if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
}());
