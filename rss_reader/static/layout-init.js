(() => {
  'use strict';

  const root = document.documentElement;
  const viewportWidth = window.innerWidth;
  [['item-font-size','--item-size',14], ['summary-font-size','--summary-size',16], ['subscription-font-size','--subscription-size',14]].forEach(([name, css, fallback]) => {
    const value = Number(document.querySelector(`meta[name="${name}"]`)?.content || fallback);
    root.style.setProperty(css, `${Math.max(10, Math.min(24, value))}px`);
  });

  function restoreWidth(storageKey, propertyName, minimum, maximumRatio) {
    try {
      const stored = Number.parseFloat(localStorage.getItem(storageKey) || '');
      if (!Number.isFinite(stored)) return;
      const maximum = viewportWidth * maximumRatio;
      const width = Math.max(minimum, Math.min(maximum, stored));
      root.style.setProperty(propertyName, `${width}px`);
    } catch (_) {
      // Storage can be unavailable in hardened or private browser contexts.
      // The stylesheet defaults remain usable in that case.
    }
  }

  restoreWidth('rssLeftWidth', '--left-width', 190, 0.45);
  restoreWidth('rssMiddleWidth', '--middle-width', 300, 0.65);
})();
