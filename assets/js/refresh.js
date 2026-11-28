/* RYZM home refresh: on narrow screens the dashboard opens as a summary
   (compass + current reading) so Breaking stays near the top; one button expands the full board. */
(function () {
  var dash = document.getElementById('cockpit');
  if (!dash) return;
  var mq = window.matchMedia('(max-width: 900px)');
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ck-toggle';
  btn.setAttribute('aria-controls', 'cockpit');
  dash.appendChild(btn);
  function set(summary) {
    dash.classList.toggle('is-summary', summary);
    btn.textContent = summary ? 'Open the full dashboard' : 'Show less';
    btn.setAttribute('aria-expanded', summary ? 'false' : 'true');
  }
  set(mq.matches);
  btn.addEventListener('click', function () {
    var open = dash.classList.contains('is-summary');
    set(!open);
    if (open) window.dispatchEvent(new Event('resize'));
  });
  if (mq.addEventListener) mq.addEventListener('change', function (e) { set(e.matches); });
})();
