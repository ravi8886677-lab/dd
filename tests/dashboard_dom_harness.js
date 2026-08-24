// Loads dashboard.js against a stub DOM and reports which element
// listeners it actually registered.
//
// The bug this exists for parses cleanly and passes `node --check`: three
// addEventListener calls sat inside a function that only those listeners
// ever called, so they never ran and the YOLO controls were inert. No
// static check catches that. Running the file and seeing what it wires up
// does.
//
// Prints one JSON object: {"listeners": [["element-id","event"], ...]}.

const fs = require('fs');

const registered = [];

function makeElement(id) {
    const el = {
        id,
        style: {},
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        children: [],
        textContent: '',
        innerHTML: '',
        value: '0',
        checked: false,
        disabled: false,
        addEventListener(event) { registered.push([id, event]); },
        removeEventListener() {},
        appendChild() {}, removeChild() {}, remove() {},
        setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
        querySelector: () => makeElement(id + ' >'),
        querySelectorAll: () => [],
        closest: () => null,
        focus() {}, blur() {}, click() {},
        scrollIntoView() {},
        insertAdjacentHTML() {},
        getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 }),
        width: 0, height: 0,
        getContext: () => new Proxy({}, {
            get: (t, k) => (k === 'canvas' ? el
                : k === 'measureText' ? (() => ({ width: 0 }))
                : k === 'createLinearGradient' || k === 'createRadialGradient'
                    ? (() => ({ addColorStop() {} }))
                : (() => undefined)),
            set: () => true,
        }),
    };
    return el;
}

const elements = new Map();
function byId(id) {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
}

global.document = {
    getElementById: byId,
    querySelector: () => makeElement('?'),
    querySelectorAll: () => [],
    createElement: (tag) => makeElement('<' + tag + '>'),
    createElementNS: (ns, tag) => makeElement('<' + tag + '>'),
    addEventListener() {},
    body: makeElement('body'),
    head: makeElement('head'),
    documentElement: makeElement('html'),
};
global.window = {
    addEventListener() {}, removeEventListener() {},
    location: { href: '', search: '', hash: '', reload() {} },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    devicePixelRatio: 1,
    innerWidth: 1280, innerHeight: 800,
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    open() {},
};
global.navigator = { clipboard: { writeText: async () => {} }, userAgent: 'node' };
global.localStorage = {
    getItem: () => null, setItem() {}, removeItem() {}, clear() {},
};
// Never resolves: the file kicks off loaders at the end, and a resolved
// promise would run response handling against stubs for no benefit. What
// is being measured is the synchronous wiring, which is already done.
global.fetch = () => new Promise(() => {});
global.setInterval = () => 0;
global.clearInterval = () => {};
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.requestAnimationFrame = () => 0;
global.EventSource = function () { return { addEventListener() {}, close() {} }; };
global.WebSocket = function () { return { addEventListener() {}, close() {}, send() {} }; };
global.alert = () => {};
global.confirm = () => true;

const path = process.argv[2];
const source = fs.readFileSync(path, 'utf8');

try {
    // Indirect eval so the script runs in global scope, the way a browser
    // runs a <script> body. A `new Function` wrapper would put every
    // top-level declaration inside a function and make the very nesting
    // this checks for undetectable.
    (0, eval)(source);
} catch (err) {
    console.log(JSON.stringify({ error: String(err && err.message || err), listeners: registered }));
    process.exit(0);
}

console.log(JSON.stringify({ listeners: registered }));
