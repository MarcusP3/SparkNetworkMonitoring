// Chart behaviour for server-rendered SVG charts (see charts.py).
//
// Two jobs, both optional -- a chart with this file blocked still draws:
//   * show times in this browser's time zone (the server renders UTC);
//   * a hover line with a readout of the bucket under the pointer.
//
// A file rather than an inline script so the CSP needs nothing new: it is
// served from 'self'. Styles are set through the CSSOM (element.style), which
// the policy allows, rather than as inline style attributes, which it does not.
(function () {
  'use strict';

  var timeFmt = { hour: '2-digit', minute: '2-digit' };
  var dateFmt = { month: 'short', day: 'numeric' };

  function local(date, withDate) {
    var time = date.toLocaleTimeString(undefined, timeFmt);
    return withDate ? date.toLocaleDateString(undefined, dateFmt) + ' ' + time : time;
  }

  document.querySelectorAll('.chart time[datetime]').forEach(function (el) {
    var d = new Date(el.getAttribute('datetime'));
    if (isNaN(d)) return;
    el.textContent = el.dataset.style === 'date'
      ? d.toLocaleDateString(undefined, dateFmt)
      : local(d, false);
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
      box.textContent = local(new Date((start + index * width) * 1000), true) + ' · ' + text;
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
