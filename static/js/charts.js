/*
 * The app's two charts, drawn as plain SVG: a curve over time, and a
 * donut of shares. Shared by the dashboard and Reports so the two draw
 * their figures the same way.
 *
 * Colours come from the theme's own variables, so every theme and every
 * cafe's chosen accent draws its own charts.
 */
(function () {
    if (window.CafeCharts) return;

    var money = new Intl.NumberFormat("en-IN", {maximumFractionDigits: 0});

    function esc(value) {
        var box = document.createElement("div");
        box.textContent = value == null ? "" : String(value);
        return box.innerHTML;
    }

    function compact(value, unit) {
        var sign = unit === "" ? "" : "₹";
        if (value >= 100000) return sign + (value / 100000).toFixed(1) + "L";
        if (value >= 1000) return sign + (value / 1000).toFixed(1) + "k";
        return sign + Math.round(value);
    }

    /**
     * A smooth curve with the area under it filled, guide lines labelled at
     * the left, at most seven labels along the bottom, and the value under
     * the pointer shown in a tip.
     *
     * parts: {box, svg, tip, empty} elements; points: [{label, value}];
     * unit: "₹" (the default) or "" for plain counts.
     */
    function curve(parts, points, unit) {
        var svg = parts.svg, box = parts.box;
        if (!svg || !box) return;
        points = points || [];
        unit = unit === undefined ? "₹" : unit;
        var gradient = "curveFill" + Math.random().toString(36).slice(2, 8);
        var W = Math.max(box.clientWidth, 280);
        var H = Math.max(box.clientHeight, 180);
        var left = 46, right = 8, top = 14, bottom = 30;
        var n = points.length;
        var values = points.map(function (p) { return p.value; });
        var peak = Math.max.apply(null, values.concat([0]));
        var ceiling = peak > 0 ? peak * 1.18 : (unit === "" ? 4 : 100);
        var span = W - left - right;

        function x(i) { return left + (n > 1 ? i * span / (n - 1) : span / 2); }
        function y(v) { return top + (1 - v / ceiling) * (H - top - bottom); }

        var out = [];
        out.push('<defs><linearGradient id="' + gradient + '" x1="0" y1="0" x2="0" y2="1">' +
                 '<stop offset="0" style="stop-color:var(--copper-light);stop-opacity:.30"/>' +
                 '<stop offset="1" style="stop-color:var(--copper-light);stop-opacity:0"/>' +
                 '</linearGradient></defs>');

        var labels = [];
        out.push('<g class="curve__grid">');
        for (var g = 0; g <= 3; g++) {
            var level = ceiling * g / 3;
            var gy = y(level);
            out.push('<line x1="' + left + '" x2="' + (W - right) + '" y1="' + gy + '" y2="' + gy + '"/>');
            labels.push('<text x="' + (left - 8) + '" y="' + (gy + 4) + '" text-anchor="end">' +
                        compact(level, unit) + '</text>');
        }
        out.push('</g><g class="curve__y">' + labels.join("") + '</g>');

        if (n > 0) {
            var pts = points.map(function (p, i) { return [x(i), y(p.value)]; });
            var floor = y(0);
            var d = "M" + pts[0][0] + "," + pts[0][1];
            for (var i = 0; i < pts.length - 1; i++) {
                var p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1],
                    p3 = pts[i + 2] || p2;
                var c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
                var c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
                // Never dips below zero or over the top between two points.
                c1y = Math.min(floor, Math.max(top, c1y));
                c2y = Math.min(floor, Math.max(top, c2y));
                d += " C" + c1x + "," + c1y + " " + c2x + "," + c2y + " " + p2[0] + "," + p2[1];
            }
            out.push('<path d="' + d + " L" + pts[pts.length - 1][0] + "," + floor +
                     " L" + pts[0][0] + "," + floor + ' Z" fill="url(#' + gradient + ')"/>');
            out.push('<path class="curve__line" d="' + d + '"/>');

            var every = Math.max(1, Math.ceil(n / 7));
            out.push('<g class="curve__x">');
            for (var k = 0; k < n; k += every) {
                var anchor = k === 0 ? "start" : (k === n - 1 ? "end" : "middle");
                out.push('<text x="' + x(k) + '" y="' + (H - 8) + '" text-anchor="' + anchor + '">' +
                         esc(points[k].label) + '</text>');
            }
            out.push('</g>');
            out.push('<line class="curve__guide" y1="' + top + '" y2="' + floor +
                     '" x1="-10" x2="-10" visibility="hidden"/>');
            out.push('<circle class="curve__dot" r="5" cx="-10" cy="-10" visibility="hidden"/>');
        }

        svg.setAttribute("viewBox", "0 0 " + W + " " + H);
        svg.innerHTML = out.join("");
        if (parts.empty) parts.empty.hidden = peak > 0;

        var guide = svg.querySelector(".curve__guide");
        var dot = svg.querySelector(".curve__dot");
        var tip = parts.tip;

        svg.onmousemove = svg.ontouchstart = svg.ontouchmove = function (event) {
            if (!n || !guide) return;
            var rect = svg.getBoundingClientRect();
            var point = event.touches ? event.touches[0] : event;
            var at = (point.clientX - rect.left) * (W / rect.width);
            var i = Math.round((at - left) / (n > 1 ? span / (n - 1) : 1));
            i = Math.max(0, Math.min(n - 1, i));
            var px = x(i), py = y(points[i].value);
            guide.setAttribute("x1", px);
            guide.setAttribute("x2", px);
            guide.setAttribute("visibility", "visible");
            dot.setAttribute("cx", px);
            dot.setAttribute("cy", py);
            dot.setAttribute("visibility", "visible");
            if (tip) {
                tip.innerHTML = '<strong>' + (unit === "" ? "" : "₹") +
                                money.format(points[i].value) + '</strong>' + esc(points[i].label);
                tip.style.left = (px * rect.width / W) + "px";
                tip.style.top = (py * rect.height / H) + "px";
                tip.hidden = false;
            }
        };
        svg.onmouseleave = function () {
            if (tip) tip.hidden = true;
            if (guide) guide.setAttribute("visibility", "hidden");
            if (dot) dot.setAttribute("visibility", "hidden");
        };
    }

    /**
     * A donut of shares, largest first. slices: [{label, value}]. Each
     * slice is a shade of the accent, darkest for the largest.
     */
    function donut(svg, slices) {
        if (!svg) return;
        var total = slices.reduce(function (sum, s) { return sum + s.value; }, 0);
        var R = 60, r = 38, C = 70;
        var out = [];
        if (!total) {
            out.push('<circle cx="' + C + '" cy="' + C + '" r="' + ((R + r) / 2) +
                     '" fill="none" style="stroke:var(--espresso-800)" stroke-width="' + (R - r) + '"/>');
        } else {
            var start = -Math.PI / 2;
            slices.forEach(function (slice, index) {
                var angle = slice.value / total * Math.PI * 2;
                var end = start + angle;
                var tone = "var(--donut-" + Math.min(index, 5) + ")";
                if (angle >= Math.PI * 2 - 0.0001) {
                    out.push('<circle cx="' + C + '" cy="' + C + '" r="' + ((R + r) / 2) +
                             '" fill="none" style="stroke:' + tone + '" stroke-width="' + (R - r) + '"/>');
                } else {
                    var large = angle > Math.PI ? 1 : 0;
                    var p = function (rad, a) {
                        return (C + rad * Math.cos(a)).toFixed(2) + "," +
                               (C + rad * Math.sin(a)).toFixed(2);
                    };
                    out.push('<path style="fill:' + tone + '" d="M' + p(R, start) +
                             " A" + R + "," + R + " 0 " + large + " 1 " + p(R, end) +
                             " L" + p(r, end) +
                             " A" + r + "," + r + " 0 " + large + " 0 " + p(r, start) +
                             ' Z"><title>' + esc(slice.label) + '</title></path>');
                }
                start = end;
            });
        }
        svg.setAttribute("viewBox", "0 0 140 140");
        svg.innerHTML = out.join("");
    }

    window.CafeCharts = {curve: curve, donut: donut, money: money, esc: esc};
}());
