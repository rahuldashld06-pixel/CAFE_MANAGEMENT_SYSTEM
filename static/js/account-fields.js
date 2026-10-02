/*
 * Three helps for anyone choosing a username or a password.
 *
 *  - input[data-username-check]: says, as it is typed, whether that
 *    username is already taken - and offers a free one - rather than
 *    after the whole form has been sent back. A taken name also stops
 *    the form being sent.
 *  - input[data-strength]: says how hard the password would be to guess:
 *    Weak, Medium or Strong, with what would make it stronger.
 *  - .pw-rules: the checklist an admin's password must meet, ticked off
 *    as it is typed; the form does not go with a rule unmet.
 *
 * Runs on the sign-in screens and inside the app alike, so it brings its
 * own few styles; colours are the theme's where there is one. Rescans
 * after an instant page swap.
 */
(function () {
    "use strict";
    if (window.CafeAccountFields) return;

    var CSS =
        ".field-check{display:block;margin-top:6px;font-size:12.5px;font-weight:600;color:var(--cream-faint,#c2a88e)}" +
        ".field-check[data-state=free]{color:var(--sage,#8fbb99)}" +
        ".field-check[data-state=taken],.field-check[data-state=bad]{color:var(--berry,#e1614f)}" +
        ".field-check button{margin-left:4px;padding:0 2px;border:0;background:none;color:var(--copper-light,#eed6b6);font:inherit;text-decoration:underline;cursor:pointer}" +
        ".pw-meter{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-top:8px}" +
        ".pw-meter span{height:5px;border-radius:999px;background:var(--espresso-700,#775640);transition:background .15s}" +
        ".pw-meter[data-level='1'] span:nth-child(1){background:var(--berry,#e1614f)}" +
        ".pw-meter[data-level='2'] span:nth-child(-n+2){background:var(--gold,#f2c94c)}" +
        ".pw-meter[data-level='3'] span{background:var(--sage,#8fbb99)}" +
        ".pw-meter__say{display:block;margin-top:5px;font-size:12.5px;color:var(--cream-faint,#c2a88e)}" +
        ".pw-meter__say b{color:var(--cream,#fbf4ea)}";

    function addStyle() {
        if (document.getElementById("accountFieldsStyle")) return;
        var style = document.createElement("style");
        style.id = "accountFieldsStyle";
        style.textContent = CSS;
        document.head.appendChild(style);
    }

    function after(input, element) {
        // Below the reveal button's wrapper when there is one, so the note
        // sits under the whole field rather than inside it.
        var anchor = input.closest(".pw-field") || input;
        anchor.parentNode.insertBefore(element, anchor.nextSibling);
    }

    // ---- is the username free? ----
    function wireUsername(input) {
        if (input.dataset.checkWired) return;
        input.dataset.checkWired = "1";
        var note = document.createElement("small");
        note.className = "field-check";
        note.setAttribute("aria-live", "polite");
        input.setAttribute("aria-describedby",
            ((input.getAttribute("aria-describedby") || "") + " " + (note.id = "check_" + Math.random().toString(36).slice(2))).trim());
        after(input, note);
        var timer = null, asked = 0;

        function show(state, message, suggestions) {
            note.dataset.state = state || "";
            note.textContent = message || "";
            (suggestions || []).forEach(function (name) {
                var pick = document.createElement("button");
                pick.type = "button";
                pick.textContent = "Use " + name;
                pick.addEventListener("click", function () {
                    input.value = name;
                    input.dispatchEvent(new Event("input", {bubbles: true}));
                });
                note.appendChild(pick);
            });
            input.setCustomValidity(state === "taken" ? "That username is already taken." : "");
        }

        function check() {
            var wanted = input.value.trim();
            if (!wanted) { show("", ""); return; }
            var mine = ++asked;
            fetch("/api/username-check?u=" + encodeURIComponent(wanted), {
                credentials: "same-origin",
                headers: {"Accept": "application/json"}
            }).then(function (response) {
                return response.json();
            }).then(function (data) {
                if (mine !== asked) return;         // an older answer, overtaken
                show(data.state, data.message, data.state === "taken" ? data.suggestions : []);
            }).catch(function () { if (mine === asked) show("", ""); });
        }

        input.addEventListener("input", function () {
            input.setCustomValidity("");
            note.textContent = "";
            clearTimeout(timer);
            timer = setTimeout(check, 450);
        });
        input.addEventListener("blur", function () { clearTimeout(timer); check(); });
        if (input.value.trim()) check();
    }

    // ---- how strong is the password? ----
    var COMMON = ["password", "password1", "password123", "12345678", "123456789",
                  "1234567890", "qwerty123", "qwertyuiop", "iloveyou", "11111111",
                  "00000000", "abcd1234", "admin123", "welcome1", "letmein1",
                  "cafe1234", "coffee123", "cafora123"];

    function grade(value) {
        if (!value) return {level: 0, word: "", hint: ""};
        if (value.length < 8) {
            return {level: 1, word: "Too short", hint: "Use at least 8 characters."};
        }
        var points = 0;
        if (value.length >= 10) points++;
        if (value.length >= 14) points++;
        if (/[a-z]/.test(value) && /[A-Z]/.test(value)) points++;
        if (/\d/.test(value)) points++;
        if (/[^A-Za-z0-9]/.test(value)) points++;
        var lowered = value.toLowerCase();
        if (COMMON.indexOf(lowered) > -1 || /^(.)\1+$/.test(value) ||
                /^(0123|1234|abcd|qwer|asdf)/.test(lowered)) {
            points = 0;
        }
        if (points <= 1) {
            return {level: 1, word: "Weak", hint: "easy to guess. Add capitals, numbers or symbols, or make it longer."};
        }
        if (points <= 3) {
            return {level: 2, word: "Medium", hint: "could be harder to guess. A symbol or a few more characters would help."};
        }
        return {level: 3, word: "Strong", hint: "hard to guess."};
    }

    function wireStrength(input) {
        if (input.dataset.strengthWired) return;
        input.dataset.strengthWired = "1";
        var meter = document.createElement("div");
        meter.className = "pw-meter";
        meter.setAttribute("aria-hidden", "true");
        meter.innerHTML = "<span></span><span></span><span></span>";
        var say = document.createElement("small");
        say.className = "pw-meter__say";
        say.setAttribute("aria-live", "polite");
        after(input, say);
        after(input, meter);

        function show() {
            var result = grade(input.value);
            meter.dataset.level = String(result.level);
            say.innerHTML = "";
            if (!result.word) return;
            var word = document.createElement("b");
            word.textContent = result.word;
            say.appendChild(document.createTextNode("Strength: "));
            say.appendChild(word);
            say.appendChild(document.createTextNode(" - " + result.hint));
        }
        input.addEventListener("input", show);
        show();
    }

    // ---- an admin's password: the rules, ticked off ----
    // The same tests as password_rules_failed() in app.py, which checks
    // them again when the form arrives; the words to avoid are the
    // server's own, carried on the list (data-easy).
    var LEET = {"@": "a", "4": "a", "$": "s", "5": "s", "0": "o", "1": "i",
                "!": "i", "3": "e", "|": "l", "7": "t", "+": "t"};

    function easyToGuess(value, name, easy, parts) {
        if (!value) return true;
        var lowered = value.toLowerCase();
        var core = lowered.replace(/^[^a-z]+|[^a-z]+$/g, "");
        var letters = core.replace(/[@4$501!3|7+]/g, function (c) { return LEET[c]; })
                          .replace(/[^a-z]/g, "");
        var seen = {};
        for (var i = 0; i < letters.length; i++) seen[letters.charAt(i)] = 1;
        name = (name || "").trim().toLowerCase();
        return easy.indexOf(letters) > -1
            || (parts || []).some(function (part) { return part && letters.indexOf(part) > -1; })
            || (name.length >= 3 && lowered.indexOf(name) > -1)
            || Object.keys(seen).length <= 1;
    }

    var RULES = {
        length: function (v) { return v.length >= 8; },
        upper: function (v) { return /[A-Z]/.test(v); },
        lower: function (v) { return /[a-z]/.test(v); },
        number: function (v) { return /[0-9]/.test(v); },
        special: function (v) { return /[^A-Za-z0-9]/.test(v); },
        guess: function (v, name, easy, parts) { return !easyToGuess(v, name, easy, parts); }
    };

    function wireRules(box) {
        if (box.dataset.rulesWired) return;
        var input = document.getElementById(box.getAttribute("data-rules-for"));
        if (!input) return;
        box.dataset.rulesWired = "1";
        var easy = (box.getAttribute("data-easy") || "").split(",");
        var parts = (box.getAttribute("data-easy-parts") || "").split(",");
        var nameBox = document.getElementById(box.getAttribute("data-username-field") || "");
        var items = box.querySelectorAll("[data-rule]");
        input.setAttribute("aria-describedby",
            ((input.getAttribute("aria-describedby") || "") + " " + box.id).trim());

        function check() {
            var value = input.value;
            var name = nameBox ? nameBox.value : (box.getAttribute("data-username") || "");
            var all = true;
            for (var i = 0; i < items.length; i++) {
                var test = RULES[items[i].getAttribute("data-rule")];
                var met = test ? test(value, name, easy, parts) : true;
                items[i].setAttribute("data-met", met ? "true" : "false");
                if (!met) all = false;
            }
            box.setAttribute("data-all", all ? "true" : "false");
            if (all) box.classList.remove("pw-rules--shout");
            return all;
        }

        input.addEventListener("input", check);
        if (nameBox) nameBox.addEventListener("input", check);

        // Captured, so it is first: before a "save this?" question opens
        // and before the page's own sending. A password left blank where
        // blank keeps the old one is not being set, and is let through.
        if (input.form) {
            input.form.addEventListener("submit", function (event) {
                if (!input.value && !input.required) return;
                if (check()) return;
                event.preventDefault();
                event.stopImmediatePropagation();
                box.classList.add("pw-rules--shout");
                input.focus();
            }, true);
        }
        check();
    }

    function scan(root) {
        addStyle();
        [].forEach.call((root || document).querySelectorAll("input[data-username-check]"), wireUsername);
        [].forEach.call((root || document).querySelectorAll("input[data-strength]"), wireStrength);
        [].forEach.call((root || document).querySelectorAll(".pw-rules[data-rules-for]"), wireRules);
    }

    window.CafeAccountFields = {scan: scan, grade: grade, easyToGuess: easyToGuess};

    // Registered as shell code: this file runs once per real page load,
    // after the first page's scope has opened, so a listener added plainly
    // would be torn down with that page on the first swap - and every
    // page after it went without its meter and its username check.
    function start() {
        scan(document);
        document.addEventListener("instant:load", function () { scan(document); });
    }
    if (window.Instant && window.Instant.shell) {
        window.Instant.shell(start);
    } else if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
}());
