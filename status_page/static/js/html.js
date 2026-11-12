// @ts-check
// Markup built from template strings that escapes by itself. Every value put into `html` is
// escaped unless it is already markup made by `html` or `raw`, so "forgot to escape" cannot happen:
//
//   mount(list, html`<li class="${kind}">${name}</li>`)        // `name` and `kind` are escaped
//   html`<ul>${names.map((n) => html`<li>${n}</li>`)}</ul>`  // arrays of markup are joined
//   html`<select ${attrs({ disabled: !usable, title: why })}>` // true adds a bare attribute,
//                                                              // false, null and undefined add nothing

const ESCAPES = /** @type {Record<string, string>} */ ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
});

/**
 * @param {unknown} value
 * @returns {string}
 */
export const escape = (value) =>
  String(value ?? "").replace(/[&<>"]/g, (c) => ESCAPES[c]);

/** Markup that is safe to put into the page as it is. */
export class Safe {
  /** @param {string} text */
  constructor(text) {
    this.text = text;
  }

  toString() {
    return this.text;
  }
}

/**
 * Markup written by us, not data: it is not escaped.
 * @param {string} text
 */
export const raw = (text) => new Safe(text);

/**
 * @param {unknown} value
 * @returns {string}
 */
function part(value) {
  if (value instanceof Safe) return value.text;
  if (Array.isArray(value)) return value.map(part).join("");
  if (value === null || value === undefined || value === false) return "";
  return escape(value);
}

/**
 * @param {TemplateStringsArray} strings
 * @param {...unknown} values
 * @returns {Safe}
 */
export function html(strings, ...values) {
  let out = strings[0];
  values.forEach((value, i) => {
    out += part(value) + strings[i + 1];
  });
  return new Safe(out);
}

/**
 * Attributes for a tag: `true` is a bare attribute (`disabled`), `false`, `null` and `undefined`
 * leave it out, anything else is its escaped value.
 * @param {Record<string, string | number | boolean | null | undefined>} map
 */
export function attrs(map) {
  let out = "";
  for (const [name, value] of Object.entries(map)) {
    if (value === true) out += ` ${name}`;
    else if (value !== false && value !== null && value !== undefined)
      out += ` ${name}="${escape(value)}"`;
  }
  return new Safe(out);
}

/**
 * Puts markup into an element, replacing what was there.
 * @param {Element} element
 * @param {Safe} markup
 */
export function mount(element, markup) {
  element.innerHTML = markup.text;
}
