// Chart behaviour for server-rendered SVG charts (see charts.py).
//
// Two jobs, both optional -- a chart with this file blocked still draws:
//   * show times in the chosen time zone, in this browser's clock style;
//   * a hover line with a readout of the bucket under the pointer.
//
// A file rather than an inline script so the CSP needs nothing new: it is
// served from 'self'. Styles are set through the CSSOM (element.style), which
// the policy allows, rather than as inline style attributes, which it does not.
(function () {
  'use strict';

  // Times are shown in the zone chosen under Preferences (data-tz on each
  // chart), not the browser's -- so a chart reads the same as the rest of the
  // page, whichever machine is looking at it.
  function formats(tz) {
    var zone = tz ? { timeZone: tz } : {};
    return {
      time: Object.assign({ hour: '2-digit', minute: '2-digit' }, zone),
      date: Object.assign({ month: 'short', day: 'numeric' }, zone)
    };
  }

  function local(date, withDate, f) {
    var time = date.toLocaleTimeString(undefined, f.time);
    return withDate ? date.toLocaleDateString(undefined, f.date) + ' ' + time : time;
  }

  document.querySelectorAll('figure.chart').forEach(function (figure) {
    var f;
    try { f = formats(figure.dataset.tz); new Date().toLocaleTimeString(undefined, f.time); }
    catch (e) { f = formats(null); }  // an unknown zone: fall back to the browser's
    figure.querySelectorAll('time[datetime]').forEach(function (el) {
      var d = new Date(el.getAttribute('datetime'));
      if (isNaN(d)) return;
      el.textContent = el.dataset.style === 'date'
        ? d.toLocaleDateString(undefined, f.date)
        : local(d, false, f);
    });
    figure.__formats = f;
  });

  document.querySelectorAll('figure.chart').forEach(function (figure) {
    var readouts;
    try { readouts = JSON.parse(figure.dataset.readouts || '[]'); } catch (e) { return; }
    if (!readouts.length) return;
    var start = Number(figure.dataset.start);
    var width = Number(figure.dataset.width);
    var plot = figure.querySelector('.chart-plot');
    var cursor = figure.querySelector('line.cursor');
    var box = figure.querySelector('.chart-readout');
    if (!plot || !cursor || !box) return;
    var count = readouts.length;

    function hide() {
      box.hidden = true;
      cursor.classList.remove('on');
    }

    plot.addEventListener('pointermove', function (event) {
      var rect = plot.getBoundingClientRect();
      var fraction = (event.clientX - rect.left) / rect.width;
      var index = Math.max(0, Math.min(count - 1, Math.floor(fraction * count)));
      var text = readouts[index];
      if (!text) { hide(); return; }
      var x = ((index + 0.5) * 1000 / count).toFixed(1);
      cursor.setAttribute('x1', x);
      cursor.setAttribute('x2', x);
      cursor.classList.add('on');
      box.textContent = local(new Date((start + index * width) * 1000), true,
                              figure.__formats || formats(null)) + ' · ' + text;
      box.hidden = false;
      // Keep the box inside the plot: flip to the left of the line past halfway.
      var px = (index + 0.5) / count * rect.width;
      if (px > rect.width / 2) {
        box.style.left = '';
        box.style.right = (rect.width - px + 8) + 'px';
      } else {
        box.style.right = '';
        box.style.left = (px + 8) + 'px';
      }
    });
    plot.addEventListener('pointerleave', hide);
  });
})();
