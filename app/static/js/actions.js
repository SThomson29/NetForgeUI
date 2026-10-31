/*
 * Event handlers without inline JavaScript.
 *
 * The Content-Security-Policy blocks onclick="..." and the like, so pages
 * name the function to call in a data attribute instead:
 *
 *   data-click="fn"   data-change="fn"   data-input="fn"
 *
 * fn is a global function, called with `this` set to the element. A JSON
 * array in data-args is passed as its arguments; without one it gets none.
 *
 *   <form data-confirm="Delete this?">   asks before the form submits.
 *
 * Listeners sit on the document, so elements added later work too.
 */
(function () {
  function dispatch(event, attr) {
    const el = event.target.closest && event.target.closest('[' + attr + ']');
    if (!el) return;
    const name = el.getAttribute(attr);
    const fn = window[name];
    if (typeof fn !== 'function') {
      console.error('No function called ' + name);
      return;
    }
    const raw = el.getAttribute('data-args');
    fn.apply(el, raw ? JSON.parse(raw) : []);
  }

  for (const type of ['click', 'change', 'input']) {
    document.addEventListener(type, e => dispatch(e, 'data-' + type));
  }

  document.addEventListener('submit', e => {
    const msg = e.target.getAttribute('data-confirm');
    if (msg && !confirm(msg)) e.preventDefault();
  });
})();

/* Small handlers several pages share. */

function removeRow() { this.closest('tr').remove(); }

function submitForm() { this.form.submit(); }

function printPage() { window.print(); }

function showModal(id) { document.getElementById(id).classList.add('visible'); }

function hideModal(id) { document.getElementById(id).classList.remove('visible'); }
