(() => {
  const overflow = [];
  const targets = Array.from(document.querySelectorAll("[data-fit]"));

  function boundedAncestors(el) {
    const out = [];
    let cur = el.parentElement;
    while (cur && cur !== document.documentElement) {
      if (
        cur.hasAttribute("data-check-overflow") ||
        cur.classList.contains("cover-hero") ||
        cur.classList.contains("card-body") ||
        cur.classList.contains("list-card") ||
        cur.classList.contains("col-panel") ||
        cur.classList.contains("stat-card") ||
        cur.classList.contains("quote-stage") ||
        cur.classList.contains("timeline-item") ||
        cur.classList.contains("qa-item") ||
        cur.classList.contains("excerpt-box") ||
        cur.classList.contains("main-area") ||
        cur.classList.contains("canvas")
      ) {
        out.push(cur);
      }
      if (cur.classList.contains("canvas")) {
        break;
      }
      cur = cur.parentElement;
    }
    return out;
  }

  function boxFits(box) {
    return box.scrollHeight <= box.clientHeight + 2 && box.scrollWidth <= box.clientWidth + 2;
  }

  function fits(el, ancestors) {
    if (!boxFits(el)) {
      return false;
    }
    for (const anc of ancestors) {
      if (anc.clientHeight > 0 && !boxFits(anc)) {
        return false;
      }
    }
    return true;
  }

  for (const el of targets) {
    const raw = el.getAttribute("data-fit") || "24,96";
    const parts = raw.split(",").map((v) => parseInt(v.trim(), 10));
    const minSize = Number.isFinite(parts[0]) ? parts[0] : 22;
    const maxSize = Number.isFinite(parts[1]) ? parts[1] : 96;
    const slot = el.getAttribute("data-slot") || el.className || "slot";
    const targetFill = parseFloat(el.getAttribute("data-fill") || "0");

    if (el.clientHeight <= 0 || el.clientWidth <= 0) {
      continue;
    }

    const ancestors = boundedAncestors(el);
    let lo = minSize;
    let hi = maxSize;
    let best = minSize;

    while (lo <= hi) {
      const mid = Math.floor((lo + hi) / 2);
      el.style.fontSize = mid + "px";
      if (fits(el, ancestors)) {
        best = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }

    el.style.fontSize = best + "px";

    if (targetFill > 0 && fits(el, ancestors)) {
      const ratio = el.scrollHeight / Math.max(1, el.clientHeight);
      if (ratio < targetFill && best === maxSize) {
        el.style.lineHeight = "1.34";
        if (!fits(el, ancestors)) {
          el.style.lineHeight = "";
        }
      }
    }

    if (!boxFits(el)) {
      overflow.push({
        slot: slot,
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
      });
    }
  }

  for (const box of document.querySelectorAll("[data-check-overflow]")) {
    const slot = box.getAttribute("data-slot") || box.className || "box";
    if (box.clientHeight > 0 && !boxFits(box)) {
      overflow.push({
        slot: slot,
        scrollHeight: box.scrollHeight,
        clientHeight: box.clientHeight,
        scrollWidth: box.scrollWidth,
        clientWidth: box.clientWidth,
      });
    }
  }

  window.__overflow = overflow;
})();
