(() => {
  'use strict';

  const root = document.documentElement;
  const viewportWidth = window.innerWidth;

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
